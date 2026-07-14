"""
貸借対照表（BS）チェックモジュール
- 期首残高対応（補助科目単位）
- 売掛金・買掛金を補助科目（取引先）単位でチェック
- 期首残高未提供時はスキップ（誤検知防止）
"""
import re
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
    # 補助科目ごとのチェックで DataFrame 全体を何百回も再変換すると
    # 取引先の多い会社で二乗的に遅くなるため、重い派生列を一度だけ作って使い回す。
    ctx = _build_bs_ctx(df)
    issues = []
    issues.extend(_check_cash_balance(df, ob))
    # 除外科目を RECEIVABLE / PAYABLE リストから取り除く
    recv = [a for a in RECEIVABLE_ACCOUNTS if not any(e in a for e in excl)]
    pabl = [a for a in PAYABLE_ACCOUNTS    if not any(e in a for e in excl)]
    issues.extend(_check_receivables_by_sub(df, ob, recv, "debit",  last_month, ctx))
    issues.extend(_check_receivables_by_sub(df, ob, pabl, "credit", last_month, ctx))
    issues.extend(_check_tax_temp_accounts(df))
    issues.extend(_check_suspense_payments(df))
    # 借入金の返済予定表との照合は日常の定型作業であり、残高があるだけで
    # 一律に指摘するとノイズになるため通知しない（_check_loan_repayment は無効化）
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
        # 厳密一致: 「現金」が「小口現金」に誤ってヒットしないよう
        # 「科目名そのもの」か「科目名（補助科目）」のみにマッチ
        pat = f"^{re.escape(base_acc)}$|^{re.escape(base_acc)}（"
        d_rows = df[df["debit_account"].fillna("").astype(str).str.match(pat)]
        c_rows = df[df["credit_account"].fillna("").astype(str).str.match(pat)]
        if d_rows.empty and c_rows.empty:
            continue

        subs = _collect_subs(d_rows, c_rows, df)
        targets = [(base_acc, s) for s in subs] if subs else [(base_acc, None)]

        for (acc, sub) in targets:
            label   = f"{acc}（{sub}）" if sub else acc
            # 補助科目がある場合は専用残高のみ使用（合計値を誤適用しない）
            # _ob_get_fuzzy で括弧・スペースの表記ゆれにも対応
            if sub:
                opening = _ob_get_fuzzy(ob, label)  # 例: 普通預金（永和信用金庫・梅田）
            else:
                opening = _ob_get_fuzzy(ob, acc)    # 補助科目なしの場合のみ科目合計を使用

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
                    "level": "warning", "category": "BS", "account": label, "month": str(month),
                    "message": (
                        f"【要確認】{label} の仕訳上の残高が {bal:,.0f}円 とマイナスになっています"
                        f"（期首{opening:,.0f}円 + 当期仕訳増減）。"
                        "入金仕訳が漏れている可能性があります。"
                        "銀行連携等で自動計上されている仕訳が仕訳帳CSVに含まれていない場合も同様の表示になります。"
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
# 派生列の事前計算（補助科目ループで使い回す）
# ────────────────────────────────────────────────
def _build_bs_ctx(df: pd.DataFrame) -> dict:
    """補助科目単位のチェックで毎回 DataFrame 全体を再変換するのを避けるため、
    重い派生列（正規化した科目名・月次Period）を一度だけ計算して返す。"""
    return {
        "period":      df["date"].dt.to_period("M"),
        "debit_norm":  df["debit_account"].astype(str).str.strip(),
        "credit_norm": df["credit_account"].astype(str).str.strip(),
    }


# ────────────────────────────────────────────────
# 売掛金・買掛金を補助科目（取引先）単位でチェック
# ────────────────────────────────────────────────
def _check_receivables_by_sub(
    df: pd.DataFrame,
    ob: dict,
    accounts: List[str],
    normal_side: str,
    last_month: "pd.Period",
    ctx: dict = None,
) -> List[Dict[str, Any]]:
    """
    売掛金・買掛金・未払金等を補助科目（取引先）単位でチェックする。

    重要: `str.contains(base_acc)` は部分一致のため「未払金」を検索すると
    「リース未払金」もヒットする。そのため、実際の勘定科目名（actual_acc）を
    使って期首残高辞書（ob）を照合する。
    例: base_acc="未払金" でヒットした "リース未払金（名鉄協商...）" は
        ob["リース未払金（名鉄協商...）"] で正しく期首残高が見つかる。
    """
    if ctx is None:
        ctx = _build_bs_ctx(df)
    issues = []
    for base_acc in accounts:
        d_rows = df[ctx["debit_norm"].str.contains(base_acc, na=False)]
        c_rows = df[ctx["credit_norm"].str.contains(base_acc, na=False)]
        if d_rows.empty and c_rows.empty:
            continue

        # ── 実際の勘定科目名（actual_acc）ごとにグループ化 ──
        # 「未払金」「リース未払金」「未払費用」などが混在する場合でも
        # それぞれの正確な名前で期首残高を照合できる
        actual_accs: set = set()
        if "debit_account" in d_rows.columns:
            actual_accs |= set(d_rows["debit_account"].dropna().astype(str).str.strip().unique())
        if "credit_account" in c_rows.columns:
            actual_accs |= set(c_rows["credit_account"].dropna().astype(str).str.strip().unique())
        actual_accs.discard(""); actual_accs.discard("nan")

        for actual_acc in sorted(actual_accs):
            # この勘定科目の行は1回だけ絞り込み、補助科目ループで使い回す
            # （補助科目ごとに DataFrame 全体を再フィルタしない）
            d_acc = d_rows[ctx["debit_norm"].loc[d_rows.index] == actual_acc]
            c_acc = c_rows[ctx["credit_norm"].loc[c_rows.index] == actual_acc]

            subs = _collect_subs(d_acc, c_acc, df)

            # 期首残高未提供の補助科目を収集し、親科目単位で1件にまとめる
            missing_subs: list = []
            check_issues: list = []

            if not subs:
                _check_single_account(check_issues, df, actual_acc, None, ob, normal_side, last_month, ctx,
                                      d_base=d_acc, c_base=c_acc)
            else:
                for sub in subs:
                    sub_issues: list = []
                    _check_single_account(sub_issues, df, actual_acc, sub, ob, normal_side, last_month, ctx,
                                          d_base=d_acc, c_base=c_acc)
                    for iss in sub_issues:
                        if iss.get("level") == "info" and "期首残高未提供" in iss.get("message", ""):
                            missing_subs.append(sub)
                        else:
                            check_issues.append(iss)

            issues.extend(check_issues)

            # 期首残高未提供の補助科目について:
            # 当期に新規取引が発生した補助科目は期首0円が正常であり、
            # 未提供を一律に指摘するとノイズになるため通知しない。
            # （マイナス残高など実際の異常は _check_single_account 側で検知される）

    return issues


def _check_single_account(
    issues: list,
    df: pd.DataFrame,
    base_acc: str,
    sub: str | None,
    ob: dict,
    normal_side: str,
    last_month: "pd.Period",
    ctx: dict = None,
    d_base: pd.DataFrame = None,
    c_base: pd.DataFrame = None,
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
        # 括弧の不一致（例: 弥生の試算表で内側の）が省略される）に対応するため
        # 複数のバリエーションを試みる
        opening = _ob_get_fuzzy(ob, label)
        opening_missing = (opening is None)
        opening = opening if opening is not None else 0.0
    else:
        opening = ob.get(base_acc, 0.0)
        opening_missing = (base_acc not in ob)

    if ctx is None:
        ctx = _build_bs_ctx(df)

    # 実際の勘定科目名（base_acc）でフィルタ（事前計算した正規化列を使い回す）
    # 呼び出し側で科目単位に絞り込み済みの行があればそれを使う（補助科目ループでの再フィルタ回避）
    d_rows = d_base if d_base is not None else df[ctx["debit_norm"] == base_acc]
    c_rows = c_base if c_base is not None else df[ctx["credit_norm"] == base_acc]
    d_sub, c_sub = _filter_sub(d_rows, c_rows, sub)

    if d_sub.empty and c_sub.empty:
        return

    period = ctx["period"]

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
    """
    仮払消費税・仮受消費税の期首残高チェック。
    ※ 通常の消費税仕訳（課税取引に伴う自動計上）は「期首残高」ではないため除外する。
       本当の期首残高とは、期首日（最初の日付）以前から繰り越されたものを指す。
       ここでは ob（期首残高辞書）に値があれば、その値が0でないことを確認する。
       ob がない場合（期首残高ファイル未提供）はこのチェックをスキップする。
    """
    issues = []
    return issues  # 通常の消費税仕訳と期首繰越仕訳の区別が困難なためスキップ
    # ※ 仮払消費税の期首残高チェックは、期首残高ファイルの ob["仮払消費税"] を直接確認する方法に変更予定


# ────────────────────────────────────────────────
# 仮払金・前渡金
# ────────────────────────────────────────────────
def _check_suspense_payments(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    仮払金・前渡金の法人税誤処理チェックのみ行う。
    残高総額の表示は ar_ap_checker.py の _check_4_2_suspense_aging が
    個別明細ベースで行うため、ここでは重複させない。
    """
    issues = []
    for account in ["仮払金", "前渡金"]:
        entries = df[
            df["debit_account"].astype(str).str.contains(account, na=False) |
            df["credit_account"].astype(str).str.contains(account, na=False)
        ]
        if entries.empty:
            continue
        # 法人税の誤処理チェックのみ（残高総額チェックは 4-2 に委譲）
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


def _norm_key(s: str) -> str:
    """照合用にキーを正規化する（全角/半角スペース除去・株式会社記号統一）"""
    return (str(s)
            .replace("　", "").replace(" ", "")
            .replace("（株）", "㈱").replace("(株)", "㈱"))


def _ob_get_fuzzy(ob: dict, label: str):
    """
    期首残高辞書から label を検索する。
    括弧の不一致（弥生の試算表で入れ子括弧の末尾が省略されるケース）や
    スペースの全角/半角ゆれに対応するため、以下の順に検索する:
      1. 完全一致
      2. 末尾の「）」を1つ除いたキー  例: A（B（C））→ A（B（C）
      3. 末尾の「）」を追加したキー   例: A（B（C） → A（B（C））
      4. スペース・株式会社記号を正規化した上での一致
    """
    # 1. 完全一致
    if label in ob:
        return ob[label]
    # 2. 末尾）を除去
    if label.endswith("）"):
        alt = label[:-1]
        if alt in ob:
            return ob[alt]
    # 3. 末尾に）を追加
    alt2 = label + "）"
    if alt2 in ob:
        return ob[alt2]
    # 4. 正規化一致（スペース・記号ゆれ）
    norm = _norm_key(label)
    for k, v in ob.items():
        if _norm_key(k) == norm:
            return v
    return None


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
