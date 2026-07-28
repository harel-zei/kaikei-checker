"""
カテゴリ7: 経過勘定の消込整合チェック

7-1: 経過勘定の未清算差額（計上と支払の差額）
7-2: 同額の仕訳との突き合わせ（科目の取り違えの可能性）

BSチェック（bs_checker）との違い:
  bs_checker は「期首残高 + 当期増減」で残高を評価するため、
  補助科目の期首残高が提供されていない場合は誤検知を防ぐ目的で判定をスキップする。
  そのため「期首残高ファイルが無い顧問先」では補助科目単位の異常を検知できない。

判定の考え方（期首残高に依存しない）:
  未払費用・未払金のような経過勘定は「当月に計上し、翌月に支払う」サイクルを持つ。
  したがって各月末時点で
      D = （前月までの計上累計） −（当月までの精算累計）
  は、正常であれば毎月同じ値になる（その値は期首残高に相当する）。
  D が途中で変化したら、その変化額が「計上額と支払額の差額」を意味する。

  経費精算やカード利用のように毎月の金額が変動しても、累計で比較するため
  正しく判定できる（金額が一定であることを前提にしない）。

  例) 経費精算を毎月計上し翌月支払（金額は毎月変動）:
      正常                → D は毎月一定  → 差額なし
      5月に8,031円多く支払 → D が5月以降 8,031円ずれる → 差額として検知
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

MIN_CYCLE_MONTHS   = 4      # 精算サイクルの判定に必要な最小月数
MIN_ACCRUAL_MONTHS = 3      # 計上が何ヶ月以上あれば「定期的な計上」とみなすか
MIN_SETTLE_MONTHS  = 2      # 精算（反対仕訳）が何ヶ月以上あるか
MIN_DIFF_AMOUNT    = 100    # この額未満の差額は指摘しない（消費税端数等のノイズ抑制）

# 補助科目が付いていない行をまとめる系列ラベル
NO_SUB = "（補助科目なし）"


def check_reconciliation(df: pd.DataFrame) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    if df.empty or "date" not in df.columns or df["date"].dropna().empty:
        return issues

    periods = sorted(df["date"].dropna().dt.to_period("M").unique())
    if len(periods) < MIN_CYCLE_MONTHS:
        return issues

    records = _collect_clearing_records(df, periods)
    if not records:
        return issues

    diffs = _find_unsettled_differences(records, periods)
    issues.extend(_build_7_1_issues(diffs))
    issues.extend(_check_7_2_misposting(df, diffs))
    return issues


def _base_account(name: str) -> str:
    """経過勘定のうち、どの分類に属するかを返す（該当しなければ空文字）"""
    for acc in CLEARING_ACCOUNTS:
        if acc in name:
            return acc
    return ""


def _collect_clearing_records(df: pd.DataFrame, periods: list) -> List[Dict[str, Any]]:
    """経過勘定を科目×補助科目ごとに、月次の計上額・精算額として集計する"""
    work = df.copy()
    work["_d_acc"] = work["debit_account"].fillna("").astype(str).str.strip()
    work["_c_acc"] = work["credit_account"].fillna("").astype(str).str.strip()
    if "debit_sub" in work.columns and "credit_sub" in work.columns:
        work["_d_sub"] = work["debit_sub"].fillna("").astype(str).str.strip()
        work["_c_sub"] = work["credit_sub"].fillna("").astype(str).str.strip()
    else:
        work["_d_sub"] = ""
        work["_c_sub"] = ""
    work["_fp"] = work["date"].dt.to_period("M")

    def _norm_sub(s: str) -> str:
        return NO_SUB if s in ("", "nan", "None", "指定なし") else s

    acc_map: dict = {}
    for side, acc_col, sub_col, amt_col in (
        ("debit", "_d_acc", "_d_sub", "debit_amount"),
        ("credit", "_c_acc", "_c_sub", "credit_amount"),
    ):
        rows = work[(work[amt_col] != 0) & (work[acc_col].map(_base_account) != "")]
        if rows.empty:
            continue
        grouped = rows.groupby(
            [rows[acc_col], rows[sub_col].map(_norm_sub), rows["_fp"]]
        )[amt_col].sum()
        for (acc, sub, period), amount in grouped.items():
            rec = acc_map.setdefault((acc, sub), {"debit": {}, "credit": {}})
            rec[side][period] = rec[side].get(period, 0.0) + float(amount)

    results = []
    for (acc, sub), rec in acc_map.items():
        is_credit_normal = any(a in acc for a in CREDIT_NORMAL_ACCOUNTS)
        # 計上側（正常残高が増える側）と精算側（取り崩す側）
        accrual = rec["credit"] if is_credit_normal else rec["debit"]
        settle = rec["debit"] if is_credit_normal else rec["credit"]
        results.append({
            "account": acc,
            "sub": sub,
            "label": f"{acc}（{sub}）" if sub != NO_SUB else acc,
            "accrual": accrual,
            "settle": settle,
        })
    return results


# ──────────────────────────────────────────────────────────
# 7-1: 経過勘定の未清算差額
# ──────────────────────────────────────────────────────────
def _find_unsettled_differences(records: List[Dict[str, Any]],
                                periods: list) -> List[Dict[str, Any]]:
    """
    各月末の D =（前月までの計上累計）−（当月までの精算累計）を求め、
    D が途中から変化して最後まで戻らないものを「未清算差額」として抽出する。
    """
    diffs = []
    n = len(periods)

    for rec in records:
        accrual, settle = rec["accrual"], rec["settle"]
        if len([v for v in accrual.values() if v > 0]) < MIN_ACCRUAL_MONTHS:
            continue
        if len([v for v in settle.values() if v > 0]) < MIN_SETTLE_MONTHS:
            continue

        # D[k] = 前月までの計上累計 − 当月までの精算累計
        d_series = []
        cum_accrual = 0.0
        cum_settle = 0.0
        prev_cum_accrual = 0.0
        for p in periods:
            prev_cum_accrual = cum_accrual
            cum_accrual += accrual.get(p, 0.0)
            cum_settle += settle.get(p, 0.0)
            d_series.append(prev_cum_accrual - cum_settle)

        # 正常時に D が取る値（＝期首残高相当）を最頻値で推定する
        rounded = [round(d) for d in d_series]
        baseline = max(set(rounded), key=rounded.count)

        diff = d_series[-1] - baseline
        if abs(diff) < MIN_DIFF_AMOUNT:
            continue
        # 最終月だけのズレは「当月分の支払がまだ」という正常な状態なので除外する。
        # 前月時点でも同じズレが続いている＝解消されていない差額のみを対象とする。
        if n < 2 or abs((d_series[-2] - baseline) - diff) >= MIN_DIFF_AMOUNT:
            continue

        # ズレが生じた月を特定する
        changed_at = None
        for i, d in enumerate(d_series):
            if abs((d - baseline) - diff) < MIN_DIFF_AMOUNT:
                changed_at = periods[i]
                break

        diffs.append({
            "label": rec["label"],
            "account": rec["account"],
            "sub": rec["sub"],
            "amount": abs(diff),
            "overpaid": diff < 0,
            "month": str(changed_at) if changed_at else "全期間",
        })
    return diffs


def _build_7_1_issues(diffs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    issues = []
    for d in diffs:
        if d["overpaid"]:
            detail = (
                f"精算（支払）額が計上額を {d['amount']:,.0f}円 上回っています。"
                "支払超過、または計上漏れの可能性があります。"
            )
        else:
            detail = (
                f"計上額のうち {d['amount']:,.0f}円 が精算されずに残っています。"
                "支払漏れ、または計上先の科目誤りの可能性があります。"
            )
        issues.append({
            "level": "warning", "category": "7-1 経過勘定の未清算差額",
            "check_id": "7-1", "account": d["label"], "month": d["month"],
            "detail": {"amount": float(d["amount"]), "overpaid": d["overpaid"]},
            "message": (
                f"【7-1・中】{d['label']} は毎月の計上と精算が行われていますが、"
                f"{d['month']} 以降、計上額と精算額に差額が生じたまま解消されていません。"
                f"{detail}"
            ),
        })
    return issues


# ──────────────────────────────────────────────────────────
# 7-2: 同額の仕訳との突き合わせ（科目の取り違え）
# ──────────────────────────────────────────────────────────
def _check_7_2_misposting(df: pd.DataFrame,
                          diffs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    7-1 で検出した差額と同額の仕訳が、別の経過勘定に計上されていないかを探す。

    例) 未払費用（経費精算・清水）に 8,031円 の差額があり、
        同額 8,031円 の仕訳が未払金（アメックスカード）に計上されている
        → 本来どちらか一方に計上すべきものが取り違えられている可能性。

    実務担当者が「金額の一致」を手掛かりに科目誤りを見つける手順を再現している。
    """
    issues = []
    if not diffs:
        return issues

    d_acc = df["debit_account"].fillna("").astype(str)
    c_acc = df["credit_account"].fillna("").astype(str)
    d_amt = df["debit_amount"].round()
    c_amt = df["credit_amount"].round()

    for d in diffs:
        amount = round(d["amount"])
        src_account = d["account"]

        # 同額で、差額が生じた科目とは別の経過勘定に計上されている仕訳を探す
        def _other_clearing(name: str) -> bool:
            base = _base_account(name)
            return bool(base) and base != _base_account(src_account)

        mask = (
            ((d_amt == amount) & c_acc.map(_other_clearing)) |
            ((c_amt == amount) & d_acc.map(_other_clearing)) |
            ((d_amt == amount) & d_acc.map(_other_clearing)) |
            ((c_amt == amount) & c_acc.map(_other_clearing))
        )
        hits = df[mask]
        if hits.empty:
            continue

        lines = []
        counter_accounts = set()
        for _, row in hits.head(5).iterrows():
            for name in (str(row["debit_account"]), str(row["credit_account"])):
                if _other_clearing(name):
                    counter_accounts.add(name.strip())
            parts = [date_safe(row)]
            if slip_safe(row):
                parts.append(f"伝票No.{slip_safe(row)}")
            parts.append(f"{str(row['debit_account']).strip()} / {str(row['credit_account']).strip()}")
            if desc_safe(row):
                parts.append(f"摘要「{desc_safe(row)}」")
            lines.append("　".join(parts))

        detail_lines = "\n".join(f"・{l}" for l in lines)
        suffix = f"\n・ほか{len(hits) - 5}件" if len(hits) > 5 else ""
        issues.append({
            "level": "warning", "category": "7-2 科目取り違えの可能性",
            "check_id": "7-2", "account": d["label"], "month": d["month"],
            "detail": {"amount": float(amount), "counter_accounts": sorted(counter_accounts)},
            "message": (
                f"【7-2・中】{d['label']} の未清算差額 {amount:,.0f}円 と同額の仕訳が、"
                f"別の科目（{'、'.join(sorted(counter_accounts)[:3])}）に計上されています。"
                "本来どちらか一方に計上すべきものが、別の科目に計上されている"
                f"（科目の取り違え）可能性があります。\n【同額の仕訳】\n{detail_lines}{suffix}"
            ),
        })
    return issues
