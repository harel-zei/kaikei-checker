"""
JDL会計CSVパーサーのテスト。

顧問先の実データは使わず、JDLのエクスポート形式を再現した合成データで検証する。
JDL特有の落とし穴（科目名の8バイト切り詰め・13月の決算整理仕訳・
部門列の有無によるレイアウト差）を固定する。
"""
import pandas as pd
import pytest

from parsers.csv_parser import parse_csv, detect_software
from parsers.file_detector import (
    detect_file_type, extract_period_date,
    FILE_TYPE_JOURNAL, FILE_TYPE_BALANCE_MAIN, FILE_TYPE_BALANCE_SUB,
)
from parsers.jdl_parser import (
    parse_jdl_balance, expand_account_name, jdl_period_date, is_jdl,
)


# ── 合成データ生成 ────────────────────────────────────
_HEAD = (
    '"＜商号Ｃ＞",1714,\n'
    '"＜商号名＞","テスト商事株式会社　　　　",\n'
    '"＜決算年月日＞","令和8年6月30日",\n'
    '"＜会計処理方法＞","税込処理",\n'
    '\n'
)

# 当期形式: 部門がコード＋名称の2列
_HDR_WITH_DEPT = (
    '"番号","部門",,"日付","借方科目",,"借方補助",,"貸方科目",,"貸方補助",,'
    '"金額","摘要","課区",,"税区",,"資金",,"手形期日",'
)
# 前期形式: 部門が1列だけ（レイアウトが1列ずれる）
_HDR_NO_DEPT = (
    '"番号","","日付","借方科目",,"借方補助",,"貸方科目",,"貸方補助",,'
    '"金額","摘要","課区",,"税区",,"資金",,"手形期日",'
)


def _row(date, dacc, dname, cacc, cname, amount, desc="",
         dsub="", csub="", kubun="", rate="", with_dept=True):
    dept = '1,"本部　　"' if with_dept else '" #"'
    return (
        f'10,{dept},{date},{dacc},"{dname}","","{dsub}",{cacc},"{cname}","","{csub}",'
        f'{amount},"{desc}","31","{kubun}","10","{rate}",,,,'
    )


def _journal(rows, with_dept=True):
    hdr = _HDR_WITH_DEPT if with_dept else _HDR_NO_DEPT
    return _HEAD + '"＜仕訳一覧＞",\n' + hdr + "\n" + "\n".join(rows) + "\n"


_BALANCE = _HEAD + (
    '"自7月1日～至13月30日"\n'
    '"","",\n'
    '\n'
    '"＜合計残高試算表（全科目）＞",\n'
    '"科目","","期首残高","借方","貸方","繰越残高","構成比",\n'
    '112,"小口現金",184778,2043143,2079494,148427,,\n'
    '"","（現金　　　　　　）",184778,2043143,2079494,148427,,\n'
    '213,"売掛金　",84578891,1419044854,1365024479,138599266,"16.9",\n'
    '"","［営業債権　　　　］",84578891,1419044854,1365024479,138599266,"16.9",\n'
    '423,"短期借入",55685000,100000000,185515000,141200000,,\n'
    '834,"広告宣伝",,302063697,,302063697,,\n'
    '"","【合計　　　　　　】",,12695757719,12695757719,,,\n'
    '"＜合計残高試算表（全科目）終了＞",\n'
)

_BALANCE_SUB = _BALANCE.replace(
    "＜合計残高試算表（全科目）＞", "＜合計残高試算表（全科目補助別）＞"
).replace(
    '"","［営業債権　　　　］",84578891,1419044854,1365024479,138599266,"16.9",\n',
    '""," -   1楽天　　　　",40795721,331729999,330988356,41537364,"5.1",\n'
    '""," -   2Google　　　",640808,,637869,2939,,\n',
)


# ── 判定 ──────────────────────────────────────────
class TestDetect:
    def test_journal_is_recognized_as_jdl(self):
        c = _journal([_row(70701, 445, "未払費用", 141, "りそな", 8800000)])
        assert detect_software(c) == "jdl"
        assert detect_file_type(c) == FILE_TYPE_JOURNAL

    def test_journal_not_mistaken_for_moneyforward(self):
        """「借方科目/貸方科目」列を持つためMFと誤判定されやすい"""
        c = _journal([_row(70701, 445, "未払費用", 141, "りそな", 100)])
        assert detect_software(c) != "moneyforward"

    def test_balance_main_and_sub(self):
        assert detect_file_type(_BALANCE) == FILE_TYPE_BALANCE_MAIN
        assert detect_file_type(_BALANCE_SUB) == FILE_TYPE_BALANCE_SUB

    def test_period_date_from_settlement_header(self):
        c = _journal([_row(70701, 445, "未払費用", 141, "りそな", 100)])
        assert jdl_period_date(c) == pd.Timestamp("2026-06-30")
        assert extract_period_date(c, FILE_TYPE_JOURNAL) == pd.Timestamp("2026-06-30")
        assert extract_period_date(_BALANCE, FILE_TYPE_BALANCE_MAIN) == pd.Timestamp("2026-06-30")

    def test_non_jdl_content_untouched(self):
        assert not is_jdl('"2110",1,"","R.08/01/05","現金","","","",1000,0,"売上高"')


