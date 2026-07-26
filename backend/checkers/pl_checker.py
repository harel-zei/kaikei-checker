"""
損益計算書（PL）チェックモジュール
"""
import pandas as pd
from typing import List, Dict, Any

# 売上科目
SALES_ACCOUNTS = ["売上", "売上高", "売上金額"]

# 仕入科目（棚卸高も含める: 期首商品棚卸高は借方/期末商品棚卸高は貸方で
# 借方−貸方のネット計算により売上原価が正しく算出される）
COGS_ACCOUNTS = ["仕入", "仕入高", "売上原価", "製造原価", "棚卸高"]

# 雑費・支払手数料の肥大化判定（件数）
MISC_COUNT_THRESHOLD = 20


def check_pl(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """PL項目のチェックを実行して指摘事項リストを返す"""
    issues = []

    issues.extend(_check_gross_profit_ratio(df))
    # 定例費用の計上漏れは completeness_checker の 1-1 に一本化（重複指摘を防ぐ）
    # 修繕費の資産計上チェックは asset_checker の 3-3 に一本化（重複指摘を防ぐ）
    issues.extend(_check_misc_expenses(df))
    issues.extend(_check_month_over_month(df))

    return issues


def _check_gross_profit_ratio(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """粗利率の月次変動チェック"""
    issues = []

    sales_pattern = "|".join(SALES_ACCOUNTS)
    cogs_pattern = "|".join(COGS_ACCOUNTS)

    period = df["date"].dt.to_period("M")

    # 売上 = 貸方 − 借方（売上値引・返品を控除したネット額）
    sales_cr = df[
        df["credit_account"].fillna("").astype(str).str.contains(sales_pattern, na=False)
    ].groupby(period)["credit_amount"].sum()
    sales_dr = df[
        df["debit_account"].fillna("").astype(str).str.contains(sales_pattern, na=False)
    ].groupby(period)["debit_amount"].sum()
    monthly_sales = sales_cr.subtract(sales_dr, fill_value=0)

    # 売上原価 = 借方 − 貸方（仕入値引・期末棚卸高を控除したネット額）
    cogs_dr = df[
        df["debit_account"].fillna("").astype(str).str.contains(cogs_pattern, na=False)
    ].groupby(period)["debit_amount"].sum()
    cogs_cr = df[
        df["credit_account"].fillna("").astype(str).str.contains(cogs_pattern, na=False)
    ].groupby(period)["credit_amount"].sum()
    monthly_cogs = cogs_dr.subtract(cogs_cr, fill_value=0)

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
                "message": (
                    f"【要確認】{month} の粗利率が {ratio:.1f}% で、"
                    f"期中平均（{mean_ratio:.1f}%）から大きく乖離しています。{reason}。"
                    "※棚卸仕訳を決算月のみ計上している場合、月次粗利率は仕入ベースの簡易値です。"
                ),
                "detail": {
                    "sales": float(row["sales"]),
                    "cogs": float(row["cogs"]),
                    "ratio": float(ratio),
                    "mean_ratio": float(mean_ratio),
                }
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

    # 毎月ほぼ一定であるべき費用のみを対象とする。
    # 広告宣伝費はキャンペーンで変動が大きく、租税公課も月で変わるため対象外
    # （これらはYoY累計チェックで比較する）。
    target_accounts = ["給与", "外注費", "地代家賃"]

    for account in target_accounts:
        entries = df[
            df["debit_account"].astype(str).str.contains(account, na=False)
        ]

        if entries.empty:
            continue

        monthly = entries.groupby(df["date"].dt.to_period("M"))["debit_amount"].sum()

        if len(monthly) < 2:
            continue

        # 変動した月をまとめて1件に集約（科目ごとに何度も指摘しない）
        changes = []
        for i in range(1, len(monthly)):
            prev = monthly.iloc[i - 1]
            curr = monthly.iloc[i]
            month = monthly.index[i]
            if prev == 0:
                continue
            change_rate = (curr - prev) / prev * 100
            if abs(change_rate) > 50 and abs(curr - prev) > 50000:
                changes.append(f"{month}：{change_rate:+.0f}%（{prev:,.0f}→{curr:,.0f}円）")

        if changes:
            issues.append({
                "level": "warning", "category": "PL", "account": account,
                "month": "全期間",
                "message": (
                    f"【要確認】{account} に前月比±50%超の変動が {len(changes)}ヶ月 あります: "
                    + "、".join(changes[:12])
                    + "。原因を確認してください。"
                ),
            })

    return issues
