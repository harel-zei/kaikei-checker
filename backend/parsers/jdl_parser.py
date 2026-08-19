"""
JDL会計（JDL IBEX / A-SaaS系）のエクスポートCSVパーサー。

JDLのCSVは他社ソフトと構造が大きく異なるため、独立したモジュールにしている。

共通の特徴:
  ・文字コードは cp932（Shift-JIS）
  ・先頭4行が固定のヘッダー
        "＜商号Ｃ＞",1714,
        "＜商号名＞","株式会社○○　　　　"
        "＜決算年月日＞","令和8年6月30日",
        "＜会計処理方法＞","税込処理",
  ・科目名は8バイト（全角4文字）で切り詰められる
        例) 短期借入金 → 「短期借入」 / 広告宣伝費 → 「広告宣伝」

対応ファイル:
  ① 仕訳一覧          （＜仕訳一覧＞）           → 仕訳帳
  ② 合計残高試算表（全科目）                      → 試算表（主科目）
  ③ 合計残高試算表（全科目補助別）                → 補助残高
"""
import csv
import io
import re
import unicodedata
from typing import Optional

import pandas as pd

# ── ファイル判定用のマーカー ───────────────────────────
_MARK_COMPANY  = "＜商号Ｃ＞"
_MARK_JOURNAL  = "＜仕訳一覧＞"
_MARK_BALANCE  = "＜合計残高試算表"
_MARK_BAL_SUB  = "補助別"

_HEAD = 2000  # 判定に使う先頭バイト数（他モジュールと揃える）


def is_jdl(content: str) -> bool:
    """JDL会計のエクスポートCSVかどうか"""
    return _MARK_COMPANY in content[:_HEAD]


def is_jdl_journal(content: str) -> bool:
    return is_jdl(content) and _MARK_JOURNAL in content[:_HEAD]


def is_jdl_balance(content: str) -> bool:
    return is_jdl(content) and _MARK_BALANCE in content[:_HEAD]


def is_jdl_balance_sub(content: str) -> bool:
    """補助科目の内訳を含む「合計残高試算表（全科目補助別）」かどうか"""
    if not is_jdl_balance(content):
        return False
    head = content[:_HEAD]
    i = head.find(_MARK_BALANCE)
    return _MARK_BAL_SUB in head[i:i + 40]


# ── 科目名の切り詰め復元 ──────────────────────────────
# JDLは科目名を8バイト（全角4文字）で切り出すため、5文字以上の標準科目は
# 末尾が欠落する。チェックロジックは「借入金」「減価償却費」等の完全な名称で
# 判定しているため、欠落したままだと本来拾うべき指摘が出ない。
# 復元は「標準的な勘定科目で、切り詰め形から一意に決まるもの」に限定する。
_TRUNCATED_ACCOUNTS = {
    # 資産
    "未収消費": "未収消費税等",
    "未収法人": "未収法人税等",
    "仮払消費": "仮払消費税等",
    "貸倒引当": "貸倒引当金",
    "建物付属": "建物附属設備",
    "建物附属": "建物附属設備",
    "車両運搬": "車両運搬具",
    "工具器具": "工具器具備品",
    "ソフトウ": "ソフトウェア",
    "建設仮勘": "建設仮勘定",
    "長期前払": "長期前払費用",
    "差入保証": "差入保証金",
    "役員貸付": "役員貸付金",
    "社員立替": "社員立替金",
    "前払保険": "前払保険料",
    # 負債
    "短期借入": "短期借入金",
    "長期借入": "長期借入金",
    "役員借入": "役員借入金",
    "仮受消費": "仮受消費税等",
    "未払消費": "未払消費税等",
    "未払法人": "未払法人税等",
    "住民税預": "住民税預り金",
    "社会保険": "社会保険預り金",
    "賞与引当": "賞与引当金",
    # 純資産
    "資本準備": "資本準備金",
    "利益準備": "利益準備金",
    "別途積立": "別途積立金",
    "繰越利益": "繰越利益剰余金",
    "新株予約": "新株予約権",
    # 売上・売上原価
    "期首棚卸": "期首棚卸高",
    "期末棚卸": "期末棚卸高",
    "他勘定振": "他勘定振替高",
    "外注加工": "外注加工費",
    # 販売費及び一般管理費
    "広告宣伝": "広告宣伝費",
    "販売促進": "販売促進費",
    "支払手数": "支払手数料",
    "販売手数": "販売手数料",
    "旅費交通": "旅費交通費",
    "接待交際": "接待交際費",
    "法定福利": "法定福利費",
    "福利厚生": "福利厚生費",
    "水道光熱": "水道光熱費",
    "支払保険": "支払保険料",
    "新聞図書": "新聞図書費",
    "図書教育": "図書教育費",
    "採用教育": "採用教育費",
    "研究開発": "研究開発費",
    "事務用品": "事務用品費",
    "開発費償": "開発費償却",
    # 営業外・税金
    "受取配当": "受取配当金",
    "商品廃棄": "商品廃棄損",
    "法人税・": "法人税等",
}

