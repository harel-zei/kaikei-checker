"""
貸借対照表（BS）チェックモジュール
議事録に基づくチェック項目を実装
"""
import pandas as pd
from typing import List, Dict, Any


# 資産勘定科目（借方残高が正常）
ASSET_ACCOUNTS = [
    "現金", "小口現金", "普通預金", "当座預金", "定期預金", "その他預金",
    "売掛金", "受取手形", "棚卸資産", "商品", "製品", "原材料", "仕掛品",
    "前払費用", "前払金", "仮払金", "仮払消費税", "未収入金",
    "建物", "機械装置", "車両運搬具", "工具器具備品", "土地", "建設仮勘定",
    "投資有価証券", "敷金", "保証金",
]

# 負債勘定科目（貸方残高が正常）
LIABILITY_ACCOUNTS = [
    "買掛金", "支払手形", "短期借入金", "長期借入金", "未払金", "未払費用",
    "前受金", "預り金", "仮受消費税", "未払法人税等", "未払消費税等",
    "社会保険料預り金", "源泉所得税預り金",
]

# 現預金科目
CASH_ACCOUNTS = ["現金", "小口現金", "普通預金", "当座預金", "定期預金"]

# 消費税科目（決算でゼロになるべき）
TAX_TEMP_ACCOUNTS = ["仮払消費税", "仮受消費税"]


def check_bs(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """BS項目のチェックを実行して指摘事項リストを返す"""
    issues = []

    issues.extend(_check_cash_balance(df))
    issues.extend(_check_tax_temp_accounts(df))
    issues.extend(_check_suspense_payments(df))
    issues.extend(_check_receivables_payables(df))

    return issues


def _get_account_balance(df: pd.DataFrame, account: str) -> pd.Series:
    """特定科目の月次残高を計算"""
    debit = df[df["debit_account"].astype(str).str.contains(account, na=False)].groupby(
        df["date"].dt.to_period("M")
    )["debit_amount"].sum()

    credit = df[df["credit_account"].astype(str).str.contains(account, na=False)].groupby(
        df["date"].dt.to_period("M")
    )["credit_amount"].sum()

    return debit.subtract(credit, fill_value=0)


def _check_cash_balance(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """現預金残高チェック：マイナス残高の検出"""
    issues = []

    for account in CASH_ACCOUNTS:
        monthly = _get_account_balance(df, account)
        cumulative = monthly.cumsum()

        negative_months = cumulative[cumulative < 0]
        if not negative_months.empty:
            for month, balance in negative_months.items():
                issues.append({
                    "level": "error",
                    "category": "BS",
                    "account": account,
                    "month": str(month),
                    "message": f"【要確認】{account} の残高がマイナスになっています（{balance:,.0f}円）。現預金残高はマイナスになり得ないため、仕訳漏れや誤入力の可能性があります。",
                })

    return issues


def _check_tax_temp_accounts(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """仮払消費税・仮受消費税：期首残高チェック"""
    issues = []

    for account in TAX_TEMP_ACCOUNTS:
        # 最初の月の残高
        first_month_entries = df[
            (df["debit_account"].astype(str).str.contains(account, na=False)) |
            (df["credit_account"].astype(str).str.contains(account, na=False))
        ]

        if first_month_entries.empty:
            continue

        first_month = df["date"].dropna().min()
        if pd.isna(first_month):
            continue

        first_period = first_month_entries[
            first_month_entries["date"].dt.month == first_month.month
        ]

        if not first_period.empty:
            debit_sum = first_period[
                first_period["debit_account"].astype(str).str.contains(account, na=False)
            ]["debit_amount"].sum()
            credit_sum = first_period[
                first_period["credit_account"].astype(str).str.contains(account, na=False)
            ]["credit_amount"].sum()

            opening_balance = debit_sum - credit_sum
            if abs(opening_balance) > 0:
                issues.append({
                    "level": "warning",
                    "category": "BS",
                    "account": account,
                    "month": str(first_month.to_period("M")),
                    "message": f"【要確認】{account} に期首残高（{opening_balance:,.0f}円）があります。決算整理後はゼロになるべき科目のため、前期の処理誤りの可能性があります。",
                })

    return issues


def _check_suspense_payments(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """仮払金チェック：長期滞留・法人税の誤処理"""
    issues = []
    account = "仮払金"

    entries = df[
        (df["debit_account"].astype(str).str.contains(account, na=False)) |
        (df["credit_account"].astype(str).str.contains(account, na=False))
    ]

    if entries.empty:
        return issues

    # 仮払金の残高計算
    debit_total = entries[
        entries["debit_account"].astype(str).str.contains(account, na=False)
    ]["debit_amount"].sum()

    credit_total = entries[
        entries["credit_account"].astype(str).str.contains(account, na=False)
    ]["credit_amount"].sum()

    balance = debit_total - credit_total

    if balance > 100000:
        issues.append({
            "level": "warning",
            "category": "BS",
            "account": account,
            "month": "全期間",
            "message": f"【要確認】仮払金の残高が {balance:,.0f}円 あります。内容不明のまま長期間放置されていないか確認してください。法人税の支払いが仮払金で処理されている場合は「未払法人税等」の取崩し仕訳に修正が必要です。",
        })

    # 摘要に「法人税」を含む仮払金を検出
    tax_entries = entries[
        entries.get("description", pd.Series(dtype=str)).astype(str).str.contains("法人税", na=False)
    ]
    if not tax_entries.empty:
        issues.append({
            "level": "error",
            "category": "BS",
            "account": account,
            "month": "全期間",
            "message": f"【要修正】摘要に「法人税」を含む仮払金仕訳が {len(tax_entries)}件 あります。法人税の支払いは「未払法人税等」を取り崩す仕訳に修正してください。",
        })

    return issues


def _check_receivables_payables(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """売掛金・買掛金の未消込チェック"""
    issues = []

    for account, direction in [("売掛金", "debit"), ("買掛金", "credit")]:
        if direction == "debit":
            debit_sum = df[
                df["debit_account"].astype(str).str.contains(account, na=False)
            ]["debit_amount"].sum()
            credit_sum = df[
                df["credit_account"].astype(str).str.contains(account, na=False)
            ]["credit_amount"].sum()
        else:
            debit_sum = df[
                df["debit_account"].astype(str).str.contains(account, na=False)
            ]["debit_amount"].sum()
            credit_sum = df[
                df["credit_account"].astype(str).str.contains(account, na=False)
            ]["credit_amount"].sum()

        balance = abs(debit_sum - credit_sum)

        if balance > 0:
            issues.append({
                "level": "info",
                "category": "BS",
                "account": account,
                "month": "全期間",
                "message": f"【確認】{account} の期末残高は {balance:,.0f}円 です。補助元帳を確認し、未回収・未払いの内訳が正しいか検証してください。分割回収や手数料差引による残高ズレがないか確認が必要です。",
            })

    return issues
