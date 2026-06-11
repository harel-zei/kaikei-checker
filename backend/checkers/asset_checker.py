"""
カテゴリ3: 資産・修繕費の判定チェック
3-1: 中小企業特例（少額資産）の適用漏れ（10万〜30万）
3-2: 10万円未満の資産計上（費用化漏れ）
3-3: 修繕費の資産計上漏れ（20万円以上）
"""
import pandas as pd
from typing import List, Dict, Any
from checkers.check_utils import desc_safe, month_safe, date_safe, slip_safe

# 固定資産科目
FIXED_ASSET_ACCOUNTS = [
    "工具器具備品", "機械装置", "車両運搬具", "建物", "構築物",
    "建設仮勘定", "土地", "リース資産", "ソフトウェア",
    "無形固定資産",
]

# 固定資産科目に部分一致するが実際はPL科目（除外対象）
FIXED_ASSET_EXCLUDE = [
    "ソフトウェア使用費", "ソフトウェア償却費", "ソフトウェア償却",
    "建物管理費", "建物清掃費",
]

# 修繕費科目
REPAIR_ACCOUNTS = ["修繕費"]

THRESHOLD_EXPENSE  = 100_000   # 10万円未満 → 費用化すべき
THRESHOLD_SME_MAX  = 300_000   # 30万円未満 → 中小特例（少額減価償却資産）
THRESHOLD_REPAIR   = 200_000   # 20万円以上 → 資産計上の可能性


def check_assets(df: pd.DataFrame) -> List[Dict[str, Any]]:
    issues = []
    issues.extend(_check_3_1_sme_deduction(df))
    issues.extend(_check_3_2_under_threshold(df))
    issues.extend(_check_3_3_repair_capitalization(df))
    return issues