# 貸借科目と損益科目で意味が変わるもの。JDLの科目コードで判別する
# （JDL標準体系では 600 未満が貸借科目、600 以上が損益科目）。
_PL_CODE_FROM = 600
_TRUNCATED_BY_SIDE = {
    #  切り詰め形     : (貸借科目のとき,     損益科目のとき)
    "減価償却": ("減価償却累計額", "減価償却費"),
    "役員賞与": ("役員賞与引当金", "役員賞与"),
}


def expand_account_name(name: str, code: str = "") -> str:
    """8バイトで切り詰められた科目名を標準的な名称に復元する。
    表にないものは元の名称のまま返す。"""
    if not name:
        return name
    pair = _TRUNCATED_BY_SIDE.get(name)
    if pair:
        try:
            is_pl = int(str(code).strip()) >= _PL_CODE_FROM
        except (TypeError, ValueError):
            return name  # コードが読めないときは推測しない
        return pair[1] if is_pl else pair[0]
    return _TRUNCATED_ACCOUNTS.get(name, name)


def _clean(v: str) -> str:
    """全角空白・半角空白・引用符を除去する（JDLは固定長で右詰めパディングされる）"""
    return str(v).replace("　", " ").strip().strip('"').strip()


# ── 仕訳一覧 ────────────────────────────────────────
# ヘッダー行の例（当期・部門あり）:
#   "番号","部門",,"日付","借方科目",,"借方補助",,"貸方科目",,"貸方補助",,"金額","摘要","課区",,"税区",...
# 前期は部門列が1列しかない等、レイアウトが揺れるため列位置はヘッダーから決める。
# 科目・補助は「コード, 名称」の2列で1組。
_HEADER_KEYS = ("番号", "日付", "借方科目", "貸方科目", "金額")


def _find_header(rows: list) -> tuple:
    """ヘッダー行の位置とセル一覧を返す。見つからなければ (None, None)"""
    for i, r in enumerate(rows[:80]):
        cells = [_clean(c) for c in r]
        if all(k in cells for k in _HEADER_KEYS):
            return i, cells
    return None, None


def parse_jdl_journal(content: str) -> pd.DataFrame:
    """JDL「仕訳一覧」CSVを共通の仕訳DataFrameに変換する。

    JDLは1行が1組の借方・貸方で、金額は1列のみ（借方＝貸方）。
    複合仕訳は科目コード999「諸口」を経由して複数行に分解されている。
    """
    try:
        rows = list(csv.reader(io.StringIO(content)))
    except Exception:
        return pd.DataFrame()

    hdr_i, hdr = _find_header(rows)
    if hdr_i is None:
        return pd.DataFrame()

    def col(name: str) -> Optional[int]:
        return hdr.index(name) if name in hdr else None

    i_date  = col("日付")
    i_damt  = col("金額")
    i_dacc  = col("借方科目")
    i_dsub  = col("借方補助")
    i_cacc  = col("貸方科目")
    i_csub  = col("貸方補助")
    i_desc  = col("摘要")
    i_kubun = col("課区")   # 課税区分（仕入対課税売上 等）
    i_rate  = col("税区")   # 税率（税率１０％ 等）
    i_no    = col("番号")
    if i_date is None or i_damt is None:
        return pd.DataFrame()

    # 名称列は「コード列 + 1」を参照するため、必要な列数はヘッダー最終列+2まで見る
    need = max(x for x in (i_date, i_damt, i_dacc, i_dsub, i_cacc, i_csub,
                           i_desc, i_kubun, i_rate) if x is not None) + 2
    width = max(len(hdr), need)

    data = [
        (r + [""] * (width - len(r)))[:width]
        for r in rows[hdr_i + 1:]
        if len(r) >= need and _clean(r[i_date])
    ]
    if not data:
        return pd.DataFrame()

    raw = pd.DataFrame(data, columns=list(range(width)))

    def s(i: Optional[int]) -> pd.Series:
        """名称列を取り出して整形する"""
        if i is None:
            return pd.Series([""] * len(raw), index=raw.index)
        return raw[i].astype(str).map(_clean)

    def acc(i: Optional[int]) -> pd.Series:
        """科目列（コード＋名称の2列組）を取り出し、切り詰めを復元する"""
        if i is None:
            return pd.Series([""] * len(raw), index=raw.index)
        codes = raw[i].astype(str).map(_clean)
        names = raw[i + 1].astype(str).map(_clean)
        # 同じ (名称, コード) の組み合わせは何千行も繰り返されるため、
        # 一意な組だけ変換して割り当てる（2万行規模での復元コストを抑える）
        pairs = pd.Series(list(zip(names, codes)), index=raw.index)
        table = {p: expand_account_name(p[0], p[1]) for p in set(pairs)}
        return pairs.map(table)

    # 課税区分と税率を1つの文字列にまとめる。
    # JDLは「課区(仕入対課税売上)」と「税区(税率１０％)」に分かれており、
    # 既存チェックは1つの税区分文字列に対して "課税" / "10%" / "軽減" を見ている。
    kubun = s(i_kubun + 1 if i_kubun is not None else None)
    rate  = s(i_rate + 1 if i_rate is not None else None)
    rate  = rate.map(lambda v: unicodedata.normalize("NFKC", v) if v else v)
    tax   = (kubun + " " + rate).str.strip()

    amount = pd.to_numeric(
        raw[i_damt].astype(str).str.replace(",", "", regex=False).str.strip(),
        errors="coerce",
    ).fillna(0.0)

    df = pd.DataFrame({
        "date":           _parse_jdl_date_series(
                              raw[i_date].astype(str).map(_clean),
                              jdl_period_date(content),
                          ),
        "slip_no":        s(i_no),
        "debit_account":  acc(i_dacc),
        "debit_sub":      s(i_dsub + 1 if i_dsub is not None else None),
        "debit_tax":      tax,
        "debit_amount":   amount,
        "debit_tax_amt":  0.0,
        "credit_account": acc(i_cacc),
        "credit_sub":     s(i_csub + 1 if i_csub is not None else None),
        "credit_tax":     tax,
        "credit_amount":  amount,
        "credit_tax_amt": 0.0,
        "description":    s(i_desc),
    })
    return df.reset_index(drop=True)


