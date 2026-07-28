"""
カテゴリ7: 経過勘定の消込整合チェック

7-1: 経過勘定の未清算差額（計上と支払の差額）
7-2: 同額の未清算差額の対応（科目の取り違えの可能性）

BSチェック（bs_checker）との違い:
  bs_checker は「期首残高 + 当期増減」で残高を評価するため、
  補助科目の期首残高が提供されていない場合は誤検知を防ぐ目的で判定をスキップする。
  そのため「期首残高ファイルが無い顧問先」では補助科目単位の異常を検知できない。

  本チェックは期首残高に依存しない。未払費用・預り金のような経過勘定は
  「毎月計上され、翌月に同額が支払われる」サイクルを持つため、
  当期の貸方合計と借方合計の差（当期ネット）は、正常なら
  「月次計上額の整数倍」（＝未払で残っている月数分）になる。
  そこから外れた端数は、計上額と支払額の差額を意味する。

  例) 月額50,000円の経費精算を6ヶ月:
      正常     → 貸方300,000 / 借方250,000 → ネット +50,000（1ヶ月分未払）→ 端数0
      8,031円多く支払 → 貸方300,000 / 借方258,031 → ネット +41,969 → 端数8,031 → 検知
"""
import pandas as pd
from typing import List, Dict, Any

from checkers.check_utils import desc_safe, date_safe, slip_safe

# 経過勘定（毎月の計上と精算でサイクルする科目）
CLEARING_ACCOUNTS = [
    "未払費用", "未払金", "預り金", "仮受金", "前受金", "前受収益",
    "立替金", "仮払金", "前払費用", "未収入金", "未収収益",
]
# 上記のうち貸方が正常残高となる（負債側）科目
CREDIT_NORMAL_ACCOUNTS = [
    "未払費用", "未払金", "預り金", "仮受金", "前受金", "前受収益",
]

MIN_ACCRUAL_MONTHS = 3      # 計上が何ヶ月以上あれば「定期的な精算サイクル」とみなすか
MIN_SETTLE_MONTHS  = 2      # 精算（反対仕訳）が何ヶ月以上あるか
MIN_MONTHLY_AMOUNT = 1_000  # 月次計上額がこの額未満はノイズとして除外
MIN_DIFF_AMOUNT    = 100    # この額未満の差額は指摘しない（消費税端数等のノイズ抑制）
DIFF_RATIO_MAX     = 0.5    # 端数が月次計上額のこの割合未満なら「未清算差額」とみなす
ACCRUAL_STABLE_R   = 0.25   # 月次計上額のブレ許容（(最大-最小) <= 中央値*この値 で「定額計上」）

# 補助科目が付いていない行をまとめる系列ラベル
NO_SUB = "（補助科目なし）"


def check_reconciliation(df: pd.DataFrame) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    if df.empty or "date" not in df.columns or df["date"].dropna().empty:
        return issues

    balances = _collect_clearing_balances(df)
    if not balances:
        return issues

    issues.extend(_check_7_1_unsettled_difference(balances))
    issues.extend(_check_7_2_amount_match(df, balances))
    return issues


def _base_account(name: str) -> str:
    """経過勘定のうち、どの分類に属するかを返す（該当しなければ空文字）"""
    for acc in CLEARING_ACCOUNTS:
        if acc in name:
            return acc
    return ""


