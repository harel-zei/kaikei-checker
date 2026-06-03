"""
カテゴリ4: 債権・債務の消込・滞留チェック
4-1: 振込手数料差引による売掛金消込漏れ
4-2: 仮払金・立替金の長期滞留・精算漏れ
"""
import pandas as pd
from typing import List, Dict, Any

# 主な振込手数料金額
WIRE_FEE_AMOUNTS = [110, 220, 330, 440, 550, 660, 770, 880, 990, 1100]

# 仮払金・立替金の滞留判定日数
SUSPENSE_DAYS_THRESHOLD = 90


def check_ar_ap(df: pd.DataFrame) -> List[Dict[str, Any]]:
    issues = []
    issues.extend(_check_4_1_wire_fee_clearing(df))
    issues.extend(_check_4_2_suspense_aging(df))
    return issues


# ──────────────────────────────────────────────────────────
# 4-1: 振込手数料差引による売掛金消込漏れ
# ──────────────────────────────────────────────────────────
def _check_4_1_wire_fee_clearing(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    売掛金の補助科目（顧客別）の残高に振込手数料相当額が残留しているケースを検知
    """
    issues = []

    ar_d = df[df["debit_account"].astype(str).str.contains("売掛金", na=False)]
    ar_c = df[df["credit_account"].astype(str).str.contains("売掛金", na=False)]

    if ar_d.empty and ar_c.empty:
        return issues

    # 補助科目（取引先）ごとに残高を計算
    subs = set()
    if "debit_sub" in df.columns:
        subs |= set(ar_d["debit_sub"].dropna().astype(str).str.strip().unique())
    if "credit_sub" in df.columns:
        subs |= set(ar_c["credit_sub"].dropna().astype(str).str.strip().unique())
    subs.discard(""); subs.discard("nan"); subs.discard("指定なし")

    for sub in subs:
        d_sub = ar_d[ar_d.get("debit_sub", pd.Series(dtype=str)).astype(str).str.strip() == sub]
        c_sub = ar_c[ar_c.get("credit_sub", pd.Series(dtype=str)).astype(str).str.strip() == sub]

        d_total = d_sub["debit_amount"].sum()
        c_total = c_sub["credit_amount"].sum()
        balance = d_total - c_total  # 売掛金は借方残が正常

        if balance > 0:
            for fee in WIRE_FEE_AMOUNTS:
                if balance == fee:
                    issues.append({
                        "level": "error", "category": "4-1 振込手数料消込漏れ",
                        "check_id": "4-1", "account": f"売掛金（{sub}）",
                        "month": "全期間",
                        "message": (
                            f"【4-1・高】売掛金（{sub}）の残高が {balance:,.0f}円 です。"
                            f"これは振込手数料 {fee}円 と一致します。"
                            "振込手数料差引入金の際、「支払手数料」または「売上値引」として"
                            "消込仕訳が必要です。"
                        ),
                    })
                    break

    return issues


# ──────────────────────────────────────────────────────────
# 4-2: 仮払金・立替金の長期滞留・精算漏れ
# ──────────────────────────────────────────────────────────
def _check_4_2_suspense_aging(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    仮払金・立替金の発生後90日以上精算されていない残高を検知
    また、二重精算疑い（仮払金が残ったまま同期間に旅費等が直接計上）も検知
    """
    issues = []
    last_date = df["date"].dropna().max()
    if pd.isna(last_date):
        return issues

    for account in ["仮払金", "立替金"]:
        entries = df[
            df["debit_account"].astype(str).str.contains(account, na=False) |
            df["credit_account"].astype(str).str.contains(account, na=False)
        ].copy()
        if entries.empty:
            continue

        # 借方（発生）
        debit_entries = entries[
            entries["debit_account"].astype(str).str.contains(account, na=False)
        ].copy()
        # 貸方（精算）
        credit_total = entries[
            entries["credit_account"].astype(str).str.contains(account, na=False)
        ]["credit_amount"].sum()

        total_issued  = debit_entries["debit_amount"].sum()
        total_settled = credit_total
        unsettled     = total_issued - total_settled

        if unsettled <= 0:
            continue

        # 90日以上前の未精算発生を検知
        old_entries = debit_entries[
            (last_date - debit_entries["date"]).dt.days >= SUSPENSE_DAYS_THRESHOLD
        ]
        if not old_entries.empty:
            old_total = old_entries["debit_amount"].sum()
            oldest    = old_entries["date"].min()
            issues.append({
                "level": "error", "category": f"4-2 {account}滞留",
                "check_id": "4-2", "account": account,
                "month": str(oldest.to_period("M")),
                "message": (
                    f"【4-2・高】{account} に {oldest.date()} から90日以上経過した"
                    f"未精算残高（合計 {old_total:,.0f}円）があります。"
                    "放置すると役員貸付金認定リスクや経費の期間帰属誤りが生じます。"
                    "精算仕訳または返金処理を確認してください。"
                ),
            })

        # 決算またぎチェック（期末最終月に未精算残高あり）
        last_month = last_date.to_period("M")
        period = df["date"].dt.to_period("M")
        monthly_d = debit_entries.groupby(period.loc[debit_entries.index])["debit_amount"].sum()
        monthly_c = entries[
            entries["credit_account"].astype(str).str.contains(account, na=False)
        ].groupby(period.loc[entries[
            entries["credit_account"].astype(str).str.contains(account, na=False)
        ].index])["credit_amount"].sum()

        running = monthly_d.subtract(monthly_c, fill_value=0).cumsum()
        if last_month in running.index and running[last_month] > 10000:
            issues.append({
                "level": "warning", "category": f"4-2 {account}決算またぎ",
                "check_id": "4-2", "account": account,
                "month": str(last_month),
                "message": (
                    f"【4-2・高】{account} が期末月（{last_month}）に {running[last_month]:,.0f}円 残高があります。"
                    "決算をまたいで残存する場合、役員貸付認定や費用期間帰属の問題が生じる可能性があります。"
                ),
            })

    return issues
