"""
会計ソフト別CSVパーサー
対応: 弥生会計（ヘッダーなし独自形式）, freee, MoneyForward
"""
import pandas as pd
import io
import re
from typing import Optional


def detect_software(content: str) -> str:
    first_lines = "\n".join(content.split("\n")[:5])
    if re.search(r'"21\d\d",\d+,"","R\.\d{2}/\d{2}/\d{2}"', first_lines):
        return "yayoi_raw"
    if "借方勘定科目" in content and "貸方勘定科目" in content:
        return "yayoi"
    if "取引No" in content or ("発生日" in content and "借方勘定科目" in content):
        return "freee"
    if "仕訳番号" in content or ("借方科目" in content and "貸方科目" in content):
        return "moneyforward"
    return "yayoi_raw"


def parse_reiwa_date(date_str: str) -> Optional[pd.Timestamp]:
    date_str = date_str.strip().strip('"')
    m = re.match(r'R\.(\d{2})/(\d{2})/(\d{2})', date_str)
    if m:
        western_year = int(m.group(1)) + 2018
        try:
            return pd.Timestamp(year=western_year, month=int(m.group(2)), day=int(m.group(3)))
        except Exception:
            return None
    try:
        return pd.to_datetime(date_str, errors="coerce")
    except Exception:
        return None


def parse_yayoi_raw(content: str) -> pd.DataFrame:
    """
    弥生会計ヘッダーなし独自形式
    [0]コード [1]伝票番号 [2]空 [3]日付 [4]借方科目 [5]借方補助 [6]空
    [7]借方税区分 [8]借方金額 [9]借方消費税額 [10]貸方科目 [11]貸方補助 [12]空
    [13]貸方税区分 [14]貸方金額 [15]貸方消費税額 [16]摘要
    """
    rows = []
    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            parts = list(next(pd.read_csv(
                io.StringIO(line), header=None, quotechar='"'
            ).itertuples(index=False)))
        except Exception:
            continue
        if len(parts) < 15:
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
            "credit_tax_amt": _to_num(parts[15]) if len(parts) > 15 else 0.0,
            "description":    str(parts[16]).strip().strip('"') if len(parts) > 16 else "",
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    # 借方or貸方が空でも残す（複合仕訳の片側のみ行）
    df["debit_account"]  = df["debit_account"].replace("nan", "")
    df["credit_account"] = df["credit_account"].replace("nan", "")
    return df.reset_index(drop=True)


def parse_yayoi(content: str) -> pd.DataFrame:
    lines = content.split("\n")
    header_idx = 0
    for i, line in enumerate(lines):
        if "日付" in line and "借方勘定科目" in line:
            header_idx = i
            break
    df = pd.read_csv(io.StringIO("\n".join(lines[header_idx:])),
                     encoding="utf-8-sig", on_bad_lines="skip")
    col_map = {
        "日付": "date", "借方勘定科目": "debit_account", "借方補助科目": "debit_sub",
        "借方税区分": "debit_tax", "借方金額": "debit_amount",
        "貸方勘定科目": "credit_account", "貸方補助科目": "credit_sub",
        "貸方税区分": "credit_tax", "貸方金額": "credit_amount", "摘要": "description",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    return _normalize(df)


def parse_freee(content: str) -> pd.DataFrame:
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
    for col in ["date", "debit_account", "debit_amount", "credit_account", "credit_amount"]:
        if col not in df.columns:
            df[col] = None
    for col in ["debit_amount", "credit_amount"]:
        df[col] = pd.to_numeric(
            df[col].astype(str).str.replace(",", "").str.strip(),
            errors="coerce"
        ).fillna(0)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df.reset_index(drop=True)


def parse_csv(content: str) -> tuple:
    software = detect_software(content)
    parsers = {
        "yayoi_raw":    parse_yayoi_raw,
        "yayoi":        parse_yayoi,
        "freee":        parse_freee,
        "moneyforward": parse_moneyforward,
    }
    df = parsers.get(software, parse_yayoi_raw)(content)
    return df, software


def parse_opening_balances(content: str) -> dict:
    """
    期首残高CSVをパース。
    フォーマット例（弥生「残高試算表」エクスポート）:
      勘定科目,補助科目,期首残高
      現金,,319541
      普通預金,永和信用金庫・梅田,5000000
      普通預金,〇〇銀行,1200000

    Returns:
      {
        "現金": 319541,
        "普通預金（永和信用金庫・梅田）": 5000000,
        "普通預金（〇〇銀行）": 1200000,
        "普通預金": 6200000,   ← 補助科目なし=合計も登録
      }
    """
    balances = {}
    account_totals: dict = {}

    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) < 2:
            continue

        account = parts[0]
        sub     = parts[1] if len(parts) > 1 else ""
        # 金額は3列目優先、なければ2列目
        amount_str = parts[2] if len(parts) > 2 else parts[1]
        try:
            amount = float(amount_str.replace(",", ""))
        except ValueError:
            continue  # ヘッダー行などスキップ

        if not account:
            continue

        if sub and sub not in ("", "nan"):
            key = f"{account}（{sub}）"
            balances[key] = amount
            account_totals[account] = account_totals.get(account, 0) + amount
        else:
            # 補助科目なしの行は科目合計として登録
            balances[account] = amount

    # 補助科目から積み上げた合計がある場合は上書き（より正確）
    for acc, total in account_totals.items():
        if acc not in balances or balances[acc] == 0:
            balances[acc] = total

    return balances