def _collect_clearing_balances(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    経過勘定を科目×補助科目ごとに集計し、当期ネット増減と月次計上額を返す。

    Returns: [{account, sub, label, net, monthly, accrual_months, settle_months}, ...]
      net … 正常残高側から見た当期ネット増減（負債側は貸方－借方）
    """
    work = df.copy()
    work["_d_acc"] = work["debit_account"].fillna("").astype(str).str.strip()
    work["_c_acc"] = work["credit_account"].fillna("").astype(str).str.strip()
    has_sub = "debit_sub" in work.columns and "credit_sub" in work.columns
    if has_sub:
        work["_d_sub"] = work["debit_sub"].fillna("").astype(str).str.strip()
        work["_c_sub"] = work["credit_sub"].fillna("").astype(str).str.strip()
    else:
        work["_d_sub"] = ""
        work["_c_sub"] = ""
    work["_fp"] = work["date"].dt.to_period("M")

    def _norm_sub(s: str) -> str:
        return NO_SUB if s in ("", "nan", "None", "指定なし") else s

    # (実科目名, 補助科目) 単位で借方・貸方を集計
    acc_map: dict = {}
    for side, acc_col, sub_col, amt_col in (
        ("debit", "_d_acc", "_d_sub", "debit_amount"),
        ("credit", "_c_acc", "_c_sub", "credit_amount"),
    ):
        rows = work[(work[amt_col] != 0) & (work[acc_col].map(_base_account) != "")]
        if rows.empty:
            continue
        grouped = rows.groupby([rows[acc_col], rows[sub_col].map(_norm_sub), rows["_fp"]])[amt_col].sum()
        for (acc, sub, period), amount in grouped.items():
            rec = acc_map.setdefault((acc, sub), {"debit": {}, "credit": {}})
            rec[side][period] = rec[side].get(period, 0.0) + float(amount)

    results = []
    for (acc, sub), rec in acc_map.items():
        is_credit_normal = any(a in acc for a in CREDIT_NORMAL_ACCOUNTS)
        # 計上側（正常残高が増える側）と精算側
        accrual = rec["credit"] if is_credit_normal else rec["debit"]
        settle  = rec["debit"] if is_credit_normal else rec["credit"]

        accrual_total = sum(accrual.values())
        settle_total  = sum(settle.values())
        net = accrual_total - settle_total

        monthly_vals = [v for v in accrual.values() if v > 0]
        rec_out = {
            "account": acc,
            "sub": sub,
            "label": f"{acc}（{sub}）" if sub != NO_SUB else acc,
            "net": net,
            "monthly": float(pd.Series(monthly_vals).median()) if monthly_vals else 0.0,
            "monthly_span": float(max(monthly_vals) - min(monthly_vals)) if monthly_vals else 0.0,
            "accrual_months": len(monthly_vals),
            "settle_months": len([v for v in settle.values() if v > 0]),
            "is_credit_normal": is_credit_normal,
        }
        rec_out["remainder"] = _unsettled_remainder(rec_out)
        results.append(rec_out)
    return results


def _unsettled_remainder(b: dict):
    """
    「月次計上額の整数倍では説明できない端数」を返す（該当しなければ None）。

    経過勘定は毎月同額が計上され、翌月に精算されるため、当期ネットは
    正常なら月次計上額の整数倍（＝未払で残っている月数分）になる。
    その剰余が、計上額と支払額の差額を表す。
    """
    if b["accrual_months"] < MIN_ACCRUAL_MONTHS or b["settle_months"] < MIN_SETTLE_MONTHS:
        return None  # 定期的な精算サイクルが確認できない
    m = b["monthly"]
    if m < MIN_MONTHLY_AMOUNT:
        return None
    # 「整数倍で説明できる」という前提は毎月ほぼ定額で計上される場合にのみ成立する。
    # 月ごとに金額が大きく変動する科目では剰余に意味がないため対象外とする。
    if b["monthly_span"] > m * ACCRUAL_STABLE_R:
        return None

    remainder = abs(b["net"]) % m
    # 月次額をわずかに下回る端数（例: 49,999円 ≒ 1ヶ月分）は正常側に寄せる
    if remainder > m * (1 - DIFF_RATIO_MAX):
        remainder = m - remainder
    if remainder < MIN_DIFF_AMOUNT or remainder >= m * DIFF_RATIO_MAX:
        return None
    return remainder


# ──────────────────────────────────────────────────────────
# 7-1: 経過勘定の未清算差額
# ──────────────────────────────────────────────────────────
def _check_7_1_unsettled_difference(balances: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []

    for b in balances:
        remainder = b.get("remainder")
        if remainder is None:
            continue
        m = b["monthly"]
        net = b["net"]

        overpaid = net < 0
        if overpaid:
            detail = (
                f"当期の精算額が計上額を {abs(net):,.0f}円 上回っています（支払超過）。"
            )
        else:
            detail = (
                f"月次の計上額（約{m:,.0f}円）では説明できない "
                f"{remainder:,.0f}円 の端数が残っています。"
            )

        issues.append({
            "level": "warning", "category": "7-1 経過勘定の未清算差額",
            "check_id": "7-1", "account": b["label"], "month": "全期間",
            "detail": {"net": float(net), "remainder": float(remainder), "monthly": float(m)},
            "message": (
                f"【7-1・中】{b['label']} は毎月計上と精算が行われていますが、{detail}"
                "計上額と支払額に差額が生じている可能性があります。"
                "（支払額の誤り、または計上先の科目誤りが考えられます）"
            ),
        })
    return issues


# ──────────────────────────────────────────────────────────
# 7-2: 同額の未清算差額の対応（科目の取り違え）
# ──────────────────────────────────────────────────────────
def _check_7_2_amount_match(df: pd.DataFrame,
                            balances: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    ある経過勘定の未清算差額と、別の科目に残っている同額の残高を突き合わせる。

    例) 未払費用（経費精算・清水）に 8,031円 の差額があり、
        未払金（アメックスカード）にも同額 8,031円 の端数が残っている
        → 本来どちらか一方に計上すべきものが取り違えられている可能性。

    照合には「未清算の端数」を使う。残高そのものには当月分の正常な未払が
    含まれるため（例: 未払金 128,031円 = 当月分120,000円 + 端数8,031円）、
    生の残高同士では一致しない。
    """
    issues: List[Dict[str, Any]] = []

    # 未清算の端数（円単位に丸めた値）→ 該当する科目のリスト
    by_amount: dict = {}
    for b in balances:
        remainder = b.get("remainder")
        if remainder is None:
            continue
        amt = round(remainder)
        if amt < MIN_DIFF_AMOUNT:
            continue
        by_amount.setdefault(amt, []).append(b)

    for amt, group in sorted(by_amount.items()):
        # 「異なる勘定科目」同士の一致のみを対象にする
        distinct_accounts = {b["account"] for b in group}
        if len(distinct_accounts) < 2:
            continue

        labels = [b["label"] for b in group]
        hint = _find_matching_entries(df, amt, distinct_accounts)
        issues.append({
            "level": "warning", "category": "7-2 科目取り違えの可能性",
            "check_id": "7-2", "account": labels[0], "month": "全期間",
            "detail": {"amount": float(amt), "accounts": labels},
            "message": (
                f"【7-2・中】同額 {amt:,.0f}円 の未清算残高が複数の科目に残っています: "
                f"{'、'.join(labels[:4])}。"
                "本来どちらか一方に計上すべきものが、別の科目に計上されている"
                "（科目の取り違え）可能性があります。" + hint
            ),
        })
    return issues


def _find_matching_entries(df: pd.DataFrame, amount: float, accounts: set) -> str:
    """一致した金額と同額の仕訳を探し、日付・伝票番号・摘要を手掛かりとして返す"""
    acc_mask = (
        df["debit_account"].fillna("").astype(str).apply(lambda x: any(a in x for a in accounts)) |
        df["credit_account"].fillna("").astype(str).apply(lambda x: any(a in x for a in accounts))
    )
    amt_mask = (
        (df["debit_amount"].round() == round(amount)) |
        (df["credit_amount"].round() == round(amount))
    )
    hits = df[acc_mask & amt_mask]
    if hits.empty:
        return ""

    lines = []
    for _, row in hits.head(3).iterrows():
        parts = [date_safe(row)]
        if slip_safe(row):
            parts.append(f"伝票No.{slip_safe(row)}")
        if desc_safe(row):
            parts.append(f"摘要「{desc_safe(row)}」")
        lines.append("　".join(parts))
    return "\n【同額の仕訳】\n" + "\n".join(f"・{l}" for l in lines)
