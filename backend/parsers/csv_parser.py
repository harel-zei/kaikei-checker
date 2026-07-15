"""
会計ソフト別CSVパーサー
対応: 弥生会計（ヘッダーなし独自形式）, freee, MoneyForward
"""
import pandas as pd
import csv
import io
import re
from typing import Optional


def detect_software(content: str) -> str:
    first_lines = "\n".join(content.split("\n")[:5])
    if re.search(r'"21\d\d",\d+,"","R\.\d{2}/\d{2}/\d{2}"', first_lines):
        return "yayoi_raw"
    # freee 仕訳帳（新）CSV: ヘッダーが "No","取引日","管理番号","借方勘定科目" で始まる
    if '"No"' in first_lines and '"取引日"' in first_lines and '"借方勘定科目"' in first_lines:
        return "freee_new"
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

    ファイル全体を csv.reader（C実装）で一括パースし、列ごとにベクトル化して処理する。
    （旧実装は1行ずつ pd.read_csv を呼び出しており、2万行規模で30秒以上を要した）
    """
    WIDTH = 17  # 実データの列数
    try:
        # csv.reader は引用符・埋め込みカンマ・複数行フィールドを正しく処理する。
        # 旧実装同様「フィールド数15未満の行は除外」する（ヘッダ・端数行などのノイズ対策）。
        recs = [r for r in csv.reader(io.StringIO(content)) if len(r) >= 15]
    except Exception:
        # 一括パースに失敗した場合は従来の行単位パースにフォールバック
        return _parse_yayoi_raw_linewise(content)

    if not recs:
        return pd.DataFrame()

    # 各行を WIDTH 列に正規化（不足は空文字で埋め、超過は切り捨て）
    norm = [(r + [""] * (WIDTH - len(r)))[:WIDTH] for r in recs]
    raw = pd.DataFrame(norm, columns=list(range(WIDTH)))

    def _s(i: int) -> pd.Series:
        """指定列を文字列化し、前後空白と引用符を除去"""
        return raw[i].astype(str).str.strip().str.strip('"')

    def _n(i: int) -> pd.Series:
        """指定列を数値化（カンマ除去、失敗は0.0）"""
        return pd.to_numeric(
            raw[i].astype(str).str.replace(",", "", regex=False).str.strip(),
            errors="coerce",
        ).fillna(0.0)

    df = pd.DataFrame({
        "date":           _parse_reiwa_series(_s(3)),
        "slip_no":        _s(1),
        "debit_account":  _s(4),
        "debit_sub":      _s(5),
        "debit_tax":      _s(7),
        "debit_amount":   _n(8),
        "debit_tax_amt":  _n(9),
        "credit_account": _s(10),
        "credit_sub":     _s(11),
        "credit_tax":     _s(13),
        "credit_amount":  _n(14),
        "credit_tax_amt": _n(15),
        "description":    _s(16),
    })
    df["debit_account"]  = df["debit_account"].replace("nan", "")
    df["credit_account"] = df["credit_account"].replace("nan", "")
    return df.reset_index(drop=True)


def _parse_reiwa_series(s: pd.Series) -> pd.Series:
    """日付列（`R.yy/mm/dd` 形式優先）をベクトル化して Timestamp に変換する。
    parse_reiwa_date と同じ規則: R.形式は年+2018、それ以外は pd.to_datetime。"""
    m = s.str.extract(r'R\.(\d{2})/(\d{2})/(\d{2})')
    yy = pd.to_numeric(m[0], errors="coerce")
    reiwa = pd.to_datetime(
        {
            "year":  yy + 2018,
            "month": pd.to_numeric(m[1], errors="coerce"),
            "day":   pd.to_numeric(m[2], errors="coerce"),
        },
        errors="coerce",
    )
    # R.形式に一致しない行のみ通常の日付解析にフォールバック
    fallback = pd.to_datetime(s.where(yy.isna()), errors="coerce")
    return reiwa.fillna(fallback)


def _parse_yayoi_raw_linewise(content: str) -> pd.DataFrame:
    """行単位パース（一括読み込みが失敗した場合のフォールバック）"""
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


def parse_freee_new(content: str) -> pd.DataFrame:
    """
    freee 仕訳帳（新）CSV をパースする。

    主要な列マッピング:
      [1]  取引日        → date
      [3]  借方勘定科目  → debit_account
      [7]  借方金額      → debit_amount
      [8]  借方税区分    → debit_tax
      [9]  借方税金額    → debit_tax_amt
      [14] 借方取引先名  → debit_sub
      [36] 貸方勘定科目  → credit_account
      [40] 貸方金額      → credit_amount
      [41] 貸方税区分    → credit_tax
      [42] 貸方税金額    → credit_tax_amt
      [47] 貸方取引先名  → credit_sub
      [84] 仕訳番号      → slip_no
      [90] 取引内容      → description
    """
    try:
        df_raw = pd.read_csv(
            io.StringIO(content),
            encoding="utf-8-sig",
            on_bad_lines="skip",
            dtype=str,
        )
    except Exception:
        return pd.DataFrame()

    if df_raw.empty:
        return pd.DataFrame()

    col_names = list(df_raw.columns)

    # 列名で安全にマッピング
    def gcol(name: str) -> Optional[str]:
        """列名から DataFrame の列名を返す"""
        for c in col_names:
            if name in c:
                return c
        return None

    mapping = {
        gcol("取引日"):        "date",
        gcol("借方勘定科目"):  "debit_account",
        gcol("借方金額"):      "debit_amount",
        gcol("借方税区分"):    "debit_tax",
        gcol("借方税金額"):    "debit_tax_amt",
        gcol("借方取引先名"):  "debit_sub",
        gcol("貸方勘定科目"):  "credit_account",
        gcol("貸方金額"):      "credit_amount",
        gcol("貸方税区分"):    "credit_tax",
        gcol("貸方税金額"):    "credit_tax_amt",
        gcol("貸方取引先名"):  "credit_sub",
        gcol("仕訳番号"):      "slip_no",
        gcol("取引内容"):      "description",
    }
    mapping = {k: v for k, v in mapping.items() if k is not None}

    df = df_raw.rename(columns=mapping)

    # 借方・貸方両方にある「勘定科目ショートカット２」は先に除外
    # (借方勘定科目が複数マッチする可能性への対処)
    needed = ["date", "debit_account", "debit_amount", "debit_tax",
              "credit_account", "credit_amount", "credit_tax",
              "debit_sub", "credit_sub", "slip_no", "description",
              "debit_tax_amt", "credit_tax_amt"]
    df = df[[c for c in needed if c in df.columns]].copy()

    return _normalize(df)


def _to_num(val) -> float:
    try:
        return float(str(val).replace(",", "").strip())
    except Exception:
        return 0.0


# 和暦の元号と西暦開始年（元年=開始年）
_JP_ERA = {
    "令和": 2019, "令": 2019, "R": 2019,
    "平成": 1989, "平": 1989, "H": 1989,
    "昭和": 1926, "昭": 1926, "S": 1926,
    "大正": 1912, "大": 1912, "T": 1912,
}
_JP_DATE_RE = re.compile(
    r"(令和|平成|昭和|大正|令|平|昭|大|[RHST])\s*(\d{1,2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
)


def _convert_jp_era(val):
    """和暦表記（令和07年05月01日 等）を西暦の 'YYYY-MM-DD' に変換。
    和暦でなければ元の値をそのまま返す。"""
    if not isinstance(val, str):
        return val
    m = _JP_DATE_RE.search(val)
    if not m:
        return val
    era, yy, mm, dd = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
    base = _JP_ERA.get(era)
    if base is None:
        return val
    year = base + (yy - 1)  # 元号N年 = 開始年 + (N-1)
    return f"{year:04d}-{mm:02d}-{dd:02d}"


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    for col in ["date", "debit_account", "debit_amount", "credit_account", "credit_amount"]:
        if col not in df.columns:
            df[col] = None
    for col in ["debit_amount", "credit_amount"]:
        df[col] = pd.to_numeric(
            df[col].astype(str).str.replace(",", "").str.strip(),
            errors="coerce"
        ).fillna(0)
    # 和暦（令和・平成等）を西暦に変換してから日付解析
    df["date"] = df["date"].apply(_convert_jp_era)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df.reset_index(drop=True)


def parse_csv(content: str) -> tuple:
    software = detect_software(content)
    parsers = {
        "yayoi_raw":    parse_yayoi_raw,
        "yayoi":        parse_yayoi,
        "freee":        parse_freee,
        "freee_new":    parse_freee_new,   # freee 仕訳帳（新）CSV
        "moneyforward": parse_moneyforward,
    }
    df = parsers.get(software, parse_yayoi_raw)(content)
    return df, software


def _is_ledger_format(content: str) -> bool:
    """補助元帳形式かどうかを判定"""
    return '[前期繰越' in content[:2000]


def _is_freee_trial_balance(content: str) -> bool:
    """freee の試算表CSVかどうかを判定"""
    head = content[:500]
    return "試算表：" in head or ("期首" in head and "期末" in head and "借方金額" in head)


def parse_freee_balance(content: str, use_ending: bool = False) -> dict:
    """
    freee の試算表CSV（貸借対照表）から残高を取得する。

    freee BS 列構成:
      [0-7]: 階層（勘定科目・補助科目）
      [8]:   期首残高
      [9]:   借方金額
      [10]:  貸方金額
      [11]:  期末残高
      [12]:  構成比

    use_ending=True  → [11] 期末残高を返す（前期末 = 当期首として使用）
    use_ending=False → [8]  期首残高を返す

    取引先別の内訳（深い階層）は除外し、主科目・補助科目レベルのみ取得する。
    """
    balances: dict = {}

    # ヘッダー行から「期首」「期末」列の位置を特定
    opening_col = 8
    ending_col  = 11

    lines = content.split("\n")

    # ヘッダー行を探して列位置を確定
    for line in lines[:5]:
        parts = _split_csv_line(line)
        if "期首" in parts or "期末" in parts:
            for i, p in enumerate(parts):
                if "期首" in p:
                    opening_col = i
                if "期末" in p:
                    ending_col = i
            break

    amount_col = ending_col if use_ending else opening_col

    skip_keywords = ["の部", "合計", "構成比", "取引先別", "期間", "表示単位",
                     "試算表", "帳票名"]

    # 各depth レベルの「現在の科目名」を記録するスタック
    # freeeの階層: 0=大区分, 1=中区分, 2=主科目, 3=補助科目, 4=取引先
    current_at_depth: dict = {}

    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = _split_csv_line(line)

        # どの深さに値があるか確認
        account = ""
        depth   = -1
        for i in range(min(8, len(parts))):
            v = parts[i].strip().strip('"')
            if v:
                account = v
                depth   = i
                break

        if not account or depth < 0:
            continue

        # 現在の深さより深いスタックをクリア
        for d in list(current_at_depth.keys()):
            if d > depth:
                del current_at_depth[d]

        # スキップキーワードを含む行はスタックも更新しない
        if any(k in account for k in skip_keywords):
            continue

        # スタックを更新
        current_at_depth[depth] = account

        # depth >= 5 は詳細すぎるため除外
        if depth >= 5:
            continue

        # 金額を取得
        if len(parts) <= amount_col:
            continue
        amount_str = parts[amount_col].strip().strip('"')
        if not amount_str or amount_str in ("0", "0.0", ""):
            continue
        try:
            amount = float(amount_str.replace(",", ""))
        except ValueError:
            continue

        # キーを生成
        if depth <= 2:
            # 主科目レベル: そのまま登録
            key = account
        elif depth == 3:
            # 補助科目レベル（りそな　当座, paild等）
            parent = current_at_depth.get(2, "")
            key = f"{parent}（{account}）" if parent else account
        else:
            # depth=4: 取引先レベル（freeeの補助元帳相当）
            # 主科目（depth=2）をキーの親として使う
            parent = current_at_depth.get(2, current_at_depth.get(3, ""))
            key = f"{parent}（{account}）" if parent else account

        balances[key] = amount

    return balances


def _split_csv_line(line: str) -> list:
    """CSV行を安全にパースして各セルをリストで返す"""
    try:
        import csv
        return list(next(csv.reader(io.StringIO(line))))
    except Exception:
        return line.split(",")


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
            # 月度列があるか判定
            # 月次形式: parts[2]="12月度" など → parts[3]が分類コード
            # 全期間形式: parts[2]が分類コード "[貸借対照表]" など
            has_period_col = not (parts[2].startswith("[") and parts[2].endswith("]"))
            offset = 1 if has_period_col else 0  # 月度列の有無による列ずれ

            # 分類コードの位置: 月次=parts[3], 全期間=parts[2]
            class_code = parts[3] if has_period_col else parts[2]

            # 補助残高一覧表
            if class_code == "[貸借科目]":
                # 月次: 科目=parts[4], 補助=parts[5], 前期繰越=parts[6]
                # 全期間: 科目=parts[3], 補助=parts[4], 前期繰越=parts[5]
                acc_idx = 3 + offset
                sub_idx = 4 + offset
                amt_idx = 5 + offset
                if len(parts) > amt_idx:
                    account = parts[acc_idx]
                    sub     = parts[sub_idx]
                    amount  = _to_num(parts[amt_idx])  # 前期繰越
                    if sub in ("指定なし", ""):
                        balances[account] = amount
                    else:
                        key = f"{account}（{sub}）"
                        balances[key] = amount
                        sub_totals[account] = sub_totals.get(account, 0.0) + amount

            # 残高試算表（主科目）
            elif class_code in ("[貸借対照表]", "[損益計算書]", "[製造原価報告書]"):
                # 月次: 科目=parts[4], 前期繰越=parts[5]
                # 全期間: 科目=parts[3], 前期繰越=parts[4]
                acc_idx = 3 + offset
                amt_idx = 4 + offset
                if len(parts) > amt_idx:
                    account = parts[acc_idx]
                    amount  = _to_num(parts[amt_idx])  # 前期繰越
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


def parse_opening_from_ledger(content: str) -> dict:
    """
    弥生「補助元帳」エクスポートCSVから期首残高（前期繰越）を抽出する。

    補助元帳の [前期繰越行] 形式:
      [前期繰越行], 部門, 勘定科目, 補助科目, ...(空)..., 残高(列26)

    複数の補助元帳ファイルを個別にアップロードした場合でも
    まとめてマージして使用できる。

    Returns:
      { "買掛金（ナカウミベトナム）": 287735399, ... }
    """
    balances: dict = {}
    sub_totals: dict = {}

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

        parts = [str(p).strip().strip('"') for p in parts]
        if not parts:
            continue

        tag = parts[0]

        # 補助元帳の前期繰越行: [前期繰越行], 部門, 勘定科目, 補助科目, ..., 残高(26列目)
        if '[前期繰越' in tag and len(parts) >= 4:
            account = parts[2]
            sub     = parts[3]
            # 残高は列26付近を探す（空でない最後の数値列）
            amount  = 0.0
            # まず決まった位置(26)を試みる
            if len(parts) > 26 and parts[26]:
                amount = _to_num(parts[26])
            else:
                # フォールバック: 後ろから最初の数値を探す
                for v in reversed(parts):
                    try:
                        val = float(v.replace(",", ""))
                        if val != 0:
                            amount = val
                            break
                    except Exception:
                        continue

            if not account or account in ("", "nan"):
                continue

            if sub and sub not in ("", "nan", "指定なし"):
                key = f"{account}（{sub}）"
                balances[key] = amount
                sub_totals[account] = sub_totals.get(account, 0.0) + amount
            else:
                balances[account] = amount

    # 補助科目からの積み上げで主科目を補完
    for acc, total in sub_totals.items():
        if acc not in balances or balances[acc] == 0:
            balances[acc] = total

    return balances


def parse_ending_balances(content: str) -> dict:
    """
    弥生「残高試算表」または「補助残高一覧表」から**期末残高（当月残高）**を読み込む。
    前期の試算表を渡すと → 前期末残高 = 当期首残高 として使える。

    列構成（弥生）:
    主科目: [明細行],部門,月度,[貸借対照表],科目,前期繰越,借方,貸方,期末残高,構成比
                                                [4]   [5]     [6]  [7]  [8]
    補助:   [明細行],部門,月度,[貸借科目],科目,補助科目,前期繰越,借方,貸方,期末残高,構成比
                                           [4]  [5]    [6]     [7]  [8]  [9]

    ※ parse_opening_balances との違い: [5]/[6] の前期繰越ではなく [8]/[9] の期末残高を使う
    """
    balances: dict = {}
    sub_totals: dict = {}

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

        parts = [str(p).strip().strip('"') for p in parts]

        if len(parts) >= 1 and parts[0] == "[明細行]":
            has_period_col = not (parts[2].startswith("[") and parts[2].endswith("]"))
            offset     = 1 if has_period_col else 0
            class_code = parts[3] if has_period_col else parts[2]

            # 補助残高一覧: 期末残高
            # 月次: 科目=4+off, 補助=5+off, 期末=9+off
            # 全期間: 科目=3, 補助=4, 期末=8
            if class_code == "[貸借科目]":
                acc_idx = 3 + offset
                sub_idx = 4 + offset
                end_idx = 8 + offset  # 前期繰越(+0) 借方(+1) 貸方(+2) 期末(+3) → 5+3=8 or 6+3=9
                if len(parts) > end_idx:
                    account = parts[acc_idx]
                    sub     = parts[sub_idx]
                    amount  = _to_num(parts[end_idx])
                    if sub in ("指定なし", ""):
                        balances[account] = amount
                    else:
                        key = f"{account}（{sub}）"
                        balances[key] = amount
                        sub_totals[account] = sub_totals.get(account, 0.0) + amount

            # 残高試算表（主科目）: 期末残高
            # 月次: 科目=4+off, 期末=8+off  全期間: 科目=3, 期末=7
            elif class_code in ("[貸借対照表]", "[損益計算書]", "[製造原価報告書]"):
                acc_idx = 3 + offset
                end_idx = 7 + offset
                if len(parts) > end_idx:
                    account = parts[acc_idx]
                    amount  = _to_num(parts[end_idx])
                    if account not in sub_totals:
                        balances[account] = amount

    for acc, total in sub_totals.items():
        balances[acc] = total

    return balances
