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
    弥生「残高試算表」または「補助残高一覧表」のエクスポートCSVから期首残高を読み込む。

    対応フォーマット:
    ① 弥生 残高試算表（主科目）:
       "[明細行]","事業所(合計)","12月度","[貸借対照表]","現金",319541,34405,156407,197539,...
       → 列[4]=勘定科目, 列[5]=前期繰越（期首残高）

    ② 弥生 補助残高一覧表（補助科目）:
       "[明細行]","事業所(合計)","12月度","[貸借科目]","普通預金","永和信用金庫",74878245,...
       → 列[4]=勘定科目, 列[5]=補助科目, 列[6]=前期繰越（期首残高）

    ③ シンプル形式（手入力用）:
       勘定科目, 補助科目, 期首残高  例: 普通預金,永和信用金庫,5000000
       勘定科目, , 期首残高          例: 現金,,319541

    Returns:
      {
        "現金":                          319541,
        "普通預金（永和信用金庫・梅田）":  74878245,
        "普通預金（三菱ＵＦＪ・大阪駅前）": 154966,
        "普通預金":                      合計値（補助科目の積み上げ）,
        "売掛金（ＷＳＰ）":              18823349,
        ...
      }
    """
    balances: dict = {}
    sub_totals: dict = {}  # 補助科目から積み上げる合計

    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue

        # CSV行をパース
        try:
            parts = list(next(pd.read_csv(
                io.StringIO(line), header=None, quotechar='"'
            ).itertuples(index=False)))
        except Exception:
            continue

        parts = [str(p).strip().strip('"') for p in parts]

        # ── ① 弥生フォーマット（[明細行] 形式）──
        if len(parts) >= 1 and parts[0] == "[明細行]":
            # 補助残高一覧表: [明細行],部門,月度,[貸借科目],勘定科目,補助科目,前期繰越,...
            if len(parts) >= 7 and parts[3] == "[貸借科目]":
                account = parts[4]
                sub     = parts[5]
                amount  = _to_num(parts[6])  # 前期繰越

                # 「指定なし」は補助科目なしの科目全体を指す
                if sub in ("指定なし", ""):
                    balances[account] = amount
                else:
                    key = f"{account}（{sub}）"
                    balances[key] = amount
                    sub_totals[account] = sub_totals.get(account, 0.0) + amount

            # 残高試算表（主科目）: [明細行],部門,月度,[貸借対照表]/[損益計算書],勘定科目,前期繰越,...
            elif len(parts) >= 6 and parts[3] in ("[貸借対照表]", "[損益計算書]", "[製造原価報告書]"):
                account = parts[4]
                amount  = _to_num(parts[5])  # 前期繰越
                # 補助科目からの積み上げがなければ主科目として登録
                if account not in sub_totals:
                    balances[account] = amount
            continue

        # ── ② シンプル形式（手入力 / 他ソフト）──
        # 勘定科目, 補助科目, 金額  または  勘定科目, 金額
        if len(parts) < 2:
            continue
        account = parts[0]
        # 弥生の行タグ・ヘッダー行・空科目はスキップ
        if not account:
            continue
        if account.startswith("[") and account.endswith("]"):
            continue  # [表題行][区分行][合計行] など
        if account in ("勘定科目", "科目", "帳票名", "書式名", "事業所名",
                       "処理日時", "月次/期間", "集計期間", "税抜/税込"):
            continue

        if len(parts) >= 3:
            sub    = parts[1]
            amount = _to_num(parts[2])
            if sub and sub not in ("", "nan"):
                key = f"{account}（{sub}）"
                balances[key] = amount
                sub_totals[account] = sub_totals.get(account, 0.0) + amount
            else:
                if account not in sub_totals:
                    balances[account] = amount
        else:
            amount = _to_num(parts[1])
            if account not in sub_totals:
                balances[account] = amount

    # 補助科目の積み上げ合計で主科目を上書き（より正確）
    for acc, total in sub_totals.items():
        balances[acc] = total

    return balances