_JDL_DATE_RE = re.compile(r"^\d{5,6}$")


def _parse_jdl_date_series(s: pd.Series,
                           settle_date: Optional[pd.Timestamp] = None) -> pd.Series:
    """JDLの日付（和暦の YMMDD / YYMMDD）をベクトル化して変換する。
    例) 70701 → 令和7年7月1日 → 2025-07-01 / 100301 → 令和10年3月1日

    JDLは決算整理仕訳を「13月」で表す（例 61330）。カレンダー上は存在しないため、
    決算年月日に寄せる。決算年月日が読めない場合のみ日付なしとして扱う。
    """
    ok = s.str.match(_JDL_DATE_RE).fillna(False)
    v = s.where(ok)
    month = pd.to_numeric(v.str[-4:-2], errors="coerce")
    parsed = pd.to_datetime(
        {
            "year":  pd.to_numeric(v.str[:-4], errors="coerce") + 2018,
            "month": month.where(month <= 12),
            "day":   pd.to_numeric(v.str[-2:], errors="coerce"),
        },
        errors="coerce",
    )
    if settle_date is not None:
        parsed = parsed.mask(month > 12, settle_date)
    return parsed


# ── 合計残高試算表 ──────────────────────────────────
# 列構成: [0]科目コード [1]科目名 [2]期首残高 [3]借方 [4]貸方 [5]繰越残高 [6]構成比
#   ・科目行         : [0]にコード、[1]に科目名
#   ・補助科目行     : [0]が空、[1]が " -   1楽天" の形式（補助別ファイルのみ）
#   ・小計・合計行   : [0]が空、[1]が （…） ［…］ 【…】 で囲まれている → 読み飛ばす
_BAL_OPENING_COL = 2   # 期首残高
_BAL_ENDING_COL  = 5   # 繰越残高（期末残高）
_SUB_RE = re.compile(r"^-\s*\d+\s*")
_GROUP_CHARS = "（［【(“"


def parse_jdl_balance(content: str, use_ending: bool = False) -> dict:
    """JDL「合計残高試算表」から残高を読み込む。

    use_ending=False → 期首残高（当期の期首残高として使う）
    use_ending=True  → 繰越残高（前期ファイルの期末＝当期首として使う）

    戻り値のキーは他パーサーと同じ形式:
        {"売掛金": 84578891, "売掛金（楽天）": 40795721, ...}
    """
    amount_col = _BAL_ENDING_COL if use_ending else _BAL_OPENING_COL
    balances: dict = {}
    current_account = ""

    try:
        rows = list(csv.reader(io.StringIO(content)))
    except Exception:
        return balances

    for r in rows:
        if len(r) <= amount_col:
            continue
        code = _clean(r[0])
        name = _clean(r[1])
        if not name:
            continue

        if code.isdigit():
            # 科目行
            current_account = expand_account_name(name, code)
            balances[current_account] = _num(r[amount_col])
        elif name[0] in _GROUP_CHARS:
            # （現金）［現金預金］【合計】などの集計行は科目ではない
            continue
        elif name.startswith("-") and current_account:
            # 補助科目行 " -   1楽天" → 補助科目名は先頭の "- 連番" を除いた部分
            sub = _SUB_RE.sub("", name).strip()
            if sub:
                balances[f"{current_account}（{sub}）"] = _num(r[amount_col])

    return balances


def _num(v) -> float:
    try:
        return float(str(v).replace(",", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


# ── 期間判定 ────────────────────────────────────────
_SETTLE_RE = re.compile(r"＜決算年月日＞\D*令和(\d{1,2})年(\d{1,2})月(\d{1,2})日")


def jdl_period_date(content: str) -> Optional[pd.Timestamp]:
    """ヘッダーの「＜決算年月日＞」から決算日を返す。
    当期・前期の振り分けはこの日付の新旧で判定できる。"""
    m = _SETTLE_RE.search(content[:_HEAD])
    if not m:
        return None
    try:
        return pd.Timestamp(
            year=int(m.group(1)) + 2018,
            month=int(m.group(2)),
            day=int(m.group(3)),
        )
    except ValueError:
        return None
