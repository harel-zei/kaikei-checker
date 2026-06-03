"""
前年同月比較チェックモジュール
前期仕訳帳をアップロードすることで、当期と前期の月次金額を比較する
"""
import pandas as pd
from typing import List, Dict, Any

# 比較対象の主要科目
COMPARE_ACCOUNTS = [
    # PL
    ("製品売上高",  "credit"), ("商品売上高",  "credit"),
    ("仕入高",      "debit"),  ("材料仕入高",  "debit"),
    ("給料手当",    "debit"),  ("役員報酬",    "debit"),
    ("外注加工費",  "debit"),  ("広告宣伝費",  "debit"),
    ("地代家賃",    "debit"),  ("水道光熱費",  "debit"),
    ("修繕費",      "debit"),  ("接待交際費",  "debit"),
    ("減価償却費",  "debit"),
    # BS（月末残高ではなく月次増減を比較）
    ("売掛金",      "debit"),  ("買掛金",      "credit"),
]

# 前年同月比で警告する変動率（%）
ALERT_THRESHOLD = 30.0


def check_yoy(
    current_df: pd.DataFrame,
    prior_df: pd.DataFrame,
) -> List[Dict[str, Any]]:
    """
    当期と前期の月次金額を比較して前年同月比の異常変動を検出する

    前提:
      - current_df: 当期仕訳帳
      - prior_df  : 前期仕訳帳（同形式）
      - 両者の月は「月番号」で対応（1月 ↔ 1月）
    """
    issues = []

    for account, side in COMPARE_ACCOUNTS:
        curr_monthly = _monthly_amount(current_df, account, side)
        prev_monthly = _monthly_amount(prior_df,   account, side)

        if curr_monthly.empty or prev_monthly.empty:
            continue

        # 月番号（1〜12）で結合
        curr_by_m = curr_monthly.groupby(curr_monthly.index.month).sum()
        prev_by_m = prev_monthly.groupby(prev_monthly.index.month).sum()

        common_months = curr_by_m.index.intersection(prev_by_m.index)
        for m in common_months:
            curr_val = curr_by_m[m]
            prev_val = prev_by_m[m]

            if prev_val == 0:
                continue

            change_pct = (curr_val - prev_val) / abs(prev_val) * 100

            if abs(change_pct) >= ALERT_THRESHOLD and abs(curr_val - prev_val) > 50000:
                direction = "増加" if change_pct > 0 else "減少"
                issues.append({
                    "level":    "warning",
                    "category": "前年比",
                    "account":  account,
                    "month":    f"{m}月",
                    "message": (
                        f"【前年同月比】{account} が前年同月比 {change_pct:+.1f}% {direction}しています"
                        f"（前年: {prev_val:,.0f}円 → 当年: {curr_val:,.0f}円）。"
                        "原因を確認してください。"
                    ),
                    "detail": {
                        "current": float(curr_val),
                        "prior":   float(prev_val),
                        "change_pct": float(change_pct),
                    },
                })

    return issues


def _monthly_amount(df: pd.DataFrame, account: str, side: str) -> pd.Series:
    """指定科目・サイドの月次合計を返す"""
    col_acc = f"{side}_account" if side in ("debit", "credit") else "debit_account"
    col_amt = f"{side}_amount" if side in ("debit", "credit") else "debit_amount"
    if col_acc not in df.columns or col_amt not in df.columns:
        return pd.Series(dtype=float)

    mask = df[col_acc].astype(str).str.contains(account, na=False)
    rows = df[mask].copy()
    if rows.empty:
        return pd.Series(dtype=float)

    rows["_period"] = rows["date"].dt.to_period("M")
    return rows.groupby("_period")[col_amt].sum()
