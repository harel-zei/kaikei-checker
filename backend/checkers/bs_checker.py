"""
貸借対照表（BS）チェックモジュール
- 期首残高対応（補助科目単位）
- 売掛金・買掛金を補助科目（取引先）単位でチェック
- 期首残高未提供時はスキップ（誤検知防止）
"""
import pandas as pd
from typing import List, Dict, Any

CASH_ACCOUNTS = ["現金", "小口現金", "普通預金", "当座預金", "定期預金", "定期積金"]
TAX_TEMP      = ["仮払消費税", "仮受消費税"]

# 補助科目単位でチェックする売掛・買掛系科目
RECEIVABLE_ACCOUNTS = ["売掛金", "電子記録債権", "未収入金"]
PAYABLE_ACCOUNTS    = ["買掛金", "未払金", "未払費用"]


def check_bs(df: pd.DataFrame, opening_balances: dict = None) -> List[Dict[str, Any]]:
    ob = opening_balances or {}
    issues = []
    issues.extend(_check_cash_balance(df, ob))
    issues.extend(_check_receivables_by_sub(df, ob, RECEIVABLE_ACCOUNTS, "debit"))
    issues.extend(_check_receivables_by_sub(df, ob, PAYABLE_ACCOUNTS,    "credit"))
    issues.extend(_check_tax_temp_accounts(df))
    issues.extend(_check_suspense_payments(df))
    issues.extend(_check_loan_repayment(df))
    return issues


# ────────────────────────────────────────────────
# 現預金残高チェック（補助科目＝口座単位）
# ────────────────────────────────────────────────
def _check_cash_balance(df: pd.DataFrame, ob: dict) -> List[Dict[str, Any]]:
    """
    口座（補助科目）単位で月次残高を計算しマイナスを検出する。
    残高 = 期首残高 + 当期借方累計 - 当期貸方累計
    期首残高が未提供の口座はスキップしてINFOを出す。
    """
    issues = []
    skipped = []

    for base_acc in CASH_ACCOUNTS:
        d_rows = df[df["debit_account"].astype(str).str.contains(base_acc, na=False)]
        c_rows = df[df["credit_account"].astype(str).str.contains(base_acc, na=False)]
        if d_rows.empty and c_rows.empty:
            continue

        subs = _collect_subs(d_rows, c_rows, df)
        targets = [(base_acc, s) for s in subs] if subs else [(base_acc, None)]

        for (acc, sub) in targets:
            label   = f"{acc}（{sub}）" if sub else acc
            opening = ob.get(label) if ob.get(label) is not None else ob.get(acc)

            if opening is None:
                skipped.append(label)
                continue

            d_sub, c_sub = _filter_sub(d_rows, c_rows, sub)
            period = df["date"].dt.to_period("M")
            monthly_d = d_sub.groupby(period.loc[d_sub.index])["debit_amount"].sum()
            monthly_c = c_sub.groupby(period.loc[c_sub.index])["credit_amount"].sum()
            net        = monthly_d.subtract(monthly_c, fill_value=0)
            cumulative = net.cumsum() + opening

            for month, bal in cumulative[cumulative < 0].items():
                issues.append({
                    "level": "error", "category": "BS", "account": label, "month": str(month),
                    "message": (
                        f"【要確認】{label} の残高が {bal:,.0f}円 とマイナスになっています"
                        f"（期首{opening:,.0f}円 + 当期増減）。"
                        "仕訳漏れまたは誤入力の可能性があります。"
                    ),
                })

    if skipped:
        issues.append({
            "level": "info", "category": "BS", "account": "現預金", "month": "全期間",
            "message": (
                f"【期首残高未提供】{', '.join(skipped)} の期首残高が見つかりませんでした。"
                "期首残高CSVまたは期首補助残高CSVをアップロードすると正確にチェックできます。"
            ),
        })
    return issues


# ────────────────────────────────────────────────
# 売掛金・買掛金を補助科目（取引先）単位でチェック
# ────────────────────────────────────────────────
def _check_receivables_by_sub(
    df: pd.DataFrame,
    ob: dict,
    accounts: List[str],
    normal_side: str,   # "debit"=売掛金系, "credit"=買掛金系
) -> List[Dict[str, Any]]:
    """
    取引先（補助科目）単位で期末残高を計算し、異常な残高を指摘する。

    チェック内容:
    ① 期首残高あり → 残高が計算できる（マイナスも検出）
    ② 期首残高なし → 当期増減のみ（マイナスの場合は誤処理の可能性として報告）
    ③ 残高がゼロのはずなのに残高がある（長期未消込）
    """
    issues = []

    for base_acc in accounts:
        d_rows = df[df["debit_account"].astype(str).str.contains(base_acc, na=False)]
        c_rows = df[df["credit_account"].astype(str).str.contains(base_acc, na=False)]
        if d_rows.empty and c_rows.empty:
            continue

        subs = _collect_subs(d_rows, c_rows, df)

        # 補助科目がない場合は科目全体のみ
        if not subs:
            _check_single_account(issues, df, base_acc, None, ob, normal_side)
            continue

        for sub in subs:
            _check_single_account(issues, df, base_acc, sub, ob, normal_side)

    return issues