# ──────────────────────────────────────────────────────────
# 3-1: 中小企業特例（少額資産）の適用漏れ（10万〜30万）
# ──────────────────────────────────────────────────────────
def _check_3_1_sme_deduction(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    固定資産科目に 10万〜30万の計上がある場合、
    中小企業特例による即時費用化の可能性をアラート
    """
    issues = []
    acc_mask = df["debit_account"].fillna("").astype(str).apply(
        lambda x: bool(x)
                  and any(a in x for a in FIXED_ASSET_ACCOUNTS)
                  and not any(e in x for e in FIXED_ASSET_EXCLUDE)
    )
    asset_entries = df[acc_mask].copy()
    if asset_entries.empty:
        return issues

    # 同一伝票内の合算金額で判定
    if "slip_no" in df.columns:
        slip_totals = asset_entries.groupby(
            [asset_entries["date"].dt.to_period("M"), "slip_no"]
        )["debit_amount"].sum()
        for (period, slip), total in slip_totals.items():
            if THRESHOLD_EXPENSE <= total < THRESHOLD_SME_MAX:
                sample = asset_entries[asset_entries["slip_no"] == str(slip)].iloc[0]
                issues.append({
                    "level": "warning", "category": "3-1 少額資産特例",
                    "check_id": "3-1", "account": str(sample["debit_account"]),
                    "month": str(period), "slip": str(slip),
                    "message": (
                        f"【3-1・中】伝票No.{slip}の固定資産計上額が {total:,.0f}円（10万〜30万）です。"
                        "中小企業の少額減価償却資産の特例（年間300万円まで即時損金算入）の"
                        "適用を検討してください。"
                    ),
                })
    else:
        # 伝票番号なし→単行判定
        target = asset_entries[
            (asset_entries["debit_amount"] >= THRESHOLD_EXPENSE) &
            (asset_entries["debit_amount"] < THRESHOLD_SME_MAX)
        ]
        for _, row in target.iterrows():
            issues.append({
                "level": "warning", "category": "3-1 少額資産特例",
                "check_id": "3-1", "account": str(row["debit_account"]),
                "month": str(row["date"].to_period("M")) if pd.notna(row["date"]) else "不明",
                "message": (
                    f"【3-1・中】固定資産計上額 {row['debit_amount']:,.0f}円 は10万〜30万の範囲です。"
                    "中小企業特例（少額減価償却資産）による即時費用化を検討してください。"
                ),
            })
    return issues


# ──────────────────────────────────────────────────────────
# 3-2: 10万円未満の資産計上（費用化漏れ）
# ──────────────────────────────────────────────────────────
def _check_3_2_under_threshold(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """固定資産科目に10万円未満の計上→消耗品費等で費用化すべき"""
    issues = []
    acc_mask = df["debit_account"].fillna("").astype(str).apply(
        lambda x: bool(x)
                  and any(a in x for a in FIXED_ASSET_ACCOUNTS)
                  and not any(e in x for e in FIXED_ASSET_EXCLUDE)
    )
    target = df[acc_mask & (df["debit_amount"] > 0) & (df["debit_amount"] < THRESHOLD_EXPENSE)]

    for _, row in target.iterrows():
        d = desc_safe(row)
        desc_part = f"（摘要: {d}）" if d else ""
        issues.append({
            "level": "error", "category": "3-2 少額資産費用化",
            "check_id": "3-2", "account": str(row["debit_account"]),
            "month": date_safe(row), "slip": slip_safe(row),
            "message": (
                f"【3-2・高】固定資産科目「{row['debit_account']}」に"
                f"{row['debit_amount']:,.0f}円（10万円未満）の計上があります{desc_part}。"
                "10万円未満の物品は消耗品費等で費用処理（損金算入）すべきです。"
            ),
        })
    return issues


# ──────────────────────────────────────────────────────────
# 3-3: 修繕費の資産計上漏れ（20万円以上）
# ──────────────────────────────────────────────────────────
def _check_3_3_repair_capitalization(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """修繕費に20万円以上の計上→資本的支出として固定資産計上の可能性"""
    issues = []
    acc_mask = df["debit_account"].fillna("").astype(str).apply(
        lambda x: isinstance(x, str) and x not in ("nan", "None", "") and any(a in x for a in REPAIR_ACCOUNTS)
    )
    repair_entries = df[acc_mask].copy()
    if repair_entries.empty:
        return issues

    # 単行 + 同一伝票合算の両方で判定
    checked_slips = set()

    if "slip_no" in df.columns:
        slip_totals = repair_entries.groupby(
            [repair_entries["date"].dt.to_period("M"), "slip_no"]
        )["debit_amount"].sum()
        for (period, slip), total in slip_totals.items():
            if total >= THRESHOLD_REPAIR:
                checked_slips.add(str(slip))
                sample = repair_entries[repair_entries["slip_no"] == str(slip)].iloc[0]
                issues.append({
                    "level": "warning", "category": "3-3 修繕費資本的支出",
                    "check_id": "3-3", "account": "修繕費",
                    "month": str(period), "slip": str(slip),
                    "message": (
                        f"【3-3・高】伝票No.{slip}の修繕費合計が {total:,.0f}円 に達しています。"
                        "20万円以上の修繕工事は、現状回復目的か価値向上目的かを確認し、"
                        "資本的支出に該当する場合は固定資産として計上が必要です。"
                        + (f"（摘要: {desc_safe(sample)}）" if desc_safe(sample) else "")
                    ),
                })

    # 伝票番号のない行の単行チェック
    single = repair_entries[
        (repair_entries["debit_amount"] >= THRESHOLD_REPAIR) &
        ~repair_entries.get("slip_no", pd.Series(dtype=str)).isin(checked_slips)
    ]
    for _, row in single.iterrows():
        issues.append({
            "level": "warning", "category": "3-3 修繕費資本的支出",
            "check_id": "3-3", "account": "修繕費",
            "month": str(row["date"].to_period("M")) if pd.notna(row["date"]) else "不明",
            "message": (
                f"【3-3・高】修繕費に {row['debit_amount']:,.0f}円 の計上があります。"
                "20万円以上の修繕は資本的支出として固定資産計上が必要な場合があります。"
                + (f"（摘要: {desc_safe(row)}）" if desc_safe(row) else "")
            ),
        })
    return issues
