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


def estimate_last_complete_month(df: pd.DataFrame) -> "pd.Period":
    """
    仕訳データから「最終会計入力月」を推定する。

    手法:
    1. 月ごとのユニーク勘定科目数を集計
    2. 中央値の50%以上の活動がある月を「入力済み月」とみなす
       （決算整理仕訳など少数の先行入力月は除外）
    3. そのうち最後の月を返す

    Returns: pd.Period（例: 2026-04）
    """
    period = df["date"].dt.to_period("M")
    # 月ごとのユニーク科目数（借方・貸方合計）
    def count_accounts(x):
        d = set(x["debit_account"].dropna().astype(str).str.strip())
        c = set(x["credit_account"].dropna().astype(str).str.strip())
        return len((d | c) - {""})

    monthly_counts = df.groupby(period).apply(count_accounts).sort_index()
    if monthly_counts.empty:
        return df["date"].dropna().dt.to_period("M").max()

    median = monthly_counts.median()
    threshold = median * 0.5
    complete = monthly_counts[monthly_counts >= threshold]
    return complete.index[-1] if not complete.empty else monthly_counts.index[-1]


def check_bs(
    df: pd.DataFrame,
    opening_balances: dict = None,
    exclude_accounts: list = None,
) -> List[Dict[str, Any]]:
    ob = opening_balances or {}
    excl = exclude_accounts or []
    last_month = estimate_last_complete_month(df)  # 最終会計入力月を推定
    issues = []
    issues.extend(_check_cash_balance(df, ob))
    # 除外科目を RECEIVABLE / PAYABLE リストから取り除く
    recv = [a for a in RECEIVABLE_ACCOUNTS if not any(e in a for e in excl)]
    pabl = [a for a in PAYABLE_ACCOUNTS    if not any(e in a for e in excl)]
    issues.extend(_check_receivables_by_sub(df, ob, recv, "debit",  last_month))
    issues.extend(_check_receivables_by_sub(df, ob, pabl, "credit", last_month))
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
            # 補助科目がある場合は専用残高のみ使用（合計値を誤適用しない）
            if sub:
                opening = ob.get(label)          # 例: 普通預金（永和信用金庫・梅田）
            else:
                opening = ob.get(acc)            # 補助科目なしの場合のみ科目合計を使用

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
    normal_side: str,
    last_month: "pd.Period",
) -> List[Dict[str, Any]]:
    issues = []
    for base_acc in accounts:
        d_rows = df[df["debit_account"].astype(str).str.contains(base_acc, na=False)]
        c_rows = df[df["credit_account"].astype(str).str.contains(base_acc, na=False)]
        if d_rows.empty and c_rows.empty:
            continue
        subs = _collect_subs(d_rows, c_rows, df)
        if not subs:
            _check_single_account(issues, df, base_acc, None, ob, normal_side, last_month)
            continue
        for sub in subs:
            _check_single_account(issues, df, base_acc, sub, ob, normal_side, last_month)
    return issues


