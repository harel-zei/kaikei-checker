"""
カテゴリ5: 税務リスク・ガバナンスチェック
5-1: 役員給与の定期同額要件
5-2: 重複仕訳の検知
5-3: 金額の桁数ミス・逆仕訳
5-4: 交際費と会議費・福利厚生費の境界線
5-5: 源泉所得税の徴収漏れ
"""
import pandas as pd
import numpy as np
from typing import List, Dict, Any
from checkers.check_utils import desc_safe, month_safe

# 5-4: 交際費への疑いキーワード
KW_ENTERTAINMENT = [
    "贈答", "ゴルフ", "お中元", "お歳暮", "中元", "歳暮",
    "祝儀", "香典", "ギフト", "gift", "Gift",
]

# 5-5: 源泉徴収が必要な報酬キーワード
KW_WITHHOLDING = [
    "弁護士", "税理士", "司法書士", "行政書士", "社会保険労務士",
    "デザイン", "デザイナー", "講師", "原稿", "翻訳", "通訳",
    "著作", "イラスト", "写真家", "カメラマン", "コンサルタント",
]

ENTERTAINMENT_UNIT_LIMIT = 10_000  # 1人あたり1万円超で交際費
DIGIT_ERROR_MULTIPLIER   = 5.0     # 平均の5倍以上/5分の1以下


def check_governance(df: pd.DataFrame) -> List[Dict[str, Any]]:
    issues = []
    issues.extend(_check_5_1_director_pay(df))
    issues.extend(_check_5_2_duplicate_entries(df))
    issues.extend(_check_5_3_digit_error(df))
    issues.extend(_check_5_4_entertainment(df))
    issues.extend(_check_5_5_withholding(df))
    return issues


