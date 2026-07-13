"""
カテゴリ4: 債権・債務の消込・滞留チェック
4-1: 振込手数料差引による売掛金消込漏れ
4-2: 仮払金・立替金の長期滞留・精算漏れ
"""
import pandas as pd
from typing import List, Dict, Any
from checkers.check_utils import date_safe

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
# 4-2: 仮払金・立替金の長期滞留・精算漏れ（補助科目・件別）
# ──────────────────────────────────────────────────────────
def _check_4_2_suspense_aging(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    仮払金・立替金を補助科目（または案件）ごとに追跡し、
    発生後90日以上精算されていない個々の取引を指摘する。
    合計額ではなく1件1件を個別に報告する。
    """
    issues = []
    last_date = df["date"].dropna().max()
    if pd.isna(last_date):
        return issues

    for account in ["仮払金", "立替金"]:
        all_entries = df[
            df["debit_account"].astype(str).str.contains(account, na=False) |
            df["credit_account"].astype(str).str.contains(account, na=False)
        ].copy()
        if all_entries.empty:
            continue

        debit_rows  = all_entries[all_entries["debit_account"].astype(str).str.contains(account, na=False)].copy()
        credit_rows = all_entries[all_entries["credit_account"].astype(str).str.contains(account, na=False)].copy()

        # 補助科目の一覧を収集
        subs = set()
        if "debit_sub" in df.columns:
            subs |= set(debit_rows["debit_sub"].dropna().astype(str).str.strip().unique())
        subs.discard(""); subs.discard("nan")

        # 補助科目がない場合は全体を1グループとして扱う
        groups = [(s, True) for s in subs] if subs else [(None, False)]

        for (sub, has_sub) in groups:
            if has_sub:
                d_sub = debit_rows[debit_rows.get("debit_sub", pd.Series(dtype=str)).astype(str).str.strip() == sub]
                c_sub = credit_rows[credit_rows.get("credit_sub", pd.Series(dtype=str)).astype(str).str.strip() == sub]
                label = f"{account}（{sub}）"
            else:
                d_sub = debit_rows
                c_sub = credit_rows
                label = account

            if d_sub.empty:
                continue

            # 累積精算額（貸方合計）
            settled_total = c_sub["credit_amount"].sum()
            issued_total  = d_sub["debit_amount"].sum()
            if issued_total <= settled_total:
                continue  # 精算済み

            # 未精算の発生仕訳を古い順に積み上げ、未精算分を特定
            remaining = issued_total - settled_total
            d_sorted = d_sub.sort_values("date")
            covered  = 0.0

            for _, row in d_sorted.iterrows():
                amt = row["debit_amount"]
                if covered + amt <= settled_total:
                    covered += amt
                    continue  # この行は精算済み

                # この発生は未精算（全部または一部）
                days_elapsed = (last_date - row["date"]).days if pd.notna(row["date"]) else 0
                if days_elapsed < SUSPENSE_DAYS_THRESHOLD:
                    continue  # 90日未満はまだ許容

                desc = str(row.get("description", "")).strip()
                desc_clean = "" if desc.lower() in ("nan","none","") else desc
                desc_part  = f"（摘要: {desc_clean[:30]}）" if desc_clean else ""

                issues.append({
                    "level": "error",
                    "category": f"4-2 {account}滞留",
                    "check_id": "4-2",
                    "account": label,
                    "month": date_safe(row), "slip": slip_safe(row),
                    "message": (
                        f"【4-2・高】{label} に {row['date'].date()} 発生の"
                        f" {amt:,.0f}円 が{days_elapsed}日経過しても未精算です{desc_part}。"
                        "精算仕訳または返金処理を確認してください。"
                    ),
                })
                covered += amt  # 残りは次のループへ

        # 注: 決算またぎチェックは個別指摘の中で90日超判定により実質カバー済み

    return issues
