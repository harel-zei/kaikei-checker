"""
前年同月比較チェックモジュール
累計比較版：当期の入力済み期間と前期の同期間を累計で比較する
"""
import pandas as pd
from typing import List, Dict, Any, Optional

# 比較対象の主要PL科目（累計比較）
COMPARE_PL = [
    ("製品売上高",  "credit"), ("商品売上高",  "credit"),
    ("仕入高",      "debit"),  ("材料仕入高",  "debit"),  ("副資材仕入",  "debit"),
    ("給料手当",    "debit"),  ("役員報酬",    "debit"),  ("雑給",        "debit"),
    ("外注加工費",  "debit"),  ("広告宣伝費",  "debit"),
    ("地代家賃",    "debit"),  ("水道光熱費",  "debit"),
    ("修繕費",      "debit"),  ("接待交際費",  "debit"),
    ("減価償却費",  "debit"),  ("法定福利費",  "debit"),
    ("租税公課",    "debit"),  # 月次変動が大きいため累計で比較
]

ALERT_THRESHOLD = 20.0    # 累計変動率（%）のアラート閾値
MIN_AMOUNT_DIFF = 200000  # 最小差額（ノイズ除去）


def check_yoy(
    current_df:          pd.DataFrame,
    prior_df:            pd.DataFrame,
    prior_ob:            Optional[dict] = None,
    last_complete_month: Optional["pd.Period"] = None,
    current_ob:          Optional[dict] = None,   # 当期首残高（BS比較の精度向上用）
) -> List[Dict[str, Any]]:
    issues = []
    issues.extend(_compare_pl_cumulative(current_df, prior_df, last_complete_month))
    if prior_ob:
        issues.extend(_compare_bs_balance(
            current_df, prior_df, prior_ob, last_complete_month, current_ob or {}
        ))
    return issues


# ──────────────────────────────────────────────────
# PL 累計比較（当期入力済み期間 vs 前期同期間）
# ──────────────────────────────────────────────────
def _compare_pl_cumulative(
    current_df:          pd.DataFrame,
    prior_df:            pd.DataFrame,
    last_complete_month: Optional["pd.Period"],
) -> List[Dict[str, Any]]:
    """
    当期と前期の「同じ月範囲の累計」を比較する。

    ・当期データは last_complete_month までに限定する
      （未入力期間を比較対象から除外）
    ・前期データは当期と同じ月番号の範囲を使う
    ・月をまたぐ会計年度（12月〜4月など）も正しく処理する
    ・ラベルは当期の実際の期間を表示（例: 12月〜4月 累計）
    """
    issues = []

    # 当期の Period 範囲（last_complete_month でカット）
    curr_periods = current_df["date"].dt.to_period("M").dropna().unique()
    curr_periods = sorted(curr_periods)
    if last_complete_month:
        curr_periods = [p for p in curr_periods if p <= last_complete_month]
    if not curr_periods:
        return issues

    curr_first = curr_periods[0]
    curr_last  = curr_periods[-1]

    # 当期に使う月番号のセット
    curr_months = {p.month for p in curr_periods}

    # 前期の対応する月を取得（同じ月番号）
    prior_periods = prior_df["date"].dt.to_period("M").dropna().unique()
    prior_months_available = {p.month for p in prior_periods}
    common_months = sorted(curr_months & prior_months_available)

    if not common_months:
        return issues

    # ラベル: 当期の実際の月範囲（日付順）で表示
    # 例: 12月〜4月 累計（年をまたいでも月番号を順番通り）
    period_label = _make_period_label(curr_periods)

    for account, side in COMPARE_PL:
        curr_total = _period_total(current_df, account, side, curr_months)
        prev_total = _period_total(prior_df,   account, side, common_months)

        if prev_total == 0 or curr_total == 0:
            continue

        pct = (curr_total - prev_total) / abs(prev_total) * 100
        if abs(pct) >= ALERT_THRESHOLD and abs(curr_total - prev_total) >= MIN_AMOUNT_DIFF:
            direction = "増加" if pct > 0 else "減少"
            issues.append({
                "level":    "warning",
                "category": "前年比",
                "account":  account,
                "month":    period_label,
                "message": (
                    f"【前年比較・累計】{account} の{period_label}が"
                    f"前年同期比 {pct:+.1f}% {direction}しています"
                    f"（前年: {prev_total:,.0f}円 → 当年: {curr_total:,.0f}円"
                    f"、差額: {curr_total - prev_total:+,.0f}円）。"
                    "原因を確認してください。"
                ),
                "detail": {
                    "current":    float(curr_total),
                    "prior":      float(prev_total),
                    "change_pct": float(pct),
                    "months":     [int(m) for m in common_months],
                },
            })

    return issues