# ──────────────────────────────────────────────────────────
# 5-1: 役員給与の定期同額要件
# ──────────────────────────────────────────────────────────
def _check_5_1_director_pay(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    役員報酬の月次集計を行い、期首3ヶ月以降に金額変動があれば警告
    """
    issues = []
    mask = df["debit_account"].astype(str).str.contains("役員報酬|役員給与", na=False)
    entries = df[mask].copy()
    if entries.empty:
        return issues

    period = entries["date"].dt.to_period("M")
    monthly = entries.groupby(period)["debit_amount"].sum().sort_index()

    if len(monthly) < 4:
        return issues

    # 4ヶ月目以降の変動チェック
    base_months = monthly.iloc[:3]
    base_amount = base_months.mean()
    check_months = monthly.iloc[3:]

    for p, val in check_months.items():
        if abs(val - base_amount) > 1:  # 1円以上の差
            issues.append({
                "level": "error", "category": "5-1 役員給与定期同額",
                "check_id": "5-1", "account": "役員報酬",
                "month": str(p),
                "message": (
                    f"【5-1・高】役員報酬が {p} に {val:,.0f}円 と、"
                    f"期初3ヶ月の平均額（{base_amount:,.0f}円）から変動しています。"
                    "定期同額給与は期首から3ヶ月以内の改定を除き、"
                    "変動があると損金不算入となります。"
                ),
            })
    return issues


# ──────────────────────────────────────────────────────────
# 5-2: 重複仕訳の検知
# ──────────────────────────────────────────────────────────
def _check_5_2_duplicate_entries(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    金額・借方科目が同じで、日付が7日以内、摘要が類似している仕訳ペアを検知
    """
    issues = []
    if df.empty:
        return issues

    work = df[df["debit_amount"] > 0].copy().reset_index(drop=True)
    if len(work) < 2:
        return issues

    found_pairs = set()

    for i in range(len(work)):
        for j in range(i + 1, min(i + 50, len(work))):  # 近傍50件だけ比較
            ri = work.iloc[i]
            rj = work.iloc[j]

            if ri["debit_amount"] != rj["debit_amount"]:
                continue
            if ri["debit_account"] != rj["debit_account"]:
                continue
            # 補助科目が異なる場合は別取引 → 重複ではない
            sub_i = str(ri.get("debit_sub", "")).strip()
            sub_j = str(rj.get("debit_sub", "")).strip()
            if sub_i != sub_j and sub_i not in ("", "nan") and sub_j not in ("", "nan"):
                continue
            if pd.isna(ri["date"]) or pd.isna(rj["date"]):
                continue

            days_diff = abs((ri["date"] - rj["date"]).days)
            if days_diff > 7:
                continue

            # 摘要の類似度（簡易）
            desc_i = str(ri.get("description", ""))
            desc_j = str(rj.get("description", ""))
            similar = _simple_similarity(desc_i, desc_j)

            key = (min(i, j), max(i, j))
            if similar > 0.6 and key not in found_pairs:
                found_pairs.add(key)
                issues.append({
                    "level": "warning", "category": "5-2 重複仕訳",
                    "check_id": "5-2", "account": str(ri["debit_account"]),
                    "month": str(ri["date"].to_period("M")),
                    "message": (
                        f"【5-2・高】重複仕訳の疑いがあります: "
                        f"{ri['date'].date()} と {rj['date'].date()} に"
                        f"同額（{ri['debit_amount']:,.0f}円）・同科目（{ri['debit_account']}）の"
                        f"仕訳が {days_diff}日 以内に存在します。"
                        f"摘要: 「{desc_i[:20]}」vs「{desc_j[:20]}」"
                    ),
                })
    return issues


def _simple_similarity(s1: str, s2: str) -> float:
    """簡易文字列類似度（共通文字数比率）"""
    if not s1 or not s2:
        return 0.0
    s1, s2 = s1.lower(), s2.lower()
    if s1 == s2:
        return 1.0
    common = sum(1 for c in set(s1) if c in s2)
    return common / max(len(set(s1)), len(set(s2)), 1)


# ──────────────────────────────────────────────────────────
# 5-3: 金額の桁数ミス・逆仕訳
# ──────────────────────────────────────────────────────────
def _check_5_3_digit_error(df: pd.DataFrame) -> List[Dict[str, Any]]:
    issues = []
    target_accounts = ["水道光熱費", "通信費", "地代家賃", "保険料", "リース料"]

    for account in target_accounts:
        mask = df["debit_account"].astype(str).str.contains(account, na=False)
        entries = df[mask].copy()
        if entries.empty:
            continue

        period = df["date"].dt.to_period("M")
        monthly = entries.groupby(period)["debit_amount"].sum()
        if len(monthly) < 3:
            continue

        mean_val = monthly.mean()
        if mean_val == 0:
            continue

        for p, val in monthly.items():
            if val > mean_val * DIGIT_ERROR_MULTIPLIER and abs(val - mean_val) > 50000:
                issues.append({
                    "level": "warning", "category": "5-3 桁数ミス疑い",
                    "check_id": "5-3", "account": account,
                    "month": str(p),
                    "message": (
                        f"【5-3・中】{account} が {p} に {val:,.0f}円 と、"
                        f"平均（{mean_val:,.0f}円）の {val/mean_val:.1f}倍 になっています。"
                        "桁数ミスや二重計上の可能性があります。"
                    ),
                })
            elif val > 0 and val < mean_val / DIGIT_ERROR_MULTIPLIER and mean_val > 50000:
                issues.append({
                    "level": "warning", "category": "5-3 桁数ミス疑い",
                    "check_id": "5-3", "account": account,
                    "month": str(p),
                    "message": (
                        f"【5-3・中】{account} が {p} に {val:,.0f}円 と、"
                        f"平均（{mean_val:,.0f}円）の {val/mean_val*100:.0f}% に急減しています。"
                        "桁数ミスまたは計上漏れの可能性があります。"
                    ),
                })

    # 逆仕訳チェック（費用科目の貸方に大きな単発計上）
    expense_accounts = ["給料手当", "外注費", "広告宣伝費", "修繕費", "消耗品費"]
    for account in expense_accounts:
        cr_entries = df[
            df["credit_account"].astype(str).str.contains(account, na=False) &
            (df["credit_amount"] > 100000)
        ]
        for _, row in cr_entries.iterrows():
            desc = str(row.get("description", ""))
            # 正規の戻し仕訳キーワードを除外
            if any(kw in desc for kw in ["振替", "取消", "訂正", "修正", "戻し", "返還"]):
                continue
            issues.append({
                "level": "warning", "category": "5-3 逆仕訳",
                "check_id": "5-3R", "account": account,
                "month": str(row["date"].to_period("M")) if pd.notna(row["date"]) else "不明",
                "message": (
                    f"【5-3・中】費用科目「{account}」の貸方（逆仕訳）に {row['credit_amount']:,.0f}円 があります。"
                    "借貸の入力逆転や異常な戻し仕訳の可能性があります。"
                    f"摘要: 「{desc[:30]}」"
                ),
            })
    return issues


# ──────────────────────────────────────────────────────────
# 5-4: 交際費と会議費・福利厚生費の境界線
# ──────────────────────────────────────────────────────────
def _check_5_4_entertainment(df: pd.DataFrame) -> List[Dict[str, Any]]:
    issues = []

    # 会議費で1万円超
    kaigi = df[
        df["debit_account"].astype(str).str.contains("会議費", na=False) &
        (df["debit_amount"] > ENTERTAINMENT_UNIT_LIMIT)
    ]
    for _, row in kaigi.iterrows():
        issues.append({
            "level": "warning", "category": "5-4 交際費境界",
            "check_id": "5-4", "account": "会議費",
            "month": str(row["date"].to_period("M")) if pd.notna(row["date"]) else "不明",
            "message": (
                f"【5-4・中】会議費 {row['debit_amount']:,.0f}円 が1万円を超えています。"
                "1人あたり1万円超の飲食は交際費（損金不算入の可能性）となる場合があります。"
                "参加人数の確認が必要です。"
                + (f"（摘要: 「{desc_safe(row)}」）" if desc_safe(row) else "")
            ),
        })

    # 会議費・福利厚生費で交際費キーワード
    for acct in ["会議費", "福利厚生費"]:
        kw_entries = df[
            df["debit_account"].astype(str).str.contains(acct, na=False) &
            df.get("description", pd.Series(dtype=str)).astype(str).apply(
                lambda x: any(k in x for k in KW_ENTERTAINMENT)
            )
        ]
        for _, row in kw_entries.iterrows():
            issues.append({
                "level": "warning", "category": "5-4 交際費境界",
                "check_id": "5-4", "account": acct,
                "month": month_safe(row),
                "message": (
                    f"【5-4・中】{acct} の摘要「{desc_safe(row)}」に"
                    "贈答・ゴルフ等のキーワードが含まれています。"
                    "交際費（損金不算入リスク）への該当性を確認してください。"
                ),
            })
    return issues


# ──────────────────────────────────────────────────────────
# 5-5: 源泉所得税の徴収漏れ
# ──────────────────────────────────────────────────────────
def _check_5_5_withholding(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    支払手数料・外注費で個人報酬キーワードがあるのに、
    同一伝票内に預り金（源泉税）の貸方仕訳がない場合を検知
    """
    issues = []

    target = df[
        (
            df["debit_account"].astype(str).str.contains("支払手数料|外注費", na=False)
        ) &
        df.get("description", pd.Series(dtype=str)).astype(str).apply(
            lambda x: any(k in x for k in KW_WITHHOLDING)
        ) &
        (df["debit_amount"] > 50000)  # 少額は除外
    ]

    if target.empty:
        return issues

    # 同一伝票（slip_no）内に預り金の貸方があるか確認
    if "slip_no" in df.columns:
        withholding_slips = set(
            df[
                df["credit_account"].astype(str).str.contains("預り金|源泉", na=False)
            ]["slip_no"].dropna().astype(str)
        )
        missing = target[~target["slip_no"].astype(str).isin(withholding_slips)]
    else:
        # 同日の預り金仕訳があるか確認
        withholding_dates = set(
            df[
                df["credit_account"].astype(str).str.contains("預り金|源泉", na=False)
            ]["date"].dropna()
        )
        missing = target[~target["date"].isin(withholding_dates)]

    for _, row in missing.iterrows():
        issues.append({
            "level": "error", "category": "5-5 源泉徴収漏れ",
            "check_id": "5-5", "account": str(row["debit_account"]),
            "month": month_safe(row),
            "message": (
                f"【5-5・高】{row['debit_account']} {row['debit_amount']:,.0f}円"
                + (f"（摘要: 「{desc_safe(row)}」）" if desc_safe(row) else "")
                + "に個人報酬のキーワードが含まれていますが、"
                "同一伝票内に源泉所得税（預り金）の計上がありません。"
                "個人への報酬支払時は原則10.21%の源泉徴収が必要です。"
            ),
        })
    return issues
