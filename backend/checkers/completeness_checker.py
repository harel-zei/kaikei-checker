"""
カテゴリ1: 網羅性・期間帰属・部門推移のチェック
1-1: 定例費用の発生漏れ
1-2: 月末曜日ズレによる期間帰属エラー
1-3: 部門別損益の発生推移・異常値
"""
import pandas as pd
import numpy as np
from typing import List, Dict, Any


def get_fiscal_period(date: pd.Timestamp, cutoff_day: int = 1) -> pd.Period:
    """
    締め日に基づいて仕訳日付が属する「会計月」を返す。

    例: cutoff_day=20（20日締め）
      - 12月21日〜1月20日 → 「12月度」（前月）
      - 1月21日〜2月20日 → 「1月度」

    cutoff_day=1（月末締め）の場合は通常の暦月と同じ。
    """
    if cutoff_day <= 1:
        return date.to_period("M")
    if date.day <= cutoff_day:
        # この日付は「前月度」に属する
        y, m = date.year, date.month - 1
        if m == 0:
            m, y = 12, y - 1
        return pd.Period(year=y, month=m, freq="M")
    else:
        return date.to_period("M")

# 1-1: 毎月必ず発生すべき定例科目
RECURRING_ACCOUNTS = [
    "地代家賃", "賃借料", "リース料",
    "減価償却費",
    "賞与引当金", "退職給付費用",
    "法定福利費",
]

# 1-2: 月末曜日ズレチェック対象科目
MONTH_END_ACCOUNTS = [
    "法定福利費", "給料手当", "役員報酬", "賞与", "地代家賃",
]

# 1-3: 部門損益チェック対象（PL科目キーワード）
PL_KEYWORDS = [
    "売上", "仕入", "給料", "外注", "地代", "広告", "水道",
    "通信", "保険", "修繕", "消耗", "旅費",
]


def check_completeness(df: pd.DataFrame, fiscal_cutoff_day: int = 1) -> List[Dict[str, Any]]:
    issues = []
    issues.extend(_check_1_1_recurring(df, fiscal_cutoff_day))
    issues.extend(_check_1_2_month_end_timing(df))
    issues.extend(_check_1_3_dept_anomaly(df))
    return issues


# ──────────────────────────────────────────────────────────
# 1-1: 定例費用の発生漏れ
# ──────────────────────────────────────────────────────────
def _check_1_1_recurring(df: pd.DataFrame, fiscal_cutoff_day: int = 1) -> List[Dict[str, Any]]:
    """
    定例費用の発生漏れチェック。
    fiscal_cutoff_day: 月次締め日。20なら「12/21〜1/20」を1つの会計月として集計。
    """
    issues = []
    if df["date"].dropna().empty:
        return issues

    # 締め日に基づく会計月を計算
    df = df.copy()
    df["_fiscal_period"] = df["date"].apply(
        lambda d: get_fiscal_period(d, fiscal_cutoff_day) if pd.notna(d) else pd.NaT
    )

    all_periods = sorted(df["_fiscal_period"].dropna().unique())
    if not all_periods:
        return issues

    for account in RECURRING_ACCOUNTS:
        mask = df["debit_account"].astype(str).str.contains(account, na=False)
        entries = df[mask]
        if entries.empty:
            continue

        monthly = entries.groupby(entries["_fiscal_period"])["debit_amount"].sum()
        missing = [p for p in all_periods if p not in monthly.index or monthly[p] == 0]

        if missing:
            s = ", ".join(str(m) for m in missing[:3])
            suffix = f"（他{len(missing)-3}ヶ月）" if len(missing) > 3 else ""
            period_note = f"（締め日:{fiscal_cutoff_day}日基準）" if fiscal_cutoff_day > 1 else ""
            issues.append({
                "level": "error", "category": "1-1 定例費用漏れ",
                "check_id": "1-1", "account": account, "month": s,
                "message": (
                    f"【1-1・高】{account} が計上されていない月があります: {s}{suffix}{period_note}。"
                    "毎月発生すべき定例費用のため、計上漏れまたは科目誤りの可能性があります。"
                ),
            })
    return issues


