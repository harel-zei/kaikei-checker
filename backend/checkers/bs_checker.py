"""
貸借対照表（BS）チェックモジュール
- 期首残高対応（補助科目単位）
- 期首残高未提供時はスキップ（誤検知防止）
"""
import pandas as pd
from typing import List, Dict, Any

CASH_ACCOUNTS   = ["現金", "小口現金", "普通預金", "当座預金", "定期預金", "定期積金"]
TAX_TEMP        = ["仮払消費税", "仮受消費税"]


def check_bs(df: pd.DataFrame, opening_balances: dict = None) -> List[Dict[str, Any]]:
    ob = opening_balances or {}
    issues = []
    issues.extend(_check_cash_balance(df, ob))
    issues.extend(_check_tax_temp_accounts(df))
    issues.extend(_check_suspense_payments(df))
    issues.extend(_check_receivables_payables(df))
    issues.extend(_check_loan_repayment(df))
    return issues


# ────────────────────────────────────────────────
# 現預金残高チェック
# ────────────────────────────────────────────────
def _check_cash_balance(df: pd.DataFrame, ob: dict) -> List[Dict[str, Any]]:
    """
    補助科目（口座）単位で月次残高を計算しマイナスを検出する。

    残高 = 期首残高 + 当期借方合計累計 - 当期貸方合計累計

    ※ 期首残高が提供されていない科目・補助科目はチェックをスキップし、
       「期首残高未提供」のINFOを1件出す。
    """
    issues = []
    skipped = []

    for base_acc in CASH_ACCOUNTS:
        # 当科目に関係する行を抽出
        d_rows = df[df["debit_account"].astype(str).str.contains(base_acc, na=False)]
        c_rows = df[df["credit_account"].astype(str).str.contains(base_acc, na=False)]

        if d_rows.empty and c_rows.empty:
            continue

        # 補助科目の一覧を収集
        subs = set()
        if "debit_sub" in df.columns:
            subs |= set(d_rows["debit_sub"].dropna().astype(str).str.strip().unique())
        if "credit_sub" in df.columns:
            subs |= set(c_rows["credit_sub"].dropna().astype(str).str.strip().unique())
        subs.discard("")
        subs.discard("nan")

        # 補助科目がない場合は科目全体を1つとして扱う
        targets = [(base_acc, s) for s in subs] if subs else [(base_acc, None)]

        for (acc, sub) in targets:
            label = f"{acc}（{sub}）" if sub else acc

            # 期首残高を取得
            opening = ob.get(label) if ob.get(label) is not None else ob.get(acc)
            if opening is None:
                skipped.append(label)
                continue

            # 補助科目でフィルタ
            if sub:
                d_sub = d_rows[d_rows.get("debit_sub",  pd.Series(dtype=str)).astype(str).str.strip() == sub]
                c_sub = c_rows[c_rows.get("credit_sub", pd.Series(dtype=str)).astype(str).str.strip() == sub]
            else:
                d_sub = d_rows
                c_sub = c_rows

            period = df["date"].dt.to_period("M")
            monthly_d = d_sub.groupby(period.loc[d_sub.index])["debit_amount"].sum()
            monthly_c = c_sub.groupby(period.loc[c_sub.index])["credit_amount"].sum()

            # 月次純増減（借方増・貸方減）
            net = monthly_d.subtract(monthly_c, fill_value=0)
            # 累積残高 = 期首 + 当期純増減の累計
            cumulative = net.cumsum() + opening

            neg = cumulative[cumulative < 0]
            for month, bal in neg.items():
                issues.append({
                    "level":    "error",
                    "category": "BS",
                    "account":  label,
                    "month":    str(month),
                    "message": (
                        f"【要確認】{label} の残高が {bal:,.0f}円 とマイナスになっています"
                        f"（期首{opening:,.0f}円 + 当期増減）。"
                        "仕訳漏れまたは誤入力の可能性があります。"
                    ),
                })

    if skipped:
        issues.append({
            "level":    "info",
            "category": "BS",
            "account":  "現預金",
            "month":    "全期間",
            "message": (
                f"【期首残高未提供】{', '.join(skipped)} について期首残高が提供されていないため、"
                "マイナス残高チェックをスキップしました。"
                "試算表CSVまたは補助残高CSVをアップロードすると正確にチェックできます。"
            ),
        })

    return issues


