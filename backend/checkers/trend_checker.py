"""
カテゴリ6: 損益推移分析による「定期取引の欠落」チェック

仮想の損益推移表（勘定科目 × 月 の金額マトリクス）をデータから自動生成し、
「毎月発生していた費用・取引が、ある月だけ計上されていない」欠落を検知する。

既存の網羅性チェック（completeness_checker）との違い:
  - 1-1 は「地代家賃・リース料…」など固定リストの科目のみが対象
  - 1-4 は固定リストの補助科目、かつ「直近月だけ抜けた」ケースのみが対象
  本チェックは対象を固定せず、データ自体から「毎月発生している科目・取引」を
  推定し、全期間にわたって欠落月を検知する（データ駆動）。

6-1: 勘定科目レベル ― 毎月計上されていた費用が、ある月に無い
6-2: 補助科目レベル ― 毎月あった取引先/契約が、ある月に無い（科目自体は在る月）
6-3: 摘要レベル     ― 補助科目が無くても、摘要から同一の定期取引を識別して欠落を検知
6-4: 途絶          ― 期の途中まで毎月あった取引が、以降ずっと計上されていない
"""
import re

import pandas as pd
from typing import List, Dict, Any

# 既存チェックとの重複アラートを避けるため、固定リストを取り込む
from checkers.completeness_checker import (
    fiscal_period_series,
    RECURRING_ACCOUNTS,       # 1-1 が扱う科目
    RECURRING_SUB_ACCOUNTS,   # 1-4 が扱う科目
)

# ── 損益（PL）以外の科目を除外するためのキーワード ──
# 損益推移表は損益科目を対象とするため、資産・負債・純資産・決済科目は除外する。
# （これらは毎月出入りするため欠落しにくく、出しても意味が薄い）
NON_PL_KEYWORDS = [
    # 現預金・決済
    "現金", "預金", "当座", "小口現金",
    # 債権債務・経過勘定
    "売掛金", "買掛金", "未払金", "未払費用", "未払法人税", "未払消費税",
    "前払費用", "前払金", "前受金", "前受収益", "未収入金", "未収収益",
    "仮払金", "仮受金", "仮払消費税", "仮受消費税", "預り金", "立替金",
    "貸付金", "借入金", "未成工事支出金",
    # 固定資産・投資その他
    "建物", "構築物", "機械装置", "車両運搬具", "工具器具備品", "土地",
    "リース資産", "ソフトウェア", "のれん", "出資金", "有価証券",
    "敷金", "差入保証金", "保証金", "建設仮勘定", "長期前払費用", "繰延資産",
    # 純資産・事業主勘定
    "資本金", "資本準備金", "利益準備金", "繰越利益", "自己株式",
    "事業主貸", "事業主借", "元入金", "未払配当金",
]

# 棚卸資産（BS）だが、損益/製造原価の科目（商品仕入高・期末商品棚卸高 等）の
# 部分文字列でもあるため、完全一致でのみ除外する。
# 例）「商品」は除外するが「商品仕入高」「期末商品棚卸高」は損益項目として対象に含める。
NON_PL_EXACT = {
    "商品", "製品", "半製品", "原材料", "材料", "貯蔵品",
    "仕掛品", "繰越商品", "副産物", "作業くず",
}

MIN_MONTHS   = 4      # 定期性を判定するのに必要な最小月数（これ未満なら判定しない）
MIN_PRESENT  = 3      # 「定期」と見なすのに最低限必要な計上月数
RECUR_RATIO  = 0.75   # 全月のうち何割以上に計上があれば「定期」と見なすか
MIN_TYPICAL  = 1_000  # 月あたり典型額がこの額未満の科目はノイズとして除外

# 補助科目が付いていない行をまとめる系列ラベル
NO_SUB = "（補助科目なし）"


def check_trend(df: pd.DataFrame, fiscal_cutoff_day: int = 1) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    if "date" not in df.columns or df["date"].dropna().empty:
        return issues

    work = df.copy()
    work["_fp"] = fiscal_period_series(work["date"], fiscal_cutoff_day)
    all_periods = sorted(p for p in work["_fp"].dropna().unique())
    if len(all_periods) < MIN_MONTHS:
        return issues  # 期間が短すぎて定期性を判定できない

    # 各チェックが指摘した対象を記録し、後段のチェックで重複指摘を避ける
    flagged = {"accounts": set(), "pairs": set()}
    issues.extend(_check_6_1_account(work, all_periods, fiscal_cutoff_day, flagged))
    issues.extend(_check_6_2_subaccount(work, all_periods, flagged))
    issues.extend(_check_6_3_by_description(work, all_periods, flagged))
    issues.extend(_check_6_4_discontinued(work, all_periods, flagged))
    return issues