# ── 仕訳一覧 ──────────────────────────────────────
class TestJournal:
    def test_basic_columns(self):
        c = _journal([
            _row(70701, 445, "未払費用", 141, "りそな", 8800000, desc="総合振込",
                 dsub="㈱テスト", kubun="仕入対課税売上", rate="税率１０％"),
        ])
        df, sw = parse_csv(c)
        assert sw == "jdl" and len(df) == 1
        r = df.iloc[0]
        assert r["date"] == pd.Timestamp("2025-07-01")
        assert r["debit_account"] == "未払費用"
        assert r["credit_account"] == "りそな"
        assert r["debit_sub"] == "㈱テスト"
        assert r["description"] == "総合振込"
        # JDLは金額1列（借方＝貸方）
        assert r["debit_amount"] == 8800000 and r["credit_amount"] == 8800000

    def test_layout_without_department_column(self):
        """前期エクスポートは部門列が1列しかなく、以降の列が1つずれる"""
        c = _journal([
            _row(60701, 423, "短期借入", 144, "みずほ（", 185000,
                 desc="政策公庫", with_dept=False),
        ], with_dept=False)
        df, _ = parse_csv(c)
        assert len(df) == 1
        r = df.iloc[0]
        assert r["date"] == pd.Timestamp("2024-07-01")
        assert r["debit_amount"] == 185000
        assert r["description"] == "政策公庫"

    def test_settlement_month_13_maps_to_fiscal_year_end(self):
        """JDLは決算整理仕訳を13月で表す。カレンダー外なので決算日に寄せる"""
        c = _journal([
            _row(71330, 860, "減価償却", 330, "減価償却", 500000, desc="決算整理"),
        ])
        df, _ = parse_csv(c)
        assert df.iloc[0]["date"] == pd.Timestamp("2026-06-30")
        assert df["date"].notna().all(), "決算整理仕訳が日付なしとして落ちている"

    def test_tax_class_and_rate_are_combined(self):
        """課区（課税区分）と税区（税率）は別列。既存チェックは1つの文字列を見る"""
        c = _journal([
            _row(70705, 834, "広告宣伝", 433, "未払金", 55050, desc="広告",
                 kubun="仕入対課税売上", rate="税率１０％"),
        ])
        df, _ = parse_csv(c)
        tax = df.iloc[0]["debit_tax"]
        assert "課税" in tax
        # 全角の税率表記は半角に正規化して "10%" で判定できるようにする
        assert "10%" in tax

    def test_reduced_rate_is_distinguishable(self):
        c = _journal([
            _row(70705, 834, "広告宣伝", 433, "未払金", 1080, desc="茶菓",
                 kubun="仕入対課税売上", rate="軽減税率８％"),
        ])
        df, _ = parse_csv(c)
        assert "8%" in df.iloc[0]["debit_tax"]

    def test_empty_and_broken_input(self):
        assert parse_csv(_HEAD)[0].empty
        assert parse_csv(_journal([]))[0].empty


# ── 科目名の切り詰め復元 ────────────────────────────
class TestAccountNameExpansion:
    @pytest.mark.parametrize("short,full", [
        ("短期借入", "短期借入金"),
        ("長期借入", "長期借入金"),
        ("広告宣伝", "広告宣伝費"),
        ("支払手数", "支払手数料"),
        ("法定福利", "法定福利費"),
        ("車両運搬", "車両運搬具"),
        ("繰越利益", "繰越利益剰余金"),
        ("受取配当", "受取配当金"),
    ])
    def test_standard_accounts_restored(self, short, full):
        assert expand_account_name(short, "999") == full

    def test_ambiguous_name_uses_account_code(self):
        """減価償却は貸借（累計額）と損益（費用）で意味が違う"""
        assert expand_account_name("減価償却", "330") == "減価償却累計額"
        assert expand_account_name("減価償却", "860") == "減価償却費"

    def test_ambiguous_name_without_code_is_left_alone(self):
        assert expand_account_name("減価償却", "") == "減価償却"

    def test_unknown_name_is_left_alone(self):
        """顧問先独自の科目名を勝手に書き換えない"""
        for name in ("芝信用金", "ＴＶ制作", "スポンサ", "通販売上"):
            assert expand_account_name(name, "611") == name

    def test_expansion_applies_to_journal(self):
        c = _journal([_row(70701, 423, "短期借入", 144, "みずほ（", 185000)])
        df, _ = parse_csv(c)
        assert df.iloc[0]["debit_account"] == "短期借入金"
        assert df.iloc[0]["credit_account"] == "みずほ（"


# ── 合計残高試算表 ──────────────────────────────────
class TestBalance:
    def test_opening_balances(self):
        b = parse_jdl_balance(_BALANCE)
        assert b["小口現金"] == 184778
        assert b["売掛金"] == 84578891
        assert b["短期借入金"] == 55685000

    def test_ending_balances(self):
        b = parse_jdl_balance(_BALANCE, use_ending=True)
        assert b["小口現金"] == 148427
        assert b["短期借入金"] == 141200000

    def test_group_rows_are_skipped(self):
        """（現金）［営業債権］【合計】は集計行であって科目ではない"""
        b = parse_jdl_balance(_BALANCE)
        for k in b:
            assert k[0] not in "（［【", f"集計行が科目として取り込まれている: {k}"

    def test_sub_accounts(self):
        b = parse_jdl_balance(_BALANCE_SUB)
        assert b["売掛金（楽天）"] == 40795721
        assert b["売掛金（Google）"] == 640808
        assert b["売掛金"] == 84578891, "主科目の残高も残ること"

    def test_broken_input_returns_empty(self):
        assert parse_jdl_balance("") == {}
        assert parse_jdl_balance("なにか,別の,ファイル") == {}
