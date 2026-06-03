"""
貸借対照表（BS）チェックモジュール
期首残高対応版
"""
import pandas as pd
from typing import List, Dict, Any

CASH_ACCOUNTS = ["現金", "普通預金", "当座預金", "定期預金", "定期積金", "小口現金"]
TAX_TEMP_ACCOUNTS = ["仮払消費税", "仮受消費税"]
MANUFACTURING_PREFIX = "[製]"


def check_bs(df: pd.DataFrame, opening_balances: dict = None) -> List[Dict[str, Any]]:
    """BS項目のチェックを実行して指摘事項リストを返す"""
    ob = opening_balances or {}
    issues = []
    issues.extend(_check_cash_balance(df, ob))
    issues.extend(_check_tax_temp_accounts(df))
    issues.extend(_check_suspense_payments(df))
    issues.extend(_check_receivables_payables(df))
    issues.extend(_check_loan_repayment(df))
    return issues


def _monthly_net(df: pd.DataFrame, account_keyword: str) -> pd.Series:
    """特定科目の月次純増減（借方-貸方）を返す"""
    mask_d = df["debit_account"].astype(str).str.contains(account_keyword, na=False)
    mask_c = df["credit_account"].astype(str).str.contains(account_keyword, na=False)
    period = df["date"].dt.to_period("M")

    debit  = df[mask_d].groupby(period)["debit_amount"].sum()
    credit = df[mask_c].groupby(period)["credit_amount"].sum()
    return debit.subtract(credit, fill_value=0)


def _check_cash_balance(df: pd.DataFrame, ob: dict) -> List[Dict[str, Any]]:
    """現預金残高チェック：期首残高を加味してマイナスを検出"""
    issues = []

    # 補助科目単位でチェックするため、補助科目ごとに集計
    cash_mask = df["debit_account"].astype(str).apply(
        lambda x: any(a in x for a in CASH_ACCOUNTS)
    ) | df["credit_account"].astype(str).apply(
        lambda x: any(a in x for a in CASH_ACCOUNTS)
    )
    cash_df = df[cash_mask].copy()

    if cash_df.empty:
        return issues

    # 科目+補助科目でグループ化
    def get_key(row, side):
        acc = str(row[f"{side}_account"]).strip()
        sub = str(row.get(f"{side}_sub", "")).strip()
        return f"{acc}（{sub}）" if sub and sub not in ("", "nan") else acc

    period = cash_df["date"].dt.to_period("M")
    all_months = pd.period_range(
        start=cash_df["date"].dropna().min().to_period("M"),
        end=cash_df["date"].dropna().max().to_period("M"),
        freq="M"
    )

    # 補助科目別に処理
    for acc in CASH_ACCOUNTS:
        acc_d = cash_df[cash_df["debit_account"].astype(str).str.contains(acc, na=False)]
        acc_c = cash_df[cash_df["credit_account"].astype(str).str.contains(acc, na=False)]

        subs = set()
        if "debit_sub" in cash_df.columns:
            subs |= set(acc_d["debit_sub"].dropna().astype(str).unique())
        if "credit_sub" in cash_df.columns:
            subs |= set(acc_c["credit_sub"].dropna().astype(str).unique())
        if not subs:
            subs = {""}

        for sub in subs:
            label = f"{acc}（{sub}）" if sub and sub != "nan" else acc
            opening = ob.get(label, ob.get(acc, 0))

            # 月次純増減
            if sub and sub != "nan":
                d_sub = acc_d[acc_d.get("debit_sub", pd.Series(dtype=str)).astype(str) == sub]
                c_sub = acc_c[acc_c.get("credit_sub", pd.Series(dtype=str)).astype(str) == sub]
            else:
                d_sub = acc_d
                c_sub = acc_c

            monthly_d = d_sub.groupby(period)["debit_amount"].sum()
            monthly_c = c_sub.groupby(period)["credit_amount"].sum()
            net = monthly_d.subtract(monthly_c, fill_value=0)

            cumulative = net.cumsum() + opening

            neg = cumulative[cumulative < 0]
            if not neg.empty:
                for month, bal in neg.items():
                    issues.append({
                        "level": "error",
                        "category": "BS",
                        "account": label,
                        "month": str(month),
                        "message": (
                            f"【要確認】{label} の残高が {bal:,.0f}円 とマイナスになっています。"
                            "現預金残高はマイナスになり得ないため、仕訳漏れまたは誤入力の可能性があります。"
                        ),
                    })

    return issues


