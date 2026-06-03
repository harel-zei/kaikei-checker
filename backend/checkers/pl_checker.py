"""
損益計算書（PL）チェックモジュール
"""
import pandas as pd
import numpy as np
from typing import List, Dict, Any

# 売上科目
SALES_ACCOUNTS = ["売上", "売上高", "売上金額"]

# 仕入科目
COGS_ACCOUNTS = ["仕入", "仕入高", "売上原価", "製造原価"]

# 毎月計上されるべき費用
MONTHLY_FIXED_EXPENSES = ["減価償却費", "地代家賃", "リース料"]

# 修繕費の資産計上判定基準（円）
REPAIR_CAPITALIZATION_THRESHOLD = 200000

# 雑費・支払手数料の肥大化判定（件数）
MISC_COUNT_THRESHOLD = 20


def check_pl(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """PL項目のチェックを実行して指摘事項リストを返す"""
    issues = []

    issues.extend(_check_gross_profit_ratio(df))
    issues.extend(_check_monthly_fixed_expenses(df))
    issues.extend(_check_repair_expenses(df))
    issues.extend(_check_misc_expenses(df))
    issues.extend(_check_month_over_month(df))

    return issues


def _get_monthly_amount(df: pd.DataFrame, accounts: List[str]) -> pd.Series:
    """指定科目リストの月次合計を返す"""
    pattern = "|".join(accounts)

    debit = df[df["debit_account"].astype(str).str.contains(pattern, na=False)].groupby(
        df["date"].dt.to_period("M")
    )["debit_amount"].sum()

    credit = df[df["credit_account"].astype(str).str.contains(pattern, na=False)].groupby(
        df["date"].dt.to_period("M")
    )["credit_amount"].sum()

    return credit.subtract(debit, fill_value=0)  # 売上は貸方


def _check_gross_profit_ratio(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """粗利率の月次変動チェック"""
    issues = []

    sales_pattern = "|".join(SALES_ACCOUNTS)
    cogs_pattern = "|".join(COGS_ACCOUNTS)

    # 売上（貸方）
    monthly_sales = df[
        df["credit_account"].astype(str).str.contains(sales_pattern, na=False)
    ].groupby(df["date"].dt.to_period("M"))["credit_amount"].sum()

    # 仕入（借方）
    monthly_cogs = df[
        df["debit_account"].astype(str).str.contains(cogs_pattern, na=False)
    ].groupby(df["date"].dt.to_period("M"))["debit_amount"].sum()

    if monthly_sales.empty:
        return issues

    # 粗利率計算
    combined = pd.DataFrame({"sales": monthly_sales, "cogs": monthly_cogs}).fillna(0)
    combined = combined[combined["sales"] > 0]

    if combined.empty:
        return issues

    combined["gross_profit_ratio"] = (combined["sales"] - combined["cogs"]) / combined["sales"] * 100

    mean_ratio = combined["gross_profit_ratio"].mean()
    std_ratio = combined["gross_profit_ratio"].std()

    if pd.isna(std_ratio) or std_ratio == 0:
        return issues

    # 平均±2σを超える月を異常値として検出
    for month, row in combined.iterrows():
        ratio = row["gross_profit_ratio"]
        if abs(ratio - mean_ratio) > 2 * std_ratio:
            if ratio > mean_ratio:
                reason = "仕入計上漏れの可能性があります"
            else:
                reason = "在庫計上額の誤り、または売上漏れの可能性があります"

            issues.append({
                "level": "warning",
                "category": "PL",
                "account": "粗利率",
                "month": str(month),
                "message": f"【要確認】{month} の粗利率が {ratio:.1f}% で、平均（{mean_ratio:.1f}%）から大きく乖離しています。{reason}。",
                "detail": {
                    "sales": float(row["sales"]),
                    "cogs": float(row["cogs"]),
                    "ratio": float(ratio),
                    "mean_ratio": float(mean_ratio),
                }
            })

    return issues


def _check_monthly_fixed_expenses(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """毎月計上すべき費用の計上漏れチェック"""
    issues = []

    for account in MONTHLY_FIXED_EXPENSES:
        entries = df[
            df["debit_account"].astype(str).str.contains(account, na=False)
        ]

        if entries.empty:
            continue

        monthly = entries.groupby(df["date"].dt.to_period("M"))["debit_amount"].sum()
        all_months = pd.period_range(
            start=df["date"].dropna().min().to_period("M"),
            end=df["date"].dropna().max().to_period("M"),
            freq="M"
        )

        missing_months = [m for m in all_months if m not in monthly.index or monthly[m] == 0]

        if missing_months:
            months_str = ", ".join(str(m) for m in missing_months[:3])
            suffix = f"（他{len(missing_months)-3}ヶ月）" if len(missing_months) > 3 else ""
            issues.append({
                "level": "warning",
                "category": "PL",
                "account": account,
                "month": months_str,
                "message": f"【要確認】{account} が計上されていない月があります：{months_str}{suffix}。毎月計上すべき費用のため、計上漏れの可能性があります。",
            })

    return issues


def _check_repair_expenses(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """修繕費の資産計上要否チェック"""
    issues = []

    repair_entries = df[
        df["debit_account"].astype(str).str.contains("修繕費", na=False)
    ]

    large_repairs = repair_entries[
        repair_entries["debit_amount"] >= REPAIR_CAPITALIZATION_THRESHOLD
    ]

    for _, row in large_repairs.iterrows():
        amount = row["debit_amount"]
        date = row["date"]
        desc = row.get("description", "")

        issues.append({
            "level": "warning",
            "category": "PL",
            "account": "修繕費",
            "month": str(date.to_period("M")) if pd.notna(date) else "不明",
            "message": f"【要確認】修繕費に {amount:,.0f}円 の大額計上があります（摘要: {desc}）。20万円以上の場合は資産計上の可能性があります。現状回復目的か価値向上目的かを確認してください。",
        })

    return issues


def _check_misc_expenses(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """雑費・支払手数料の肥大化チェック"""
    issues = []

    for account in ["雑費", "支払手数料"]:
        entries = df[
            df["debit_account"].astype(str).str.contains(account, na=False)
        ]

        if len(entries) > MISC_COUNT_THRESHOLD:
            total = entries["debit_amount"].sum()
            issues.append({
                "level": "info",
                "category": "PL",
                "account": account,
                "month": "全期間",
                "message": f"【提案】{account} に {len(entries)}件（合計 {total:,.0f}円）の取引が集中しています。チェックが困難になるため、専用勘定科目（例：システム費、振込手数料等）の新設を検討することをお勧めします。",
            })

    return issues


def _check_month_over_month(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """前月比較による異常値チェック（±50%超の変動を検出）"""
    issues = []

    # 主要費用科目をチェック
    # 租税公課は月によって大きく変動するため前月比チェックから除外（YoY累計で比較）
    target_accounts = ["給与", "外注費", "地代家賃", "広告宣伝費"]

    for account in target_accounts:
        entries = df[
            df["debit_account"].astype(str).str.contains(account, na=False)
        ]

        if entries.empty:
            continue

        monthly = entries.groupby(df["date"].dt.to_period("M"))["debit_amount"].sum()

        if len(monthly) < 2:
            continue

        for i in range(1, len(monthly)):
            prev = monthly.iloc[i - 1]
            curr = monthly.iloc[i]
            month = monthly.index[i]

            if prev == 0:
                continue

            change_rate = (curr - prev) / prev * 100

            if abs(change_rate) > 50 and abs(curr - prev) > 50000:
                direction = "増加" if change_rate > 0 else "減少"
                issues.append({
                    "level": "warning",
                    "category": "PL",
                    "account": account,
                    "month": str(month),
                    "message": f"【要確認】{account} が前月比 {change_rate:+.1f}% （{prev:,.0f}円 → {curr:,.0f}円）と大きく{direction}しています。原因を確認してください。",
                })

    return issues
