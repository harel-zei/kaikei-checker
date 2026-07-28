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
# 補助科目を合算した「科目全体」の系列を表す内部ラベル
ACCOUNT_TOTAL = "__ACCOUNT_TOTAL__"


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
    # 科目全体（補助科目を合算）の系列も作る。
    # 計上時は補助科目を付け、支払時は付けない（またはその逆）という記帳が
    # 実務では珍しくなく、補助科目単位だけで見ると計上と精算が別系列に
    # 分かれてしまい差額を検知できないため。
    totals: dict = {}
    for (acc, sub), rec in acc_map.items():
        tot = totals.setdefault(acc, {"debit": {}, "credit": {}})
        for side in ("debit", "credit"):
            for period, amount in rec[side].items():
                tot[side][period] = tot[side].get(period, 0.0) + amount

    def _emit(acc: str, sub: str, rec: dict, label: str):
        is_credit_normal = any(a in acc for a in CREDIT_NORMAL_ACCOUNTS)
        # 計上側（正常残高が増える側）と精算側（取り崩す側）
        results.append({
            "account": acc,
            "sub": sub,
            "label": label,
            "accrual": rec["credit"] if is_credit_normal else rec["debit"],
            "settle": rec["debit"] if is_credit_normal else rec["credit"],
        })

    for (acc, sub), rec in acc_map.items():
        _emit(acc, sub, rec, f"{acc}（{sub}）" if sub != NO_SUB else acc)
    for acc, rec in totals.items():
        # 補助科目が1つ（または無し）なら科目全体＝その補助科目なので重複させない
        subs = {s for (a, s) in acc_map if a == acc}
        if len(subs) > 1:
            _emit(acc, ACCOUNT_TOTAL, rec, f"{acc}（科目全体）")
    return results


# ──────────────────────────────────────────────────────────
# 7-1: 経過勘定の未清算差額
# ──────────────────────────────────────────────────────────
def _d_series(accrual: dict, settle: dict, periods: list, lag: int) -> list:
    """D[k] =（k-lag 月までの計上累計）−（k 月までの精算累計） を返す。

    lag は精算のタイミング（0=当月精算, 1=翌月精算, 2=翌々月精算）。
    正常に消込まれていれば D は毎月一定（その値は期首残高に相当）になる。
    """
    cum_accrual, cum_settle = 0.0, 0.0
    accrual_hist, series = [], []
    for i, p in enumerate(periods):
        cum_accrual += accrual.get(p, 0.0)
        accrual_hist.append(cum_accrual)
        cum_settle += settle.get(p, 0.0)
        base_idx = i - lag
        prior_accrual = accrual_hist[base_idx] if base_idx >= 0 else 0.0
        series.append(prior_accrual - cum_settle)
    return series


def _find_unsettled_differences(records: List[Dict[str, Any]],
                                periods: list) -> List[Dict[str, Any]]:
    """
    計上と精算の累計を突き合わせ、途中から生じて解消されない差額を抽出する。

    精算のタイミング（当月・翌月・翌々月）は顧問先ごとに異なるため、
    D が最も安定するタイミングを自動的に選ぶ。
    """
    diffs = []
    n = len(periods)

    for rec in records:
        accrual, settle = rec["accrual"], rec["settle"]
        if len([v for v in accrual.values() if v > 0]) < MIN_ACCRUAL_MONTHS:
            continue
        if len([v for v in settle.values() if v > 0]) < MIN_SETTLE_MONTHS:
            continue

        # 精算タイミングを推定（D の取りうる値の種類が最も少ないものを選ぶ）
        best = None
        for lag in (0, 1, 2):
            if lag >= n:
                continue
            series = _d_series(accrual, settle, periods, lag)
            variety = len({round(d / MIN_DIFF_AMOUNT) for d in series})
            if best is None or variety < best[0]:
                best = (variety, lag, series)
        if best is None:
            continue
        variety, lag, series = best

        # 安定した消込サイクルと言えない場合は判定しない（誤検知防止）
        # 正常値 + 差額（最大2種類の変化）までを許容する
        if variety > 3:
            continue

        # 正常時の D（＝期首残高相当）。消込サイクルが立ち上がる lag 月目を基準にする
        baseline = series[min(lag, n - 1)]

        diff = series[-1] - baseline
        if abs(diff) < MIN_DIFF_AMOUNT:
            continue

        # 最終月だけのズレの扱い:
        #   未精算方向（D が増加）→「当月分の支払がまだ」という正常な状態なので除外
        #   過大精算方向（D が減少）→ 支払超過であり最終月でも異常
        sustained = n >= 2 and abs((series[-2] - baseline) - diff) < MIN_DIFF_AMOUNT
        if not sustained and diff > 0:
            continue

        # ズレが生じた月を特定する
        changed_at = None
        for i, d in enumerate(series):
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

    return _dedupe(diffs)


def _dedupe(diffs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同じ科目で同額の差額が補助科目単位と科目全体の両方で出た場合、
    補助科目単位（より具体的な方）だけを残す。"""
    by_sub = {(d["account"], round(d["amount"])) for d in diffs if d["sub"] != ACCOUNT_TOTAL}
    return [
        d for d in diffs
        if d["sub"] != ACCOUNT_TOTAL or (d["account"], round(d["amount"])) not in by_sub
    ]


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