def _is_pl_account(name: str) -> bool:
    """損益科目・製造原価科目とみなせるか（空・決済・BS科目は除外）"""
    if not name or name in ("nan", "None"):
        return False
    if name in NON_PL_EXACT:
        return False
    return not any(kw in name for kw in NON_PL_KEYWORDS)


# ──────────────────────────────────────────────────────────
# 6-1: 勘定科目レベルの定期費用の欠落
# ──────────────────────────────────────────────────────────
def _check_6_1_account(work: pd.DataFrame, all_periods: list,
                       fiscal_cutoff_day: int = 1,
                       flagged: dict = None) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    total = len(all_periods)
    period_set = set(all_periods)

    # 借方に計上のある損益科目だけを対象にする
    rows = work[work["debit_amount"] != 0].copy()
    rows["_acc"] = rows["debit_account"].fillna("").astype(str).str.strip()
    # 科目名はユニーク数が少ないので、行単位ではなく科目単位で判定する（高速化）
    pl_accounts = {a for a in rows["_acc"].unique() if _is_pl_account(a)}
    rows = rows[rows["_acc"].isin(pl_accounts)]
    if rows.empty:
        return issues

    # 科目 × 月 の金額合計（＝仮想の損益推移表）
    pivot = rows.groupby(["_acc", "_fp"])["debit_amount"].sum()

    for account, monthly in pivot.groupby(level=0):
        # 1-1 が既に扱う定例科目はスキップ（重複アラート防止）
        if any(kw in account for kw in RECURRING_ACCOUNTS):
            continue

        present = {p for (_, p) in monthly.index}
        n_present = len(present)
        # 「定期」判定: 十分な月数に計上があり、かつ欠落が存在する
        if n_present < MIN_PRESENT or n_present >= total:
            continue
        if n_present / total < RECUR_RATIO:
            continue

        typical = float(monthly.median())
        if typical < MIN_TYPICAL:
            continue

        missing = [p for p in all_periods if p not in present]
        if not missing:
            continue

        s = ", ".join(str(m) for m in missing[:4])
        suffix = f"（他{len(missing)-4}ヶ月）" if len(missing) > 4 else ""
        note = f"（締め日:{fiscal_cutoff_day}日基準）" if fiscal_cutoff_day > 1 else ""
        if flagged is not None:
            flagged["accounts"].add(account)
        issues.append({
            "level": "warning", "category": "6-1 定期費用の欠落",
            "check_id": "6-1", "account": account, "month": s,
            "message": (
                f"【6-1・中】「{account}」は全{total}ヶ月中{n_present}ヶ月で計上（月額 約{typical:,.0f}円）"
                f"されていますが、次の月には計上がありません: {s}{suffix}{note}。"
                "毎月発生している費用の計上漏れ、または科目誤りの可能性があります。"
                "（契約終了・季節性による場合は問題ありません）"
            ),
        })
    return issues


# ──────────────────────────────────────────────────────────
# 6-2: 補助科目（取引先・契約）レベルの定期取引の欠落
# ──────────────────────────────────────────────────────────
def _check_6_2_subaccount(work: pd.DataFrame, all_periods: list,
                          flagged: dict = None) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    if "debit_sub" not in work.columns:
        return issues
    total = len(all_periods)
    last_period = all_periods[-1]

    rows = work[work["debit_amount"] != 0].copy()
    rows["_acc"] = rows["debit_account"].fillna("").astype(str).str.strip()
    rows["_sub"] = rows["debit_sub"].fillna("").astype(str).str.strip()
    pl_accounts = {a for a in rows["_acc"].unique() if _is_pl_account(a)}
    rows = rows[rows["_acc"].isin(pl_accounts)]
    # 補助科目が付いていない行も対象にする（空欄は「（補助科目なし）」という1つの系列として扱う）
    blank = rows["_sub"].isin(["", "nan", "None", "指定なし"])
    rows.loc[blank, "_sub"] = NO_SUB
    if rows.empty:
        return issues

    # その科目がどの月に計上されているか（科目自体の在籍月）
    acc_months = rows.groupby("_acc")["_fp"].agg(lambda s: set(s.dropna())).to_dict()

    grouped = rows.groupby(["_acc", "_sub", "_fp"])["debit_amount"].sum()
    for (account, sub), monthly in grouped.groupby(level=[0, 1]):
        present = {p for (_, _, p) in monthly.index}
        n_present = len(present)
        if n_present < MIN_PRESENT or n_present >= total:
            continue
        if n_present / total < RECUR_RATIO:
            continue

        typical = float(monthly.median())
        if typical < MIN_TYPICAL:
            continue

        acc_present = acc_months.get(account, set())
        missing = []
        for p in all_periods:
            if p in present:
                continue
            # 科目自体がその月に無い場合は 6-1 側の話なのでスキップ（科目は在るのに補助だけ無い月を検知）
            if p not in acc_present:
                continue
            # 1-4（固定リスト×直近月×補助科目あり）が扱うケースは重複回避
            if p == last_period and sub != NO_SUB and any(kw in account for kw in RECURRING_SUB_ACCOUNTS):
                continue
            missing.append(p)
        if not missing:
            continue

        s = ", ".join(str(m) for m in missing[:4])
        suffix = f"（他{len(missing)-4}ヶ月）" if len(missing) > 4 else ""
        if flagged is not None:
            flagged["pairs"].add((account, sub))
        if sub == NO_SUB:
            label = f"「{account}」（補助科目なし）"
            tail = "この費用の計上がありません"
            hint = "定期費用の計上漏れの可能性があります"
        else:
            label = f"{account}「{sub}」"
            tail = "この取引先/補助科目の計上がありません"
            hint = "定期取引の計上漏れ、または補助科目の付け忘れの可能性があります"
        issues.append({
            "level": "warning", "category": "6-2 定期取引の欠落",
            "check_id": "6-2", "account": account, "month": s,
            "message": (
                f"【6-2・中】{label}は{n_present}ヶ月で計上（月額 約{typical:,.0f}円）"
                f"されていますが、次の月は{account}に他の計上があるのに{tail}: "
                f"{s}{suffix}。{hint}。"
                "（取引終了・契約終了による場合は問題ありません）"
            ),
        })
    return issues