# ──────────────────────────────────────────────────────────
# 1-2: 月末曜日ズレによる期間帰属エラー
# ──────────────────────────────────────────────────────────
def _check_1_2_month_end_timing(df: pd.DataFrame) -> List[Dict[str, Any]]:
    issues = []

    for account in MONTH_END_ACCOUNTS:
        mask = df["debit_account"].astype(str).str.contains(account, na=False)
        entries = df[mask].copy()
        if entries.empty:
            continue

        all_periods = pd.period_range(
            start=entries["date"].dropna().min().to_period("M"),
            end=entries["date"].dropna().max().to_period("M"),
            freq="M"
        )

        for period in all_periods:
            # 当月の末（28〜31日）に仕訳があるか
            month_entries = entries[entries["date"].dt.to_period("M") == period]
            late_month = month_entries[month_entries["date"].dt.day >= 28]

            # 翌月初（1〜3日）に仕訳があるか
            next_period = period + 1
            next_entries = entries[entries["date"].dt.to_period("M") == next_period]
            early_next = next_entries[next_entries["date"].dt.day <= 3]

            if late_month.empty and not early_next.empty:
                issues.append({
                    "level": "warning", "category": "1-2 期間帰属",
                    "check_id": "1-2", "account": account,
                    "month": str(period),
                    "message": (
                        f"【1-2・中】{account}: {period} の月末（28〜31日）に仕訳がなく、"
                        f"{next_period} 月初（1〜3日）に {len(early_next)}件 の仕訳があります。"
                        "月末が土日祝日の場合に未払計上が翌月にずれている可能性があります。"
                    ),
                })
    return issues


# ──────────────────────────────────────────────────────────
# 1-3: 部門別損益の発生推移・異常値チェック
# ──────────────────────────────────────────────────────────
def _check_1_3_dept_anomaly(df: pd.DataFrame) -> List[Dict[str, Any]]:
    issues = []

    # (C) 部門未設定チェック：損益科目なのに部門コードが空
    if "debit_sub" in df.columns:
        for kw in PL_KEYWORDS:
            pl_rows = df[
                df["debit_account"].astype(str).str.contains(kw, na=False)
            ]
            no_dept = pl_rows[
                pl_rows["debit_sub"].isna() |
                (pl_rows["debit_sub"].astype(str).str.strip() == "") |
                (pl_rows["debit_sub"].astype(str).str.strip() == "nan")
            ]
            if len(no_dept) > 5:
                issues.append({
                    "level": "info", "category": "1-3 部門未設定",
                    "check_id": "1-3C", "account": kw + "系科目",
                    "month": "全期間",
                    "message": (
                        f"【1-3(C)・高】{kw}系科目で部門コードが未設定の仕訳が {len(no_dept)}件 あります。"
                        "共通費の配賦漏れや、入力時の部門コード選択ミスの可能性があります。"
                    ),
                })
                break  # キーワードごとに1件まで

    # (A)(B) 月次推移の急変チェック
    for kw in ["売上", "仕入", "給料", "外注"]:
        mask = df["debit_account"].astype(str).str.contains(kw, na=False) | \
               df["credit_account"].astype(str).str.contains(kw, na=False)
        entries = df[mask]
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
            # (A) 急減（前3ヶ月平均の20%以下）
            idx = monthly.index.get_loc(p)
            if idx >= 3:
                prev_avg = monthly.iloc[idx-3:idx].mean()
                if prev_avg > 0 and val < prev_avg * 0.2 and prev_avg > 100000:
                    issues.append({
                        "level": "warning", "category": "1-3 急減",
                        "check_id": "1-3A", "account": kw + "系科目",
                        "month": str(p),
                        "message": (
                            f"【1-3(A)・高】{kw}系科目が {p} に前3ヶ月平均比 "
                            f"{val/prev_avg*100:.0f}% と急減しています"
                            f"（平均 {prev_avg:,.0f}円 → {val:,.0f}円）。計上漏れの可能性があります。"
                        ),
                    })
            # (B) 急増（前3ヶ月平均の3倍以上）
            if idx >= 3:
                prev_avg = monthly.iloc[idx-3:idx].mean()
                if prev_avg > 0 and val > prev_avg * 3 and abs(val - prev_avg) > 100000:
                    issues.append({
                        "level": "warning", "category": "1-3 急増",
                        "check_id": "1-3B", "account": kw + "系科目",
                        "month": str(p),
                        "message": (
                            f"【1-3(B)・高】{kw}系科目が {p} に前3ヶ月平均比 "
                            f"{val/prev_avg*100:.0f}% と急増しています"
                            f"（平均 {prev_avg:,.0f}円 → {val:,.0f}円）。誤入力の可能性があります。"
                        ),
                    })

    return issues