# ──────────────────────────────────────────────────
# BS 期末残高の前年同期比較
# ──────────────────────────────────────────────────
BS_ACCOUNTS = [
    ("売掛金",     "debit"),
    ("買掛金",     "credit"),
    ("未払金",     "credit"),
    ("短期借入金", "credit"),
    ("長期借入金", "credit"),
]


def _compare_bs_balance(
    current_df:          pd.DataFrame,
    prior_df:            pd.DataFrame,
    prior_ob:            dict,
    last_complete_month: Optional["pd.Period"],
    current_ob:          dict,
) -> List[Dict[str, Any]]:
    """
    前期首残高 + 前期仕訳で前期期末残高を再現し、当期期末残高と比較する。
    当期末残高 = 当期首残高（current_ob）+ 当期純増減
    """
    issues = []

    for account, normal_side in BS_ACCOUNTS:
        prior_opening = prior_ob.get(account, None)
        if prior_opening is None:
            continue

        prior_end = _period_end_balance(prior_df,   account, normal_side, prior_opening)

        # 当期首残高: current_ob にあればそれを使用、なければ 前期末（prior_end）を使用
        curr_opening = current_ob.get(account, prior_end)
        curr_end     = _period_end_balance(current_df, account, normal_side, curr_opening)

        if prior_end == 0:
            continue

        diff = curr_end - prior_end
        if abs(diff) < MIN_AMOUNT_DIFF:
            continue

        # 前期末と当期末で符号が反転している場合は%が無意味なので差額で表現
        if (prior_end < 0) != (curr_end < 0):
            issues.append({
                "level": "warning", "category": "前年比(BS)",
                "account": account, "month": "期末残高比較",
                "message": (
                    f"【前年比較・期末残高】{account} の期末残高の符号が反転しています"
                    f"（前年期末: {prior_end:,.0f}円 → 当年期末: {curr_end:,.0f}円、"
                    f"差額: {diff:+,.0f}円）。残高の貸借が逆転しているため確認してください。"
                ),
                "detail": {"current": float(curr_end), "prior": float(prior_end)},
            })
            continue

        pct = diff / abs(prior_end) * 100
        if abs(pct) >= ALERT_THRESHOLD:
            direction = "増加" if pct > 0 else "減少"
            issues.append({
                "level":    "warning",
                "category": "前年比(BS)",
                "account":  account,
                "month":    "期末残高比較",
                "message": (
                    f"【前年比較・期末残高】{account} の期末残高が"
                    f"前年同期比 {pct:+.1f}% {direction}しています"
                    f"（前年期末: {prior_end:,.0f}円 → 当年期末: {curr_end:,.0f}円）。"
                    "回収・支払状況を確認してください。"
                ),
                "detail": {
                    "current":    float(curr_end),
                    "prior":      float(prior_end),
                    "change_pct": float(pct),
                },
            })

    return issues


# ──────────────────────────────────────────────────
# ユーティリティ
# ──────────────────────────────────────────────────
def _make_period_label(periods: list) -> str:
    """
    Period リスト（日付順ソート済み）から表示ラベルを生成する。
    例: [2025-12, 2026-01, 2026-02, 2026-03, 2026-04] → "12月〜4月 累計"
    """
    if not periods:
        return "累計"
    first_month = periods[0].month
    last_month  = periods[-1].month
    if first_month == last_month:
        return f"{first_month}月 累計"
    return f"{first_month}月〜{last_month}月 累計"


def _period_total(
    df:      pd.DataFrame,
    account: str,
    side:    str,
    months:  set,
) -> float:
    col_acc = f"{side}_account"
    col_amt = f"{side}_amount"
    if col_acc not in df.columns:
        return 0.0
    mask = (
        df[col_acc].astype(str).str.contains(account, na=False) &
        df["date"].dt.month.isin(months)
    )
    return float(df[mask][col_amt].sum())


def _period_end_balance(
    df:          pd.DataFrame,
    account:     str,
    normal_side: str,
    opening:     float,
) -> float:
    d = df[df["debit_account"].astype(str).str.contains(account, na=False)]["debit_amount"].sum()
    c = df[df["credit_account"].astype(str).str.contains(account, na=False)]["credit_amount"].sum()
    if normal_side == "debit":
        return float(opening + d - c)
    else:
        return float(opening + c - d)