def _check_single_account(
    issues: list,
    df: pd.DataFrame,
    base_acc: str,
    sub: str | None,
    ob: dict,
    normal_side: str,
) -> None:
    """
    1つの科目（補助科目）の月次残高を計算して異常を検出する。

    フラグを立てる条件:
    ① マイナス残高 → Error（消込超過・誤入力）
    ② 2ヶ月以上残高が滞留している → Warning（長期未回収・未払）
       ※「翌月回収」のような正常サイクルは除外する
    残高0、または毎月回収できている場合はフラグなし。
    """
    label   = f"{base_acc}（{sub}）" if sub else base_acc
    opening = ob.get(label) if ob.get(label) is not None else ob.get(base_acc)
    if opening is None:
        opening = 0.0

    d_rows = df[df["debit_account"].astype(str).str.contains(base_acc, na=False)]
    c_rows = df[df["credit_account"].astype(str).str.contains(base_acc, na=False)]
    d_sub, c_sub = _filter_sub(d_rows, c_rows, sub)

    if d_sub.empty and c_sub.empty:
        return

    # 月次残高を計算
    period = df["date"].dt.to_period("M")
    monthly_d = d_sub.groupby(period.loc[d_sub.index])["debit_amount"].sum()
    monthly_c = c_sub.groupby(period.loc[c_sub.index])["credit_amount"].sum()

    all_periods = monthly_d.index.union(monthly_c.index).sort_values()
    if all_periods.empty:
        return

    # 月末残高を積み上げ
    monthly_bal = {}
    running = opening
    for p in all_periods:
        running += monthly_d.get(p, 0) - monthly_c.get(p, 0) if normal_side == "debit" \
              else monthly_c.get(p, 0) - monthly_d.get(p, 0)
        monthly_bal[p] = running

    final_balance = list(monthly_bal.values())[-1]

    # ① マイナス残高（消込超過・誤入力）
    if final_balance < -1000:
        issues.append({
            "level": "error", "category": "BS", "account": label,
            "month": str(list(monthly_bal.keys())[-1]),
            "message": (
                f"【要修正】{label} の残高が {final_balance:,.0f}円 とマイナスになっています。"
                "消込超過または仕訳の誤入力の可能性があります。補助元帳を確認してください。"
            ),
        })
        return

    # ② 滞留チェック：2ヶ月以上連続して残高が減少していない月を検出
    stale_months = _detect_stale_balance(monthly_bal, threshold=1000, stale_months=2)
    for stale_month, stale_bal in stale_months:
        issues.append({
            "level": "warning", "category": "BS", "account": label,
            "month": str(stale_month),
            "message": (
                f"【要確認】{label} の残高 {stale_bal:,.0f}円 が"
                f" {stale_month} 時点で2ヶ月以上動いていません。"
                "回収遅延または未払の可能性があります。補助元帳を確認してください。"
            ),
        })


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


# ────────────────────────────────────────────────
# ユーティリティ
# ────────────────────────────────────────────────
def _detect_stale_balance(
    monthly_bal: dict,
    threshold: float = 1000,
    stale_months: int = 2,
) -> list:
    """
    月次残高辞書を受け取り、「N ヶ月以上残高が減少していない」月を返す。

    正常サイクル（翌月回収）の例:
      12月: 175,384 → 1月: 0 → 1月: 296,054 → 2月: 0  ← フラグなし

    滞留の例:
      12月: 500,000 → 1月: 500,000 → 2月: 500,000  ← フラグ

    Returns: [(period, balance), ...]  最初に滞留が確認された月のみ返す
    """
    items   = list(monthly_bal.items())
    flagged = []
    consecutive = 0
    prev_bal = None
    flagged_start = None

    for period, bal in items:
        if bal <= threshold:
            # 残高がほぼ0になった → リセット
            consecutive = 0
            prev_bal    = None
            flagged_start = None
            continue

        if prev_bal is not None and bal >= prev_bal * 0.95:
            # 前月より残高がほとんど減っていない（5%未満の減少は誤差として許容しない）
            consecutive += 1
            if consecutive >= stale_months and flagged_start is None:
                flagged_start = period
                flagged.append((period, bal))
        else:
            # 残高が減少した → リセット
            consecutive   = 0
            flagged_start = None

        prev_bal = bal

    return flagged


def _collect_subs(d_rows: pd.DataFrame, c_rows: pd.DataFrame, df: pd.DataFrame) -> set:
    """借方・貸方の行から補助科目の一覧を収集する"""
    subs = set()
    for col, rows in [("debit_sub", d_rows), ("credit_sub", c_rows)]:
        if col in df.columns and not rows.empty:
            subs |= set(rows[col].dropna().astype(str).str.strip().unique())
    subs.discard("")
    subs.discard("nan")
    subs.discard("指定なし")
    return subs


def _filter_sub(d_rows: pd.DataFrame, c_rows: pd.DataFrame, sub: str | None):
    """補助科目でフィルタした借方・貸方の行を返す"""
    if sub:
        d_sub = d_rows[d_rows.get("debit_sub",  pd.Series(dtype=str)).astype(str).str.strip() == sub]
        c_sub = c_rows[c_rows.get("credit_sub", pd.Series(dtype=str)).astype(str).str.strip() == sub]
    else:
        d_sub, c_sub = d_rows, c_rows
    return d_sub, c_sub
