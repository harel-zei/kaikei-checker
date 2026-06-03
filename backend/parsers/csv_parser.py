"""
会計ソフト別CSVパーサー
対応: 弥生会計（ヘッダーなし独自形式）, freee, MoneyForward
"""
import pandas as pd
import io
import re
from typing import Optional


def detect_software(content: str) -> str:
    """CSVの内容から会計ソフトを自動判定"""
    first_lines = "\n".join(content.split("\n")[:5])

    # 弥生会計独自形式: 先頭が "2110" などの数値コード、令和日付 R.XX/XX/XX
    if re.search(r'"21\d\d",\d+,"","R\.\d{2}/\d{2}/\d{2}"', first_lines):
        return "yayoi_raw"

    # 弥生会計ヘッダーあり
    if "借方勘定科目" in content and "貸方勘定科目" in content:
        return "yayoi"

    # freee
    if "取引No" in content or ("発生日" in content and "借方勘定科目" in content):
        return "freee"

    # MoneyForward
    if "仕訳番号" in content or ("借方科目" in content and "貸方科目" in content):
        return "moneyforward"

    return "yayoi_raw"


def parse_reiwa_date(date_str: str) -> Optional[pd.Timestamp]:
    """令和日付 R.07/12/01 → pandas Timestamp に変換"""
    date_str = date_str.strip().strip('"')
    m = re.match(r'R\.(\d{2})/(\d{2})/(\d{2})', date_str)
    if m:
        reiwa_year = int(m.group(1))
        month = int(m.group(2))
        day = int(m.group(3))
        western_year = reiwa_year + 2018  # 令和1年 = 2019年
        try:
            return pd.Timestamp(year=western_year, month=month, day=day)
        except Exception:
            return None
    # 通常の日付も試みる
    try:
        return pd.to_datetime(date_str, errors="coerce")
    except Exception:
        return None


def parse_yayoi_raw(content: str) -> pd.DataFrame:
    """
    弥生会計ヘッダーなし独自形式をパース
    列構成:
      [0]  コード
      [1]  伝票番号
      [2]  (空)
      [3]  日付 R.07/12/01
      [4]  借方勘定科目
      [5]  借方補助科目
      [6]  (空)
      [7]  借方税区分
      [8]  借方金額
      [9]  借方消費税額
      [10] 貸方勘定科目
      [11] 貸方補助科目
      [12] (空)
      [13] 貸方税区分
      [14] 貸方金額
      [15] 貸方消費税額
      [16] 摘要
    """
    rows = []
    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue
        # CSVパース（クォートを考慮）
        try:
            parts = next(pd.read_csv(
                io.StringIO(line), header=None, quotechar='"'
            ).itertuples(index=False))
            parts = list(parts)
        except Exception:
            continue

        if len(parts) < 17:
            continue

        rows.append({
            "date":           parse_reiwa_date(str(parts[3])),
            "slip_no":        str(parts[1]).strip(),
            "debit_account":  str(parts[4]).strip().strip('"'),
            "debit_sub":      str(parts[5]).strip().strip('"'),
            "debit_tax":      str(parts[7]).strip().strip('"'),
            "debit_amount":   _to_num(parts[8]),
            "debit_tax_amt":  _to_num(parts[9]),
            "credit_account": str(parts[10]).strip().strip('"'),
            "credit_sub":     str(parts[11]).strip().strip('"'),
            "credit_tax":     str(parts[13]).strip().strip('"'),
            "credit_amount":  _to_num(parts[14]),
            "credit_tax_amt": _to_num(parts[15]),
            "description":    str(parts[16]).strip().strip('"') if len(parts) > 16 else "",
        })

    df = pd.DataFrame(rows)
    return df[df["debit_account"].str.strip() != ""].reset_index(drop=True)


def parse_yayoi(content: str) -> pd.DataFrame:
    """弥生会計CSVをパース（ヘッダーあり形式）"""
    df = pd.read_csv(io.StringIO(content), encoding="utf-8-sig", on_bad_lines="skip")
    col_map = {
        "日付": "date", "借方勘定科目": "debit_account", "借方補助科目": "debit_sub",
        "借方税区分": "debit_tax", "借方金額": "debit_amount",
        "貸方勘定科目": "credit_account", "貸方補助科目": "credit_sub",
        "貸方税区分": "credit_tax", "貸方金額": "credit_amount", "摘要": "description",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    return _normalize(df)


def parse_freee(content: str) -> pd.DataFrame:
    """freee CSVをパース"""
    df = pd.read_csv(io.StringIO(content), encoding="utf-8-sig", on_bad_lines="skip")
    col_map = {
        "発生日": "date", "借方勘定科目": "debit_account", "借方補助科目名称": "debit_sub",
        "借方税区分": "debit_tax", "借方金額": "debit_amount",
        "貸方勘定科目": "credit_account", "貸方補助科目名称": "credit_sub",
        "貸方税区分": "credit_tax", "貸方金額": "credit_amount",
        "備考": "description", "摘要": "description",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    return _normalize(df)


def parse_moneyforward(content: str) -> pd.DataFrame:
    """MoneyForward クラウド会計CSVをパース"""
    df = pd.read_csv(io.StringIO(content), encoding="utf-8-sig", on_bad_lines="skip")
    col_map = {
        "日付": "date", "借方科目": "debit_account", "借方補助科目": "debit_sub",
        "借方税区分": "debit_tax", "借方金額": "debit_amount",
        "貸方科目": "credit_account", "貸方補助科目": "credit_sub",
        "貸方税区分": "credit_tax", "貸方金額": "credit_amount", "摘要": "description",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    return _normalize(df)


def _to_num(val) -> float:
    try:
        return float(str(val).replace(",", "").strip())
    except Exception:
        return 0.0


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """データフレームを共通形式に正規化"""
    for col in ["date", "debit_account", "debit_amount", "credit_account", "credit_amount"]:
        if col not in df.columns:
            df[col] = None

    for col in ["debit_amount", "credit_amount"]:
        df[col] = pd.to_numeric(
            df[col].astype(str).str.replace(",", "").str.strip(),
            errors="coerce"
        ).fillna(0)

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["debit_account", "credit_account"], how="all")
    df = df[df["debit_account"].astype(str).str.strip() != ""]
    return df.reset_index(drop=True)


def parse_csv(content: str) -> tuple:
    """
    CSVを自動判定してパース
    Returns: (DataFrame, software_name)
    """
    software = detect_software(content)
    parsers = {
        "yayoi_raw":    parse_yayoi_raw,
        "yayoi":        parse_yayoi,
        "freee":        parse_freee,
        "moneyforward": parse_moneyforward,
    }
    df = parsers.get(software, parse_yayoi_raw)(content)
    return df, software


def parse_trial_balance(content: str) -> dict:
    """
    試算表CSVから期首残高を読み込む
    弥生の残高試算表形式を想定
    Returns: { "勘定科目名": 期首残高(float), ... }
    """
    balances = {}
    for enc in ["utf-8-sig", "utf-8", "shift_jis", "cp932"]:
        try:
            lines = content.encode("latin-1").decode(enc) if isinstance(content, str) else content.decode(enc)
            break
        except Exception:
            lines = content

    for line in (lines if isinstance(lines, list) else lines.split("\n")):
        line = str(line).strip()
        if not line:
            continue
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) < 3:
            continue
        account = parts[0]
        # 期首残高は2列目か3列目にあることが多い
        for p in parts[1:4]:
            try:
                val = float(p.replace(",", ""))
                if account and val != 0:
                    balances[account] = val
                break
            except ValueError:
                continue

    return balances
