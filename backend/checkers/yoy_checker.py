"""
前年同月比較チェックモジュール
前期仕訳帳 + 前期末残高（主科目・補助科目）を使って当期と比較する
"""
import pandas as pd
from typing import List, Dict, Any, Optional

# 比較対象の主要科目（勘定科目キーワード, 集計サイド）
COMPARE_PL = [
    ("製品売上高",  "credit"), ("商品売上高",  "credit"),
    ("仕入高",      "debit"),  ("材料仕入高",  "debit"),  ("副資材仕入",  "debit"),
    ("給料手当",    "debit"),  ("役員報酬",    "debit"),  ("雑給",        "debit"),
    ("外注加工費",  "debit"),  ("広告宣伝費",  "debit"),
    ("地代家賃",    "debit"),  ("水道光熱費",  "debit"),
    ("修繕費",      "debit"),  ("接待交際費",  "debit"),
    ("減価償却費",  "debit"),  ("法定福利費",  "debit"),
]

ALERT_THRESHOLD = 30.0   # 変動率（%）のアラート閾値
MIN_AMOUNT_DIFF = 50000  # 最小差額（ノイズ除去）


def check_yoy(
    current_df: pd.DataFrame,
    prior_df:   pd.DataFrame,
    prior_end_balances: Optional[dict] = None,
) -> List[Dict[str, Any]]:
    """
    当期と前期を月単位で比較。
    prior_end_balances: 前期末残高辞書（parse_opening_balances の結果）
      → 前期の累積残高計算に使用（BS科目の前年同月残高比較）
    """
    issues = []
    issues.extend(_compare_pl_monthly(current_df, prior_df))
    if prior_end_balances:
        issues.extend(_compare_bs_balance(current_df, prior_df, prior_end_balances))
    return issues


# ──────────────────────────────────────────────────
# PL 月次金額の前年同月比較
# ──────────────────────────────────────────────────
def _compare_pl_monthly(
    current_df: pd.DataFrame,
    prior_df:   pd.DataFrame,
) -> List[Dict[str, Any]]:
    issues = []

    for account, side in COMPARE_PL:
        curr_by_month = _monthly_by_monthnum(current_df, account, side)
        prev_by_month = _monthly_by_monthnum(prior_df,   account, side)

        if curr_by_month.empty or prev_by_month.empty:
            continue

        common = curr_by_month.index.intersection(prev_by_month.index)
        for m in common:
            curr_val = curr_by_month[m]
            prev_val = prev_by_month[m]
            if prev_val == 0:
                continue

            pct = (curr_val - prev_val) / abs(prev_val) * 100
            if abs(pct) >= ALERT_THRESHOLD and abs(curr_val - prev_val) >= MIN_AMOUNT_DIFF:
                direction = "増加" if pct > 0 else "減少"
                issues.append({
                    "level":    "warning",
                    "category": "前年比",
                    "account":  account,
                    "month":    f"{m}月",
                    "message": (
                        f"【前年同月比】{account} が前年同月比 {pct:+.1f}% {direction}しています"
                        f"（前年: {prev_val:,.0f}円 → 当年: {curr_val:,.0f}円）。"
                        "原因を確認してください。"
                    ),
                    "detail": {
                        "current":    float(curr_val),
                        "prior":      float(prev_val),
                        "change_pct": float(pct),
                    },
                })

    return issues


# ──────────────────────────────────────────────────
# BS 月末残高の前年同月比較
# ──────────────────────────────────────────────────
BS_ACCOUNTS = [
    ("売掛金",    "debit"),
    ("買掛金",    "credit"),
    ("未払金",    "credit"),
    ("短期借入金","credit"),
    ("長期借入金","credit"),
]


def _compare_bs_balance(
    current_df:          pd.DataFrame,
    prior_df:            pd.DataFrame,
    prior_end_balances:  dict,
) -> List[Dict[str, Any]]:
    """
    前期末残高 + 前期仕訳帳で前期の月末残高を再現し、当期と比較する。
    前期末残高 = prior_end_balances の値（= 当期期首残高とは別に取得）
    """
    issues = []

    for account, normal_side in BS_ACCOUNTS:
        # 前期末残高（前期の最終残高）
        prior_end = prior_end_balances.get(account, None)
        if prior_end is None:
            continue

        # 前期の月次累積残高を再現
        prior_monthly_bal = _cumulative_balance(prior_df, account, normal_side, prior_end)
        # 当期の月次累積残高（期首残高なし → あくまで増減の比較）
        curr_monthly_bal  = _cumulative_balance(current_df, account, normal_side, 0)

        if prior_monthly_bal.empty or curr_monthly_bal.empty:
            continue

        # 月番号で比較
        prior_by_m = prior_monthly_bal.groupby(prior_monthly_bal.index.month).last()
        curr_by_m  = curr_monthly_bal.groupby(curr_monthly_bal.index.month).last()
        common = curr_by_m.index.intersection(prior_by_m.index)

        for m in common:
            curr_val  = curr_by_m[m]
            prior_val = prior_by_m[m]
            if prior_val == 0:
                continue
            pct = (curr_val - prior_val) / abs(prior_val) * 100
            if abs(pct) >= ALERT_THRESHOLD and abs(curr_val - prior_val) >= MIN_AMOUNT_DIFF:
                direction = "増加" if pct > 0 else "減少"
                issues.append({
                    "level":    "warning",
                    "category": "前年比(BS)",
                    "account":  account,
                    "month":    f"{m}月末",
                    "message": (
                        f"【前年同月末残高比較】{account} の残高が前年同月末比 {pct:+.1f}% {direction}しています"
                        f"（前年: {prior_val:,.0f}円 → 当年: {curr_val:,.0f}円）。"
                        "回収・支払状況を確認してください。"
                    ),
                    "detail": {
                        "current":    float(curr_val),
                        "prior":      float(prior_val),
                        "change_pct": float(pct),
                    },
                })

    return issues


# ──────────────────────────────────────────────────
# ユーティリティ
# ──────────────────────────────────────────────────
def _monthly_by_monthnum(df: pd.DataFrame, account: str, side: str) -> pd.Series:
    """指定科目・サイドの月次合計を月番号（1〜12）で返す"""
    col_acc = f"{side}_account"
    col_amt = f"{side}_amount"
    if col_acc not in df.columns:
        return pd.Series(dtype=float)
    mask = df[col_acc].astype(str).str.contains(account, na=False)
    rows = df[mask].copy()
    if rows.empty:
        return pd.Series(dtype=float)
    rows["_m"] = rows["date"].dt.month
    return rows.groupby("_m")[col_amt].sum()


def _cumulative_balance(
    df: pd.DataFrame,
    account: str,
    normal_side: str,   # "debit" or "credit"
    opening: float,
) -> pd.Series:
    """
    月次累積残高を Period インデックスで返す。
    normal_side="debit"  → 残高 = opening + 借方累計 - 貸方累計
    normal_side="credit" → 残高 = opening + 貸方累計 - 借方累計
    """
    d_rows = df[df["debit_account"].astype(str).str.contains(account, na=False)]
    c_rows = df[df["credit_account"].astype(str).str.contains(account, na=False)]

    period = df["date"].dt.to_period("M")
    monthly_d = d_rows.groupby(period.loc[d_rows.index])["debit_amount"].sum()
    monthly_c = c_rows.groupby(period.loc[c_rows.index])["credit_amount"].sum()

    if normal_side == "debit":
        net = monthly_d.subtract(monthly_c, fill_value=0)
    else:
        net = monthly_c.subtract(monthly_d, fill_value=0)

    return net.cumsum() + opening
