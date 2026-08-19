"""
カテゴリ8: BS・PLの整合チェック（決算・税務調査を見据えた論点）

8-1: 借入金の返済と支払利息の整合
8-2: 期ズレの兆候（売上の月末集中・翌月初の取消）
8-3: 役員貸付金・役員借入金の発生
8-4: 固定資産の取得と減価償却開始の整合
8-5: 現金残高の過大・滞留
"""
import pandas as pd
from typing import List, Dict, Any

from checkers.check_utils import desc_safe, date_safe, slip_safe

# 8-1: 利息を計上すべき借入金（役員借入は無利息もあるため対象外）
LOAN_ACCOUNTS_RE = "短期借入金|長期借入金"
INTEREST_RE = "支払利息|利息割引料"
LOAN_MISSING_MONTHS_MIN = 2   # 利息の無い返済月が何ヶ月あれば指摘するか

# 8-2: 期ズレ兆候
MONTH_END_DAYS = 3            # 「月末」とみなす末日からの日数
MONTH_END_RATIO = 0.6         # 月末集中と判定する月間売上に対する割合
MONTH_END_MIN_TOTAL = 1_000_000
REVERSAL_MIN_AMOUNT = 100_000  # 月初の売上取消（借方売上）の最低額
REVERSAL_DAYS = 3
# 返品・値引は通常の商行為なので取消とは区別する
REVERSAL_EXCLUDE_KW = ["返品", "値引", "割戻", "リベート"]

# 8-3: 役員貸付金・役員借入金
OFFICER_LOAN_MIN = 100_000     # 役員貸付金の増加をこの額以上で指摘
OFFICER_BORROW_MIN = 1_000_000  # 役員借入金の増加をこの額以上で通知

# 8-4: 減価償却
DEP_ASSET_MIN = 300_000        # 償却開始チェックの対象とする取得額
# 少額特例（〜30万円即時償却）の対象外となる規模の取得のみ見る

# 8-5: 現金
CASH_LARGE_BALANCE = 1_000_000  # 現金残高がこの額を超えて継続したら通知
CASH_LARGE_MONTHS = 2


def check_consistency(df: pd.DataFrame, opening_balances: dict = None) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    if df.empty or "date" not in df.columns or df["date"].dropna().empty:
        return issues
    ob = opening_balances or {}

    issues.extend(_check_8_1_loan_interest(df))
    issues.extend(_check_8_2_period_shift(df))
    issues.extend(_check_8_3_officer_loans(df))
    issues.extend(_check_8_4_depreciation_start(df))
    issues.extend(_check_8_5_cash_balance(df, ob))
    return issues


