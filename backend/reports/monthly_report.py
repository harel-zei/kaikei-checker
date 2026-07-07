"""
月次レポート作成モジュール
仕訳データから月次の損益推移（P/L）を集計してレポート化する
"""
import pandas as pd
from typing import List, Dict, Any

# 売上（収益）科目
SALES_ACCOUNTS = ["売上", "売上高", "売上金額"]

# 売上原価科目
COGS_ACCOUNTS = ["仕入", "仕入高", "売上原価", "製造原価"]

# 損益に含めない科目（BS科目）。これらに含まれる科目は販管費集計から除外する。
NON_PL_ACCOUNTS = [
    "現金", "小口現金", "普通預金", "当座預金", "定期預金", "その他預金",
    "売掛金", "受取手形", "棚卸資産", "商品", "製品", "原材料", "仕掛品",
    "前払費用", "前払金", "仮払金", "仮払消費税", "未収入金",
    "建物", "機械装置", "車両運搬具", "工具器具備品", "土地", "建設仮勘定",
    "投資有価証券", "敷金", "保証金",
    "買掛金", "支払手形", "短期借入金", "長期借入金", "未払金", "未払費用",
    "前受金", "預り金", "仮受消費税", "未払法人税等", "未払消費税等",
    "社会保険料預り金", "源泉所得税預り金",
    "資本金", "利益剰余金", "繰越利益剰余金", "元入金", "事業主貸", "事業主借",
]


def _matches(account: str, keywords: List[str]) -> bool:
    return any(kw in account for kw in keywords)


def _month_key(period) -> str:
    return str(period)


def build_monthly_report(df: pd.DataFrame) -> Dict[str, Any]:
    """
    月次のP/L推移レポートを生成する。

    Returns:
        {
          "months": ["2024-04", ...],
          "sales":         {"values": {month: amount}, "total": amount},
          "cogs":          {"values": {...}, "total": ...},
          "gross_profit":  {"values": {...}, "total": ...},
          "expenses":      [ {"account": str, "values": {...}, "total": ...}, ... ],
          "expense_total": {"values": {...}, "total": ...},
          "operating_profit": {"values": {...}, "total": ...},
        }
    """
    empty = {
        "months": [],
        "sales": {"values": {}, "total": 0},
        "cogs": {"values": {}, "total": 0},
        "gross_profit": {"values": {}, "total": 0},
        "expenses": [],
        "expense_total": {"values": {}, "total": 0},
        "operating_profit": {"values": {}, "total": 0},
    }

    dated = df[df["date"].notna()].copy()
    if dated.empty:
        return empty

    dated["period"] = dated["date"].dt.to_period("M")

    all_months = pd.period_range(
        start=dated["period"].min(),
        end=dated["period"].max(),
        freq="M",
    )
    months = [_month_key(m) for m in all_months]

    def zero_series() -> Dict[str, float]:
        return {m: 0.0 for m in months}

    # 売上高（貸方が正）
    sales = zero_series()
    # 売上原価（借方が正）
    cogs = zero_series()
    # 販管費：科目別（借方が正）
    expenses: Dict[str, Dict[str, float]] = {}

    for _, row in dated.iterrows():
        month = _month_key(row["period"])
        debit_acc = str(row.get("debit_account", "") or "")
        credit_acc = str(row.get("credit_account", "") or "")
        debit_amt = float(row.get("debit_amount", 0) or 0)
        credit_amt = float(row.get("credit_amount", 0) or 0)

        # 貸方側の集計（収益の増加）
        if _matches(credit_acc, SALES_ACCOUNTS):
            sales[month] += credit_amt

        # 借方側の集計（費用の発生）
        if _matches(debit_acc, COGS_ACCOUNTS):
            cogs[month] += debit_amt
        elif debit_acc and not _matches(debit_acc, SALES_ACCOUNTS) \
                and not _matches(debit_acc, NON_PL_ACCOUNTS):
            # BS科目・売上・原価以外の借方科目を販管費として集計
            expenses.setdefault(debit_acc, zero_series())
            expenses[debit_acc][month] += debit_amt

    # 売上・原価の戻り（借方の売上、貸方の原価）も反映して純額にする
    for _, row in dated.iterrows():
        month = _month_key(row["period"])
        debit_acc = str(row.get("debit_account", "") or "")
        credit_acc = str(row.get("credit_account", "") or "")
        debit_amt = float(row.get("debit_amount", 0) or 0)
        credit_amt = float(row.get("credit_amount", 0) or 0)

        if _matches(debit_acc, SALES_ACCOUNTS):
            sales[month] -= debit_amt
        if _matches(credit_acc, COGS_ACCOUNTS):
            cogs[month] -= credit_amt

    def series_total(values: Dict[str, float]) -> float:
        return sum(values.values())

    gross_profit = {m: sales[m] - cogs[m] for m in months}
    expense_total = {m: sum(exp[m] for exp in expenses.values()) for m in months}
    operating_profit = {m: gross_profit[m] - expense_total[m] for m in months}

    # 販管費は年間合計の大きい順にソート
    expense_rows = sorted(
        (
            {"account": acc, "values": vals, "total": series_total(vals)}
            for acc, vals in expenses.items()
        ),
        key=lambda r: r["total"],
        reverse=True,
    )

    return {
        "months": months,
        "sales": {"values": sales, "total": series_total(sales)},
        "cogs": {"values": cogs, "total": series_total(cogs)},
        "gross_profit": {"values": gross_profit, "total": series_total(gross_profit)},
        "expenses": expense_rows,
        "expense_total": {"values": expense_total, "total": series_total(expense_total)},
        "operating_profit": {"values": operating_profit, "total": series_total(operating_profit)},
    }
