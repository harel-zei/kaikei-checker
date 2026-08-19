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

from parsers.jdl_parser import (
    is_jdl, is_jdl_balance, is_jdl_balance_sub, jdl_period_date,
)


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

    # JDL会計（＜商号Ｃ＞で始まる固定ヘッダー）。
    # 「合計残高試算表」「借方科目」等の語を含むため、他の判定より先に見る。
    if is_jdl(content):
        if is_jdl_balance_sub(content):
            return FILE_TYPE_BALANCE_SUB
        if is_jdl_balance(content):
            return FILE_TYPE_BALANCE_MAIN
        return FILE_TYPE_JOURNAL

    # 弥生仕訳帳（ヘッダーなし独自形式）
    if re.search(r'"21\d\d",\d+,"","R\.\d{2}/\d{2}/\d{2}"', head):
        return FILE_TYPE_JOURNAL

    # 弥生 補助残高一覧表
    if "補助残高一覧表" in head or "[貸借科目]" in head:
        return FILE_TYPE_BALANCE_SUB

    # 弥生 残高試算表（主科目）
    if "残高試算表" in head or "[貸借対照表]" in head or "[損益計算書]" in head:
        return FILE_TYPE_BALANCE_MAIN

    # freee 試算表（損益計算書・貸借対照表・製造原価報告書）
    # ファイル名や1行目に「試算表：」が含まれる
    if "試算表：" in head:
        return FILE_TYPE_BALANCE_MAIN

    # freee 仕訳帳（新）CSV: "No","取引日","管理番号","借方勘定科目" 形式
    if '"取引日"' in head and '"借方勘定科目"' in head and '"貸方勘定科目"' in head:
        return FILE_TYPE_JOURNAL

    # freee / MF の仕訳帳（旧形式）
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
    # JDLは仕訳・試算表とも先頭の「＜決算年月日＞」が期の識別子になる
    if is_jdl(content):
        return jdl_period_date(content)
    if file_type == FILE_TYPE_JOURNAL:
        return _latest_journal_date(content)
    else:
        return _balance_period_date(content)


def _latest_journal_date(content: str) -> Optional[pd.Timestamp]:
    """仕訳帳から最新の日付を抽出する"""
    dates = []
    # 弥生ヘッダーの「処理日時」「集計期間」等は取引日ではないため除外する
    scan = "\n".join(
        ln for ln in content.splitlines()
        if not any(h in ln for h in ("処理日時", "集計期間", "検索条件", "出力日"))
    )
    # 和暦形式: 令和07年05月01日（弥生・和暦書き出し）
    _ERA = {"令和": 2018, "平成": 1988, "昭和": 1925}
    for m in re.finditer(r'(令和|平成|昭和)(\d{1,2})年(\d{1,2})月(\d{1,2})日', scan):
        try:
            base = _ERA[m.group(1)]
            dates.append(pd.Timestamp(base + int(m.group(2)), int(m.group(3)), int(m.group(4))))
        except Exception:
            pass
    # 弥生形式: R.07/12/01
    for m in re.finditer(r'R\.(\d{2})/(\d{2})/(\d{2})', scan):
        try:
            ts = pd.Timestamp(
                year=int(m.group(1)) + 2018,
                month=int(m.group(2)),
                day=int(m.group(3))
            )
            dates.append(ts)
        except Exception:
            pass
    # 通常形式: YYYY/MM/DD（弥生ヘッダーあり）
    for m in re.finditer(r'(\d{4})/(\d{2})/(\d{2})', scan):
        try:
            dates.append(pd.Timestamp(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except Exception:
            pass
    # freee形式: YYYY-MM-DD（ハイフン区切り）
    for m in re.finditer(r'(\d{4})-(\d{2})-(\d{2})', scan[:50000]):  # 先頭50KB のみ走査
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
                dates.append(pd.Timestamp(y, mo, d))
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

    # freee 試算表: "（期間：2025年09月～2025年12月" の形式
    m = re.search(r'(\d{4})年(\d{2})月.*?(\d{4})年(\d{2})月', content[:300])
    if m:
        try:
            return pd.Timestamp(year=int(m.group(3)), month=int(m.group(4)), day=28)
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

    # 仕訳帳の中で最も新しい（当期）仕訳帳の日付を特定
    journal_files = sorted(
        [f for f in classified if f["type"] == FILE_TYPE_JOURNAL and f["date"]],
        key=lambda f: f["date"], reverse=True
    )
    current_journal_date = journal_files[0]["date"] if journal_files else None

    # 種別ごとにグループ化し、当期/前期を割り当てる
    for ftype in [FILE_TYPE_JOURNAL, FILE_TYPE_BALANCE_MAIN, FILE_TYPE_BALANCE_SUB]:
        group = [f for f in classified if f["type"] == ftype]
        if not group:
            continue

        group_with_date = [f for f in group if f["date"] is not None]
        group_no_date   = [f for f in group if f["date"] is None]
        group_with_date.sort(key=lambda f: f["date"], reverse=True)

        if ftype == FILE_TYPE_JOURNAL:
            # 仕訳帳: 最新=当期, 2番目=前期（従来通り）
            combined = group_with_date + group_no_date
            if len(combined) >= 1:
                _assign(result, ftype, PERIOD_CURRENT, combined[0])
            if len(combined) >= 2:
                _assign(result, ftype, PERIOD_PRIOR, combined[1])

        else:
            # 残高ファイル: 複数ある場合は全てマージして使用
            combined = group_with_date + group_no_date

            # 全ファイルが前期か当期かを判定
            prior_files   = []
            current_files = []
            for f in combined:
                if (current_journal_date and f.get("date") and
                        f["date"] < current_journal_date):
                    prior_files.append(f)
                else:
                    current_files.append(f)

            # 前期ファイルが複数ある場合はコンテンツをマージ
            if prior_files:
                merged = _merge_file_contents(prior_files)
                result[f"{ftype}_{PERIOD_PRIOR}"] = merged
                names = [f["filename"] for f in prior_files]
                result["log"].append(
                    f"  ✅ 前期残高ファイル {len(prior_files)}件 をマージ: {', '.join(names)}"
                )
            if current_files:
                merged = _merge_file_contents(current_files)
                result[f"{ftype}_{PERIOD_CURRENT}"] = merged
                names = [f["filename"] for f in current_files]
                result["log"].append(
                    f"  ✅ 当期残高ファイル {len(current_files)}件 をマージ: {', '.join(names)}"
                )
            # 日付なしで分類できなかった場合
            if not prior_files and not current_files and combined:
                _assign(result, ftype, PERIOD_CURRENT, combined[0])

    return result


def _merge_file_contents(files: list) -> str:
    """複数の残高ファイルのコンテンツを改行で結合してマージする"""
    return "\n".join(f["content"] for f in files if f.get("content"))


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
