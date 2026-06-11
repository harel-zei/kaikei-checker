"""
チェッカー共通ユーティリティ
"""
import pandas as pd


def desc_safe(row, max_len: int = 35) -> str:
    """摘要を安全に取得。nan/None は空文字に変換"""
    v = row.get("description", "")
    s = str(v).strip()
    if s.lower() in ("nan", "none", ""):
        return ""
    return s[:max_len]


def month_safe(row) -> str:
    """日付を月表示（YYYY-MM）に変換。NaT は '不明' に"""
    d = row.get("date")
    if d is None or (hasattr(d, "isnull") and pd.isnull(d)):
        return "不明"
    try:
        return str(pd.Timestamp(d).to_period("M"))
    except Exception:
        return "不明"


def date_safe(row) -> str:
    """
    日付を YYYY-MM-DD 形式で返す。
    Excel出力の日付欄で「2026年4月30日」のように日まで表示するために使用。
    NaT / None の場合は '不明' を返す。
    """
    d = row.get("date")
    if d is None or (hasattr(d, "isnull") and pd.isnull(d)):
        return "不明"
    try:
        ts = pd.Timestamp(d)
        return f"{ts.year}-{ts.month:02d}-{ts.day:02d}"
    except Exception:
        return "不明"


def slip_safe(row) -> str:
    """伝票番号（仕訳番号）を安全に取得。nan/None は空文字に変換"""
    v = row.get("slip_no", "")
    s = str(v).strip()
    if s.lower() in ("nan", "none", ""):
        return ""
    # "123.0" のような float 文字列を整数表記に
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


# 店舗名・住所と判断される除外ワード
# これらが摘要に含まれる場合、役所・行政キーワードでも誤検知しない
STORE_SUFFIXES = [
    "前店", "店", "支店", "営業所", "センター", "ショップ",
    "mall", "Mall", "モール", "プラザ", "Plaza",
    "ビル", "ホール", "会館",
]


def is_store_address(text: str, trigger_word: str) -> bool:
    """
    trigger_word が text の中でも「店舗名・住所の一部」として
    使われている可能性が高い場合 True を返す。
    例: '市役所前店' → True  / '市役所 収入印紙' → False
    """
    if trigger_word not in text:
        return False
    idx = text.find(trigger_word)
    after = text[idx + len(trigger_word):]
    # トリガーワードの直後に店舗を示す語句がある場合は除外
    for suffix in STORE_SUFFIXES:
        if after.startswith(suffix) or after.lstrip().startswith(suffix):
            return True
    return False