def _check_tax_temp_accounts(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """仮払消費税・仮受消費税：期首残高チェック"""
    issues = []
    for account in TAX_TEMP_ACCOUNTS:
        entries = df[
            df["debit_account"].astype(str).str.contains(account, na=False) |
            df["credit_account"].astype(str).str.contains(account, na=False)
        ]
        if entries.empty:
            continue

        first_month = df["date"].dropna().min()
        if pd.isna(first_month):
            continue

        first = entries[entries["date"].dt.month == first_month.month]
        d = first[first["debit_account"].astype(str).str.contains(account, na=False)]["debit_amount"].sum()
        c = first[first["credit_account"].astype(str).str.contains(account, na=False)]["credit_amount"].sum()

        if abs(d - c) > 0:
            issues.append({
                "level": "warning",
                "category": "BS",
                "account": account,
                "month": str(first_month.to_period("M")),
                "message": (
                    f"【要確認】{account} に期首残高（{d - c:,.0f}円）があります。"
                    "決算整理後はゼロになるべき科目のため、前期の処理誤りの可能性があります。"
                ),
            })
    return issues


def _check_suspense_payments(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """仮払金・前渡金チェック：長期滞留・法人税誤処理"""
    issues = []
    for account in ["仮払金", "前渡金"]:
        entries = df[
            df["debit_account"].astype(str).str.contains(account, na=False) |
            df["credit_account"].astype(str).str.contains(account, na=False)
        ]
        if entries.empty:
            continue

        d = entries[entries["debit_account"].astype(str).str.contains(account, na=False)]["debit_amount"].sum()
        c = entries[entries["credit_account"].astype(str).str.contains(account, na=False)]["credit_amount"].sum()
        balance = d - c

        if balance > 100000:
            issues.append({
                "level": "warning",
                "category": "BS",
                "account": account,
                "month": "全期間",
                "message": (
                    f"【要確認】{account} の残高が {balance:,.0f}円 あります。"
                    "内容不明のまま長期間放置されていないか確認してください。"
                    "法人税の支払いが仮払金で処理されている場合は「未払法人税等」の取崩し仕訳に修正が必要です。"
                ),
            })

        # 摘要に「法人税」を含む仮払金
        tax_entries = entries[
            entries.get("description", pd.Series(dtype=str)).astype(str).str.contains("法人税", na=False)
        ]
        if not tax_entries.empty:
            issues.append({
                "level": "error",
                "category": "BS",
                "account": account,
                "month": "全期間",
                "message": (
                    f"【要修正】摘要に「法人税」を含む{account}仕訳が {len(tax_entries)}件 あります。"
                    "「未払法人税等」を取り崩す仕訳に修正してください。"
                ),
            })

    return issues


def _check_receivables_payables(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """売掛金・買掛金・未払金・未収入金の残高確認"""
    issues = []
    targets = [
        ("売掛金", "debit"),
        ("買掛金", "credit"),
        ("未払金", "credit"),
        ("未払費用", "credit"),
        ("未収入金", "debit"),
        ("立替金", "debit"),
    ]

    for account, normal_side in targets:
        d = df[df["debit_account"].astype(str).str.contains(account, na=False)]["debit_amount"].sum()
        c = df[df["credit_account"].astype(str).str.contains(account, na=False)]["credit_amount"].sum()
        balance = d - c

        if normal_side == "credit":
            balance = -balance  # 負債は貸方残高が正常

        if abs(balance) > 0:
            issues.append({
                "level": "info",
                "category": "BS",
                "account": account,
                "month": "全期間",
                "message": (
                    f"【確認】{account} の期末残高は {balance:,.0f}円 です。"
                    "補助元帳を確認し、未回収・未払の内訳と金額が正しいか検証してください。"
                    "分割回収や振込手数料差引による残高ズレがないか確認が必要です。"
                ),
            })

    return issues


def _check_loan_repayment(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """借入金チェック：短期・長期の残高確認"""
    issues = []

    for account in ["短期借入金", "長期借入金"]:
        d = df[df["debit_account"].astype(str).str.contains(account, na=False)]["debit_amount"].sum()
        c = df[df["credit_account"].astype(str).str.contains(account, na=False)]["credit_amount"].sum()
        balance = c - d  # 負債なので貸方が増加

        if balance > 0:
            issues.append({
                "level": "info",
                "category": "BS",
                "account": account,
                "month": "全期間",
                "message": (
                    f"【確認】{account} の残高が {balance:,.0f}円 あります。"
                    "金融機関の返済予定表と残高を照合してください。"
                    "長期借入金のうち1年以内返済分が短期借入金に振り替えられているかも確認が必要です。"
                ),
            })

    return issues