def _check_single_account(
    issues: list,
    df: pd.DataFrame,
    base_acc: str,
    sub: str | None,
    ob: dict,
    normal_side: str,
    last_month: "pd.Period",
) -> None:
    """
    1つの科目（補助科目）の月次残高を計算して異常を検出する。

    フラグを立てる条件:
    ① マイナス残高 → Error（消込超過・誤入力）
    ② 最終入力月時点で2ヶ月以上「その取引先への取引が全くない」状態が続き、
       かつ残高が残っている → Warning（長期滞留）

    除外するパターン:
    - 翌月回収（発生→翌月ゼロ）は正常サイクルとして除外
    - 最終入力月の残高は「まだ回収期限未到来」として除外
    """
    label = f"{base_acc}（{sub}）" if sub else base_acc

    if sub:
        # 補助科目がある場合 → その補助科目専用の期首残高のみ使用
        # 科目合計（ob.get(base_acc)）は絶対に使わない
        opening = ob.get(label)
        opening_missing = (opening is None)
        opening = opening or 0.0
    else:
        opening = ob.get(base_acc, 0.0)
        opening_missing = (base_acc not in ob)

    d_rows = df[df["debit_account"].astype(str).str.contains(base_acc, na=False)]
    c_rows = df[df["credit_account"].astype(str).str.contains(base_acc, na=False)]
    d_sub, c_sub = _filter_sub(d_rows, c_rows, sub)

    if d_sub.empty and c_sub.empty:
        return

    period = df["date"].dt.to_period("M")

    # 月次借方・貸方を集計
    monthly_d = d_sub.groupby(period.loc[d_sub.index])["debit_amount"].sum()
    monthly_c = c_sub.groupby(period.loc[c_sub.index])["credit_amount"].sum()

    all_periods = monthly_d.index.union(monthly_c.index).sort_values()
    if all_periods.empty:
        return

    # ── 月末残高と「その月に取引があったか」を積み上げ ──
    monthly_bal      = {}
    monthly_activity = {}  # True = その月に借方or貸方の取引あり
    running = opening

    for p in all_periods:
        d = monthly_d.get(p, 0)
        c = monthly_c.get(p, 0)
        if normal_side == "debit":
            running += d - c
        else:
            running += c - d
        monthly_bal[p]      = running
        monthly_activity[p] = (d > 0 or c > 0)

    # last_month 以降は判定しない（入力がまだの期間）
    # ただし last_month 自体は含める
    check_bal = {p: v for p, v in monthly_bal.items() if p <= last_month}
    if not check_bal:
        return

    final_balance = list(check_bal.values())[-1]

    # ── 期首残高が未提供で、かつ最初の月に貸方が多い場合 ──
    # → 「期首残高が0扱いになっているため不正確な可能性」をINFOで通知
    if opening_missing and sub:
        first_period = list(all_periods)[0]
        first_d = monthly_d.get(first_period, 0)
        first_c = monthly_c.get(first_period, 0)
        # 最初の月に貸方（回収/支払）が大きい → 期首残高があった可能性が高い
        if (normal_side == "debit"  and first_c > first_d + 1000) or \
           (normal_side == "credit" and first_d > first_c + 1000):
            issues.append({
                "level": "info", "category": "BS", "account": label, "month": str(first_period),
                "message": (
                    f"【期首残高未提供】{label} の期首残高が提供されていないため0円として計算しています。"
                    f"最初の月に回収・支払が発生していることから、期首時点に残高があった可能性があります。"
                    "「当期首補助残高CSV」をアップロードすると正確にチェックできます。"
                ),
            })
        return  # 期首残高なしでマイナス/滞留の誤検知を防ぐため、以降の判定をスキップ

    # ① マイナス残高
    if final_balance < -1000:
        # 科目に応じてメッセージを変える
        if "買掛金" in base_acc or "未払金" in base_acc or "未払費用" in base_acc:
            extra = (
                "支払超過または消込誤りの可能性があります。"
                "また、仕入・費用の計上漏れにより残高がマイナスになっているケースもありますので、"
                "仕入先への請求書・納品書と照合してください。"
            )
        else:
            extra = "回収超過または消込誤りの可能性があります。補助元帳を確認してください。"
        issues.append({
            "level": "error", "category": "BS", "account": label,
            "month": str(last_month),
            "message": (
                f"【要修正】{label} の {last_month} 時点の残高が {final_balance:,.0f}円 とマイナスです。"
                + extra
            ),
        })
        return

    # ② 滞留チェック：取引が全くない月が2ヶ月以上続いて残高が残っている
    stale = _detect_stale_by_activity(check_bal, monthly_activity, last_month,
                                      threshold=1000, stale_count=2)
    for stale_month, stale_bal in stale:
        issues.append({
            "level": "warning", "category": "BS", "account": label,
            "month": str(stale_month),
            "message": (
                f"【要確認】{label} の残高 {stale_bal:,.0f}円 が"
                f" {stale_month} まで2ヶ月以上取引がなく滞留しています。"
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
def _detect_stale_by_activity(
    monthly_bal:      dict,
    monthly_activity: dict,
    last_month:       "pd.Period",
    threshold:        float = 1000,
    stale_count:      int   = 2,
) -> list:
    """
    「取引が全くない月が N ヶ月以上続いて残高が残っている」を検出する。

    判定ロジック:
    - 残高がゼロになった月 → カウンターリセット（正常回収）
    - 取引あり（借方 or 貸方 > 0）の月 → カウンターリセット（活動中）
    - 取引ゼロ かつ 残高あり の月 → 滞留カウンター +1
    - 滞留カウンター >= stale_count の月をフラグ
    - last_month 自体の残高は「まだ回収期限未到来」として除外
      （last_month に取引ゼロでも最終月は許容）

    Returns: [(period, balance), ...]  最初に滞留が確認された月のみ
    """
    items   = [(p, v) for p, v in sorted(monthly_bal.items()) if p <= last_month]
    flagged = []
    streak  = 0  # 取引なし月の連続カウント

    for i, (period, bal) in enumerate(items):
        is_last = (period == last_month)

        if bal <= threshold:
            streak = 0
            continue

        has_activity = monthly_activity.get(period, False)

        if has_activity:
            # この月に取引あり → 正常、リセット
            streak = 0
        elif is_last:
            # 最終入力月に取引がなくても「まだ回収期限未到来」として除外
            pass
        else:
            # 取引なし かつ 最終月でもない → 滞留
            streak += 1
            if streak >= stale_count and not flagged:
                flagged.append((period, bal))

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
