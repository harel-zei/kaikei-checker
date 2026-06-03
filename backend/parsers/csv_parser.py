"""
会計ソフト別CSVパーサー
対応: 弥生会計, freee, MoneyForward
"""
import pandas as pd
import io
from typing import Optional


def detect_software(content: str) -> str:
    """CSVの内容から会計ソフトを自動判定"""
    if "借方勘定科目" in content and "貸方勘定科目" in content and "弥生" in content:
        return "yayoi"
    if "借方勘定科目" in content and "貸方勘定科目" in content:
        return "yayoi"
    if "取引No" in content and "借方金額" in content:
        return "freee"
    if "仕訳番号" in content and "借方科目" in content:
        return "moneyforward"
    # ヘッダー行で判定
    first_line = content.split("\n")[0]
    if "freee" in content.lower():
        return "freee"
    if "勘定科目コード" in first_line:
        return "moneyforward"
    return "generic"


def parse_yayoi(content: str) -> pd.DataFrame:
    """弥生会計CSVをパース（仕訳日記帳形式）"""
    lines = content.split("\n")
    # 弥生はヘッダー行が複数あることがある
    header_idx = 0
    for i, line in enumerate(lines):
        if "日付" in line and "借方勘定科目" in line:
            header_idx = i
            break

    data_lines = lines[header_idx:]
    df = pd.read_csv(io.StringIO("\n".join(data_lines)), encoding="utf-8-sig", on_bad_lines="skip")

    # カラム名を統一フォーマットに変換
    col_map = {
        "日付": "date",
        "借方勘定科目": "debit_account",
        "借方補助科目": "debit_sub",
        "借方税区分": "debit_tax",
        "借方金額": "debit_amount",
        "貸方勘定科目": "credit_account",
        "貸方補助科目": "credit_sub",
        "貸方税区分": "credit_tax",
        "貸方金額": "credit_amount",
        "摘要": "description",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    return _normalize(df)


def parse_freee(content: str) -> pd.DataFrame:
    """freee CSVをパース"""
    df = pd.read_csv(io.StringIO(content), encoding="utf-8-sig", on_bad_lines="skip")
    col_map = {
        "発生日": "date",
        "借方勘定科目": "debit_account",
        "借方補助科目名称": "debit_sub",
        "借方税区分": "debit_tax",
        "借方金額": "debit_amount",
        "貸方勘定科目": "credit_account",
        "貸方補助科目名称": "credit_sub",
        "貸方税区分": "credit_tax",
        "貸方金額": "credit_amount",
        "備考": "description",
        "摘要": "description",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    return _normalize(df)


def parse_moneyforward(content: str) -> pd.DataFrame:
    """MoneyForward クラウド会計CSVをパース"""
    df = pd.read_csv(io.StringIO(content), encoding="utf-8-sig", on_bad_lines="skip")
    col_map = {
        "日付": "date",
        "借方科目": "debit_account",
        "借方補助科目": "debit_sub",
        "借方税区分": "debit_tax",
        "借方金額": "debit_amount",
        "貸方科目": "credit_account",
        "貸方補助科目": "credit_sub",
        "貸方税区分": "credit_tax",
        "貸方金額": "credit_amount",
        "摘要": "description",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    return _normalize(df)


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """データフレームを共通形式に正規化"""
    required = ["date", "debit_account", "debit_amount", "credit_account", "credit_amount"]
    for col in required:
        if col not in df.columns:
            df[col] = None

    # 金額列を数値に変換
    for col in ["debit_amount", "credit_amount"]:
        if col in df.columns:
            df[col] = pd.to_numeric(
                df[col].astype(str).str.replace(",", "").str.replace("　", "").str.strip(),
                errors="coerce"
            ).fillna(0)

    # 日付を変換
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")

    # 空行を除去
    df = df.dropna(subset=["debit_account", "credit_account"], how="all")
    df = df[df["debit_account"].astype(str).str.strip() != ""]

    return df.reset_index(drop=True)


def parse_csv(content: str, encoding: str = "utf-8") -> tuple[pd.DataFrame, str]:
    """
    CSVを自動判定してパース
    Returns: (DataFrame, software_name)
    """
    software = detect_software(content)

    parsers = {
        "yayoi": parse_yayoi,
        "freee": parse_freee,
        "moneyforward": parse_moneyforward,
        "generic": parse_yayoi,  # フォールバック
    }

    parser = parsers.get(software, parse_yayoi)
    df = parser(content)
    return df, software