# ──────────────────────────────────────────────────────────
# 6-3: 摘要（取引内容）レベルの定期費用の欠落
# ──────────────────────────────────────────────────────────
# 6-1（科目単位）・6-2（補助科目単位）では、
# 「同じ科目に他の取引が毎月あるため科目全体では欠落に見えない」
# かつ「補助科目が付いていない」定期費用を検知できない。
# 例）新聞図書費の中の「日経電子版購読料」だけが当月抜けている
#     → 新聞図書費には書籍代等が毎月あるため 6-1 では気づけず、
#       補助科目もないため 6-2 の系列も他取引と混ざってしまう。
# そこで摘要を正規化したキーで「取引の種類」を識別して定期性を見る。

# 摘要から取引を識別するために除去するノイズ（数字・記号・空白）
_DESC_NOISE_RE = re.compile(r"[0-9０-９,，.．/／\-－―ー~〜()（）\[\]【】<>「」・\s　]+")
# 「6月分」「4月度」などの時期表現（数字除去後に残る形）
_DESC_PERIOD_RE = re.compile(r"(月分|月度|年分|期分|日分|月度分)")

DESC_MIN_LEN   = 3     # 正規化後キーの最小文字数（短すぎる語は識別力がない）
DESC_STABLE_R  = 0.5   # 金額のブレ許容（(最大-最小) <= 中央値*この値 なら定額とみなす）


def _desc_key(text: str) -> str:
    """摘要から「取引の種類」を表すキーを作る。
    例) 「日経電子版 6月分」→「日経電子版」／「ＮＴＴ通信料(5月)」→「ＮＴＴ通信料」"""
    s = str(text).strip()
    if not s or s.lower() in ("nan", "none"):
        return ""
    s = _DESC_NOISE_RE.sub("", s)
    s = _DESC_PERIOD_RE.sub("", s)
    return s


