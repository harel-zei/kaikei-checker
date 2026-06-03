"""
前年同月比較チェックモジュール
累計比較版：月次ではなく「対象期間の合計」で前年と比較する
"""
import pandas as pd
from typing import List, Dict, Any, Optional

# 比較対象の主要PL科目
COMPARE_PL = [
    ("製品売上高",  "credit"), ("商品売上高",  "credit"),
    ("仕入高",      "debit"),  ("材料仕入高",  "debit"),  ("副資材仕入",  "debit"),
    ("給料手当",    "debit"),  ("役員報酬",    "debit"),  ("雑給",        "debit"),
    ("外注加工費",  "debit"),  ("広告宣伝費",  "debit"),
    ("地代家賃",    "debit"),  ("水道光熱費",  "debit"),
    ("修繕費",      "debit"),  ("接待交際費",  "debit"),
    ("減価償却費",  "debit"),  ("法定福利費",  "debit"),
]

ALERT_THRESHOLD = 20.0    # 累計変動率（%）のアラート閾値
MIN_AMOUNT_DIFF = 200000  # 最小差額（ノイズ除去）


def check_yoy(
    current_df:         pd.DataFrame,
    prior_df:           pd.DataFrame,
    prior_ob:           Optional[dict] = None,
) -> List[Dict[str, Any]]:
    issues = []
    issues.extend(_compare_pl_cumulative(current_df, prior_df))
    if prior_ob:
        issues.extend(_compare_bs_balance(current_df, prior_df, prior_ob))
    return issues


# ──────────────────────────────────────────────────
# PL 累計比較（当期期間合計 vs 前期同期間合計）
# ──────────────────────────────────────────────────
def _compare_pl_cumulative(
    current_df: pd.DataFrame,
    prior_df:   pd.DataFrame,
) -> List[Dict[str, Any]]:
    """
    当期と前期の「同じ月範囲の累計」を比較する。

    例: 当期が12月〜4月のデータなら、前期の12月〜4月合計と比較。
    単月比較ではなく累計なので、季節変動による誤検知が大幅に減少する。
    """
    issues = []

    # 当期の月範囲（月番号）を取得
    curr_months = set(current_df["date"].dt.month.dropna().astype(int).unique())
    prior_months = set(prior_df["date"].dt.month.dropna().astype(int).unique())
    common_months = sorted(curr_months & prior_months)

    if not common_months:
        return issues

    m_start = min(common_months)
    m_end   = max(common_months)
    period_label = f"{m_start}月〜{m_end}月 累計"

    for account, side in COMPARE_PL:
        curr_total = _period_total(current_df, account, side, common_months)
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
    current_df: pd.DataFrame,
    prior_df:   pd.DataFrame,
    prior_ob:   dict,
) -> List[Dict[str, Any]]:
    """前期首残高 + 前期仕訳で前期期末残高を再現し、当期期末残高と比較する"""
    issues = []

    # 当期の最終月を取得
    curr_last = current_df["date"].dt.to_period("M").dropna().max()
    # 前期の最終月
    prior_last = prior_df["date"].dt.to_period("M").dropna().max()

    for account, normal_side in BS_ACCOUNTS:
        prior_opening = prior_ob.get(account, None)
        if prior_opening is None:
            continue

        # 前期期末残高を再現
        prior_end = _period_end_balance(prior_df, account, normal_side, prior_opening)
        # 当期期末残高（期首残高なし → 当期増減のみ）
        curr_end  = _period_end_balance(current_df, account, normal_side, 0)

        if prior_end == 0:
            continue

        pct = (curr_end - prior_end) / abs(prior_end) * 100
        if abs(pct) >= ALERT_THRESHOLD and abs(curr_end - prior_end) >= MIN_AMOUNT_DIFF:
            direction = "増加" if pct > 0 else "減少"
            issues.append({
                "level":    "warning",
                "category": "前年比(BS)",
                "account":  account,
                "month":    f"期末残高比較",
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
def _period_total(
    df:      pd.DataFrame,
    account: str,
    side:    str,
    months:  list,
) -> float:
    """指定科目・サイドの指定月リストの合計を返す"""
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
    """期間全体の期末残高を返す（opening + 借方合計 - 貸方合計）"""
    d_total = df[df["debit_account"].astype(str).str.contains(account, na=False)]["debit_amount"].sum()
    c_total = df[df["credit_account"].astype(str).str.contains(account, na=False)]["credit_amount"].sum()
    if normal_side == "debit":
        return float(opening + d_total - c_total)
    else:
        return float(opening + c_total - d_total)