# ──────────────────────────────────────────────────────────
# 8-1: 借入金の返済と支払利息の整合
# ──────────────────────────────────────────────────────────
def _check_8_1_loan_interest(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    借入金の返済（借方）がある月に支払利息の計上が無い場合を検出する。
    通常、金融機関の借入は元金と利息が同時に引き落とされるため、
    返済月に利息ゼロは「利息の計上漏れ」または「全額を元金に充てた科目誤り」を疑う。
    ※ 役員借入金は無利息が普通のため対象外。
    """
    issues = []
    d_acc = df["debit_account"].fillna("").astype(str)
    loan_repay = df[
        d_acc.str.contains(LOAN_ACCOUNTS_RE, na=False) &
        ~d_acc.str.contains("役員", na=False) &
        (df["debit_amount"] > 0)
    ]
    if loan_repay.empty:
        # 借入金の動きが無く支払利息だけがある場合の科目確認
        interest = df[d_acc.str.contains(INTEREST_RE, na=False)]
        loan_any = (
            df["debit_account"].fillna("").astype(str).str.contains(LOAN_ACCOUNTS_RE, na=False) |
            df["credit_account"].fillna("").astype(str).str.contains(LOAN_ACCOUNTS_RE, na=False)
        ).any()
        months = interest["date"].dropna().dt.to_period("M").nunique()
        if not loan_any and months >= 3:
            issues.append({
                "level": "info", "category": "8-1 借入金と利息",
                "check_id": "8-1", "account": "支払利息", "month": "全期間",
                "message": (
                    f"【8-1・低】支払利息が {months}ヶ月 計上されていますが、"
                    "仕訳帳に借入金（短期・長期）の動きがありません。"
                    "借入金の返済仕訳が別科目で処理されていないか、"
                    "または利息の内容（何に対する利息か）をご確認ください。"
                ),
            })
        return issues

    repay_months = set(loan_repay["date"].dropna().dt.to_period("M"))
    interest_months = set(
        df[d_acc.str.contains(INTEREST_RE, na=False)]["date"].dropna().dt.to_period("M")
    )
    missing = sorted(repay_months - interest_months)
    if len(missing) < LOAN_MISSING_MONTHS_MIN:
        return issues

    s = ", ".join(str(m) for m in missing[:6])
    suffix = f"（他{len(missing)-6}ヶ月）" if len(missing) > 6 else ""
    issues.append({
        "level": "warning", "category": "8-1 借入金と利息",
        "check_id": "8-1", "account": "借入金", "month": s,
        "message": (
            f"【8-1・中】借入金の返済がある月のうち、支払利息の計上が無い月が"
            f" {len(missing)}ヶ月 あります: {s}{suffix}。"
            "返済額の全額を元金に充てている（利息の計上漏れ・科目誤り）"
            "可能性があります。返済予定表と照合してください。"
        ),
    })
    return issues


# ──────────────────────────────────────────────────────────
# 8-2: 期ズレの兆候（売上の月末集中・翌月初の取消）
# ──────────────────────────────────────────────────────────
def _check_8_2_period_shift(df: pd.DataFrame) -> List[Dict[str, Any]]:
    issues = []
    c_acc = df["credit_account"].fillna("").astype(str)
    sales_cr = df[
        c_acc.str.contains("売上", na=False) &
        ~c_acc.str.contains("仮受", na=False) &
        (df["credit_amount"] > 0) &
        df["date"].notna()
    ]

    # (A) 月末集中: 月間売上の大半が月末3日以内に計上されている
    if not sales_cr.empty:
        period = sales_cr["date"].dt.to_period("M")
        for p, grp in sales_cr.groupby(period):
            total = float(grp["credit_amount"].sum())
            if total < MONTH_END_MIN_TOTAL:
                continue
            last_day = p.end_time.day
            end_rows = grp[grp["date"].dt.day > last_day - MONTH_END_DAYS]
            # 月に数件しかないなら月末1回請求の商習慣なので指摘しない
            if len(grp) < 10 or len(end_rows) < 5:
                continue
            ratio = float(end_rows["credit_amount"].sum()) / total
            if ratio >= MONTH_END_RATIO:
                issues.append({
                    "level": "info", "category": "8-2 期ズレ兆候",
                    "check_id": "8-2", "account": "売上高", "month": str(p),
                    "message": (
                        f"【8-2・低】{p} の売上のうち {ratio*100:.0f}% が"
                        f"月末{MONTH_END_DAYS}日間に集中しています"
                        f"（{len(end_rows)}件 / 月計 {total:,.0f}円）。"
                        "締め処理による一括計上であれば問題ありませんが、"
                        "計上基準（出荷・検収）どおりの日付になっているかご確認ください。"
                    ),
                })

    # (B) 翌月初の売上取消: 月初3日以内の借方売上（返品・値引を除く）
    d_acc = df["debit_account"].fillna("").astype(str)
    reversal = df[
        d_acc.str.contains("売上", na=False) &
        ~d_acc.str.contains("仮受|売上原価", na=False) &
        (df["debit_amount"] >= REVERSAL_MIN_AMOUNT) &
        df["date"].notna()
    ]
    reversal = reversal[reversal["date"].dt.day <= REVERSAL_DAYS]
    if not reversal.empty:
        desc = reversal["description"].fillna("").astype(str)
        reversal = reversal[~desc.apply(lambda x: any(k in x for k in REVERSAL_EXCLUDE_KW))]
    for _, row in reversal.iterrows():
        d = desc_safe(row)
        issues.append({
            "level": "warning", "category": "8-2 期ズレ兆候",
            "check_id": "8-2", "account": str(row["debit_account"]),
            "month": date_safe(row), "slip": slip_safe(row),
            "message": (
                f"【8-2・中】月初{REVERSAL_DAYS}日以内に売上の取消"
                f"（借方 {row['debit_amount']:,.0f}円）があります"
                + (f"（摘要: 「{d}」）" if d else "") + "。"
                "前月に計上した売上を翌月に取り消している場合、"
                "前月の売上計上（期間帰属）が適切だったかご確認ください。"
            ),
        })
    return issues


# ──────────────────────────────────────────────────────────
# 8-3: 役員貸付金・役員借入金の発生
# ──────────────────────────────────────────────────────────
def _check_8_3_officer_loans(df: pd.DataFrame) -> List[Dict[str, Any]]:
    issues = []
    d_acc = df["debit_account"].fillna("").astype(str)
    c_acc = df["credit_account"].fillna("").astype(str)

    # 役員貸付金の増加（税務調査で注目されやすく、認定利息の計上が必要）
    lend_mask = d_acc.str.contains("役員", na=False) & d_acc.str.contains("貸付", na=False)
    lend_back = c_acc.str.contains("役員", na=False) & c_acc.str.contains("貸付", na=False)
    lend_inc = float(df[lend_mask]["debit_amount"].sum())
    lend_dec = float(df[lend_back]["credit_amount"].sum())
    net_lend = lend_inc - lend_dec
    if lend_inc >= OFFICER_LOAN_MIN:
        issues.append({
            "level": "warning", "category": "8-3 役員貸付金",
            "check_id": "8-3", "account": "役員貸付金", "month": "全期間",
            "message": (
                f"【8-3・中】役員貸付金の発生が 累計 {lend_inc:,.0f}円"
                f"（純増 {net_lend:,.0f}円）あります。"
                "役員への貸付には認定利息（受取利息）の計上が必要です。"
                "金銭消費貸借契約書の整備と利息計上の状況をご確認ください。"
            ),
        })

    # 役員借入金の大幅増加（相続財産化・債務超過の論点として把握）
    borrow_mask = c_acc.str.contains("役員", na=False) & c_acc.str.contains("借入", na=False)
    borrow_back = d_acc.str.contains("役員", na=False) & d_acc.str.contains("借入", na=False)
    net_borrow = float(df[borrow_mask]["credit_amount"].sum()) - float(df[borrow_back]["debit_amount"].sum())
    if net_borrow >= OFFICER_BORROW_MIN:
        issues.append({
            "level": "info", "category": "8-3 役員借入金",
            "check_id": "8-3", "account": "役員借入金", "month": "全期間",
            "message": (
                f"【8-3・低】役員借入金が当期間で {net_borrow:,.0f}円 純増しています。"
                "役員からの借入の増加は資金繰りのシグナルであると同時に、"
                "相続財産（貸付債権）になる論点もあります。状況を把握しておいてください。"
            ),
        })
    return issues


# ──────────────────────────────────────────────────────────
# 8-4: 固定資産の取得と減価償却開始の整合
# ──────────────────────────────────────────────────────────
# 固定資産科目（asset_checker と同一の定義を使用）
from checkers.asset_checker import FIXED_ASSET_ACCOUNTS, FIXED_ASSET_EXCLUDE


def _check_8_4_depreciation_start(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    月次で減価償却費を計上している会社で、期中に固定資産（30万円以上）を
    取得したのに減価償却費の月額が全く変わっていない場合、
    新規資産の償却登録漏れの可能性として確認を促す。
    """
    issues = []
    dep = df[df["debit_account"].fillna("").astype(str).str.contains("減価償却費", na=False)]
    dep_monthly = dep.groupby(dep["date"].dt.to_period("M"))["debit_amount"].sum()
    if len(dep_monthly) < 3:
        return issues  # 月次償却を採用していない（決算一括）なら対象外

    acc = df["debit_account"].fillna("").astype(str)
    acq = df[
        acc.apply(lambda x: any(a in x for a in FIXED_ASSET_ACCOUNTS)
                  and not any(e in x for e in FIXED_ASSET_EXCLUDE)) &
        (df["debit_amount"] >= DEP_ASSET_MIN) &
        df["date"].notna()
    ]
    if acq.empty:
        return issues

    for _, row in acq.iterrows():
        acq_p = row["date"].to_period("M")
        before = dep_monthly[dep_monthly.index < acq_p]
        after = dep_monthly[dep_monthly.index > acq_p]
        if before.empty or len(after) < 2:
            continue  # 取得後のデータが少なく判定できない
        # 取得後2ヶ月経っても償却月額が1円も変わらない → 登録漏れの可能性
        if abs(float(after.iloc[:2].mean()) - float(before.iloc[-2:].mean())) < 1:
            issues.append({
                "level": "info", "category": "8-4 減価償却の開始",
                "check_id": "8-4", "account": str(row["debit_account"]),
                "month": str(acq_p), "slip": slip_safe(row),
                "message": (
                    f"【8-4・低】{acq_p} に {row['debit_account']} へ"
                    f" {row['debit_amount']:,.0f}円 の取得がありますが、"
                    "その後も減価償却費の月額が変わっていません。"
                    "新規取得資産が償却資産として登録されているかご確認ください。"
                    "（償却開始月の設定による場合は問題ありません）"
                ),
            })
    return issues


# ──────────────────────────────────────────────────────────
# 8-5: 現金残高の過大・滞留
# ──────────────────────────────────────────────────────────
def _check_8_5_cash_balance(df: pd.DataFrame, ob: dict) -> List[Dict[str, Any]]:
    """
    現金残高が大きいまま数ヶ月続いている場合の確認喚起。
    帳簿上の現金が実際より大きい（実査差異・簿外流用）は税務調査の定番論点。
    期首残高（現金）が提供されている場合のみ判定する。
    """
    issues = []
    opening = ob.get("現金")
    if opening is None:
        return issues

    import re
    pat = re.compile(r"^現金$|^現金（")
    d = df[df["debit_account"].fillna("").astype(str).str.match(pat)]
    c = df[df["credit_account"].fillna("").astype(str).str.match(pat)]
    if d.empty and c.empty:
        return issues

    period_d = d.groupby(d["date"].dt.to_period("M"))["debit_amount"].sum()
    period_c = c.groupby(c["date"].dt.to_period("M"))["credit_amount"].sum()
    net = period_d.subtract(period_c, fill_value=0).sort_index()
    balance = net.cumsum() + float(opening)

    large = balance[balance > CASH_LARGE_BALANCE]
    if len(large) >= CASH_LARGE_MONTHS:
        peak = float(balance.max())
        issues.append({
            "level": "info", "category": "8-5 現金残高",
            "check_id": "8-5", "account": "現金", "month": str(large.index[-1]),
            "message": (
                f"【8-5・低】帳簿上の現金残高が {CASH_LARGE_BALANCE:,.0f}円 を超える月が"
                f" {len(large)}ヶ月 あります（最大 {peak:,.0f}円）。"
                "実際の手許現金と一致しているか（実査）をご確認ください。"
                "現金商売でない場合、過大な現金残高は税務調査で確認されやすい項目です。"
            ),
        })
    return issues