def _check_6_3_by_description(work: pd.DataFrame, all_periods: list,
                              flagged: dict = None) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    if "description" not in work.columns:
        return issues
    total = len(all_periods)

    rows = work[work["debit_amount"] != 0].copy()
    rows["_acc"] = rows["debit_account"].fillna("").astype(str).str.strip()
    pl_accounts = {a for a in rows["_acc"].unique() if _is_pl_account(a)}
    rows = rows[rows["_acc"].isin(pl_accounts)]
    if rows.empty:
        return issues

    rows["_dkey"] = rows["description"].fillna("").astype(str).map(_desc_key)
    rows = rows[rows["_dkey"].str.len() >= DESC_MIN_LEN]
    if rows.empty:
        return issues

    grouped = rows.groupby(["_acc", "_dkey", "_fp"])["debit_amount"].sum()
    for (account, dkey), monthly in grouped.groupby(level=[0, 1]):
        # 6-1 で科目ごと指摘済みなら重複させない
        if flagged is not None and account in flagged["accounts"]:
            continue

        present = {p for (_, _, p) in monthly.index}
        n_present = len(present)
        if n_present < MIN_PRESENT or n_present >= total:
            continue
        if n_present / total < RECUR_RATIO:
            continue

        typical = float(monthly.median())
        if typical < MIN_TYPICAL:
            continue
        # 定期購読・定額サービスに絞る（金額が毎回大きく変わる取引は対象外）
        span = float(monthly.max() - monthly.min())
        if span > typical * DESC_STABLE_R:
            continue

        missing = [p for p in all_periods if p not in present]
        if not missing:
            continue

        # 元の摘要（読みやすい方）を代表として表示する
        sample = rows[(rows["_acc"] == account) & (rows["_dkey"] == dkey)]["description"].iloc[0]
        s = ", ".join(str(m) for m in missing[:4])
        suffix = f"（他{len(missing)-4}ヶ月）" if len(missing) > 4 else ""
        issues.append({
            "level": "warning", "category": "6-3 定期費用の欠落（摘要）",
            "check_id": "6-3", "account": account, "month": s,
            "message": (
                f"【6-3・中】{account}の「{str(sample)[:30]}」は全{total}ヶ月中{n_present}ヶ月で"
                f"計上（毎月ほぼ定額 約{typical:,.0f}円）されていますが、"
                f"次の月には同じ内容の計上がありません: {s}{suffix}。"
                f"{account}には他の計上があるため科目全体では気づきにくい計上漏れです。"
                "（契約終了・解約による場合は問題ありません）"
            ),
        })
    return issues


# ──────────────────────────────────────────────────────────
# 6-4: 定期取引の途絶（解約・契約終了の確認）
# ──────────────────────────────────────────────────────────
# 6-1〜6-3 は「全期間の一定割合以上に計上がある」ことを定期性の条件とするため、
# 期の途中から計上が止まったもの（解約・契約終了の可能性）を取りこぼす。
# 例）リース料の補助科目「自動車ヴィッツ」が期初は毎月あったが、
#     途中から計上が無くなった → 解約か計上漏れかの確認が必要。

DISCONT_MIN_GAP  = 2     # 直近何ヶ月連続で計上が無ければ「途絶」とみなすか
DISCONT_DENSITY  = 0.75  # 計上されていた期間中の月次密度（これ以上で「定期的だった」）


def _check_6_4_discontinued(work: pd.DataFrame, all_periods: list,
                            flagged: dict = None) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    total = len(all_periods)
    pos = {p: i for i, p in enumerate(all_periods)}

    rows = work[work["debit_amount"] != 0].copy()
    rows["_acc"] = rows["debit_account"].fillna("").astype(str).str.strip()
    pl_accounts = {a for a in rows["_acc"].unique() if _is_pl_account(a)}
    rows = rows[rows["_acc"].isin(pl_accounts)]
    if rows.empty:
        return issues

    if "debit_sub" in rows.columns:
        rows["_sub"] = rows["debit_sub"].fillna("").astype(str).str.strip()
        blank = rows["_sub"].isin(["", "nan", "None", "指定なし"])
        rows.loc[blank, "_sub"] = NO_SUB
    else:
        rows["_sub"] = NO_SUB

    grouped = rows.groupby(["_acc", "_sub", "_fp"])["debit_amount"].sum()
    for (account, sub), monthly in grouped.groupby(level=[0, 1]):
        if flagged is not None and (
            account in flagged["accounts"] or (account, sub) in flagged["pairs"]
        ):
            continue

        present = sorted({p for (_, _, p) in monthly.index})
        if len(present) < MIN_PRESENT:
            continue

        first_i, last_i = pos[present[0]], pos[present[-1]]
        # 直近何ヶ月計上が無いか
        gap = total - 1 - last_i
        if gap < DISCONT_MIN_GAP:
            continue
        # 計上されていた期間中は定期的だったか（散発的な取引を除外）
        active_span = last_i - first_i + 1
        if active_span < MIN_PRESENT or len(present) / active_span < DISCONT_DENSITY:
            continue

        typical = float(monthly.median())
        if typical < MIN_TYPICAL:
            continue

        label = f"{account}「{sub}」" if sub != NO_SUB else f"「{account}」"
        issues.append({
            "level": "warning", "category": "6-4 定期取引の途絶",
            "check_id": "6-4", "account": account, "month": str(all_periods[last_i]),
            "message": (
                f"【6-4・中】{label}は {present[0]}〜{present[-1]} に毎月計上"
                f"（月額 約{typical:,.0f}円）されていましたが、"
                f"{all_periods[last_i + 1]} 以降 {gap}ヶ月連続で計上がありません。"
                "解約・契約終了によるものか、計上漏れかを確認してください。"
            ),
        })
    return issues