# ────────────────────────────────────────────────
# 仮払消費税・仮受消費税
# ────────────────────────────────────────────────
def _check_tax_temp_accounts(df: pd.DataFrame) -> List[Dict[str, Any]]:
    issues = []
    for account in TAX_TEMP:
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
                "level": "warning", "category": "BS", "account": account,
                "month": str(first_month.to_period("M")),
                "message": (
                    f"【要確認】{account} に期首残高（{d - c:,.0f}円）があります。"
                    "決算整理後はゼロになるべき科目のため、前期の処理誤りの可能性があります。"
                ),
            })
    return issues


# ────────────────────────────────────────────────
# 仮払金・前渡金
# ────────────────────────────────────────────────
def _check_suspense_payments(df: pd.DataFrame) -> List[Dict[str, Any]]:
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
                "level": "warning", "category": "BS", "account": account, "month": "全期間",
                "message": (
                    f"【要確認】{account} の残高が {balance:,.0f}円 あります。"
                    "内容不明のまま長期間放置されていないか確認してください。"
                    "法人税の支払いが仮払金で処理されている場合は「未払法人税等」の取崩し仕訳に修正が必要です。"
                ),
            })
        tax_ent = entries[
            entries.get("description", pd.Series(dtype=str)).astype(str).str.contains("法人税", na=False)
        ]
        if not tax_ent.empty:
            issues.append({
                "level": "error", "category": "BS", "account": account, "month": "全期間",
                "message": (
                    f"【要修正】摘要に「法人税」を含む{account}仕訳が {len(tax_ent)}件 あります。"
                    "「未払法人税等」を取り崩す仕訳に修正してください。"
                ),
            })
    return issues


# ────────────────────────────────────────────────
# 売掛金・買掛金・未払金など
# ────────────────────────────────────────────────
def _check_receivables_payables(df: pd.DataFrame) -> List[Dict[str, Any]]:
    issues = []
    targets = [
        ("売掛金", "debit"), ("買掛金", "credit"), ("未払金", "credit"),
        ("未払費用", "credit"), ("未収入金", "debit"), ("立替金", "debit"),
    ]
    for account, normal_side in targets:
        d = df[df["debit_account"].astype(str).str.contains(account, na=False)]["debit_amount"].sum()
        c = df[df["credit_account"].astype(str).str.contains(account, na=False)]["credit_amount"].sum()
        balance = (d - c) if normal_side == "debit" else (c - d)
        if abs(balance) > 0:
            issues.append({
                "level": "info", "category": "BS", "account": account, "month": "全期間",
                "message": (
                    f"【確認】{account} の期末残高は {balance:,.0f}円 です。"
                    "補助元帳を確認し、未回収・未払の内訳と金額が正しいか検証してください。"
                ),
            })
    return issues


# ────────────────────────────────────────────────
# 借入金
# ────────────────────────────────────────────────
def _check_loan_repayment(df: pd.DataFrame) -> List[Dict[str, Any]]:
    issues = []
    for account in ["短期借入金", "長期借入金"]:
        d = df[df["debit_account"].astype(str).str.contains(account, na=False)]["debit_amount"].sum()
        c = df[df["credit_account"].astype(str).str.contains(account, na=False)]["credit_amount"].sum()
        balance = c - d
        if balance > 0:
            issues.append({
                "level": "info", "category": "BS", "account": account, "month": "全期間",
                "message": (
                    f"【確認】{account} の残高が {balance:,.0f}円 あります。"
                    "金融機関の返済予定表と残高を照合してください。"
                    "長期借入金のうち1年以内返済分が短期借入金に振り替えられているかも確認が必要です。"
                ),
            })
    return issues
