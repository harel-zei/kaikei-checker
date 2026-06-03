"""
アップロードされたファイルを自動判定して6スロットに振り分けるモジュール。

判定の流れ:
1. ファイル種別を判定（仕訳帳 / 試算表主科目 / 補助残高）
2. 各種別ごとに「当期・前期」を日付で判定（新しい方が当期）
"""
import re
import io
import pandas as pd
from typing import Optional


# ── ファイル種別 ──────────────────────────────
FILE_TYPE_JOURNAL      = "journal"       # 仕訳帳
FILE_TYPE_BALANCE_MAIN = "balance_main"  # 試算表（主科目）
FILE_TYPE_BALANCE_SUB  = "balance_sub"   # 補助残高一覧

# ── 当期 / 前期 ────────────────────────────────
PERIOD_CURRENT = "current"
PERIOD_PRIOR   = "prior"


def detect_file_type(content: str) -> str:
    """ファイルの内容からファイル種別を判定する"""
    head = content[:2000]

    # 弥生仕訳帳（ヘッダーなし独自形式）
    if re.search(r'"21\d\d",\d+,"","R\.\d{2}/\d{2}/\d{2}"', head):
        return FILE_TYPE_JOURNAL

    # 弥生 補助残高一覧表
    if "補助残高一覧表" in head or "[貸借科目]" in head:
        return FILE_TYPE_BALANCE_SUB

    # 弥生 残高試算表（主科目）
    if "残高試算表" in head or "[貸借対照表]" in head or "[損益計算書]" in head:
        return FILE_TYPE_BALANCE_MAIN

    # freee / MF の仕訳帳
    if "発生日" in head and "借方勘定科目" in head:
        return FILE_TYPE_JOURNAL
    if "借方科目" in head and "貸方科目" in head:
        return FILE_TYPE_JOURNAL

    # デフォルトは仕訳帳として試みる
    return FILE_TYPE_JOURNAL


def extract_period_date(content: str, file_type: str) -> Optional[pd.Timestamp]:
    """
    ファイルが対象としている期間の代表日付を返す。
    - 仕訳帳: 最新の取引日付
    - 試算表・補助残高: ヘッダーの集計期間（終了日）
    """
    if file_type == FILE_TYPE_JOURNAL:
        return _latest_journal_date(content)
    else:
        return _balance_period_date(content)


def _latest_journal_date(content: str) -> Optional[pd.Timestamp]:
    """仕訳帳から最新の日付を抽出する"""
    dates = []
    # 弥生形式: R.07/12/01
    for m in re.finditer(r'R\.(\d{2})/(\d{2})/(\d{2})', content):
        try:
            ts = pd.Timestamp(
                year=int(m.group(1)) + 2018,
                month=int(m.group(2)),
                day=int(m.group(3))
            )
            dates.append(ts)
        except Exception:
            pass
    # 通常形式: YYYY/MM/DD
    for m in re.finditer(r'(\d{4})/(\d{2})/(\d{2})', content):
        try:
            dates.append(pd.Timestamp(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except Exception:
            pass
    return max(dates) if dates else None


def _balance_period_date(content: str) -> Optional[pd.Timestamp]:
    """
    試算表・補助残高ファイルのヘッダーから集計期間（終了日）を抽出する。
    例: "集計期間","令和07年12月01日","令和07年12月31日"
    """
    # 令和日付
    m = re.search(r'令和(\d+)年(\d+)月(\d+)日.*?令和(\d+)年(\d+)月(\d+)日', content[:500])
    if m:
        try:
            # 2番目の日付（終了日）を使用
            year  = int(m.group(4)) + 2018
            month = int(m.group(5))
            day   = int(m.group(6))
            return pd.Timestamp(year=year, month=month, day=day)
        except Exception:
            pass

    # 数値日付
    m = re.search(r'(\d{4})/(\d{2})/(\d{2}).*?(\d{4})/(\d{2})/(\d{2})', content[:500])
    if m:
        try:
            return pd.Timestamp(int(m.group(4)), int(m.group(5)), int(m.group(6)))
        except Exception:
            pass

    return None


def auto_classify_files(files: list[tuple[str, str]]) -> dict:
    """
    複数ファイルを受け取り、6スロットに自動振り分けする。

    Args:
        files: [(filename, content), ...]

    Returns:
        {
            "journal_current":      content or None,
            "journal_prior":        content or None,
            "balance_main_current": content or None,
            "balance_main_prior":   content or None,
            "balance_sub_current":  content or None,
            "balance_sub_prior":    content or None,
            "log": [振り分けログ],
        }
    """
    result = {
        "journal_current":      None,
        "journal_prior":        None,
        "balance_main_current": None,
        "balance_main_prior":   None,
        "balance_sub_current":  None,
        "balance_sub_prior":    None,
        "log": [],
    }

    # 各ファイルの種別と代表日付を判定
    classified = []
    for filename, content in files:
        ftype = detect_file_type(content)
        fdate = extract_period_date(content, ftype)
        classified.append({
            "filename": filename,
            "content":  content,
            "type":     ftype,
            "date":     fdate,
        })
        result["log"].append(
            f"📄 {filename} → 種別:{_type_label(ftype)} 日付:{fdate.strftime('%Y/%m') if fdate else '不明'}"
        )

    # 種別ごとにグループ化し、日付で当期/前期を割り当てる
    for ftype in [FILE_TYPE_JOURNAL, FILE_TYPE_BALANCE_MAIN, FILE_TYPE_BALANCE_SUB]:
        group = [f for f in classified if f["type"] == ftype]
        if not group:
            continue

        # 日付でソート（新しい順）
        group_with_date  = [f for f in group if f["date"] is not None]
        group_no_date    = [f for f in group if f["date"] is None]
        group_with_date.sort(key=lambda f: f["date"], reverse=True)

        # 日付あり: 最新=当期, 2番目=前期
        combined = group_with_date + group_no_date
        if len(combined) >= 1:
            _assign(result, ftype, PERIOD_CURRENT, combined[0])
        if len(combined) >= 2:
            _assign(result, ftype, PERIOD_PRIOR,   combined[1])
        if len(combined) > 2:
            for extra in combined[2:]:
                result["log"].append(
                    f"⚠️ {extra['filename']} は同種別3ファイル目のため無視されました"
                )

    return result


def _assign(result: dict, ftype: str, period: str, f: dict) -> None:
    key = f"{ftype}_{period}"
    result[key] = f["content"]
    label = "当期" if period == PERIOD_CURRENT else "前期"
    result["log"].append(f"  ✅ {f['filename']} → {_type_label(ftype)}（{label}）に割り当て")


def _type_label(ftype: str) -> str:
    return {
        FILE_TYPE_JOURNAL:      "仕訳帳",
        FILE_TYPE_BALANCE_MAIN: "試算表（主科目）",
        FILE_TYPE_BALANCE_SUB:  "補助残高一覧",
    }.get(ftype, ftype)
