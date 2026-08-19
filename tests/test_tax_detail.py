"""
tax_detail_checker（カテゴリ2: 消費税区分の正確性）のテスト。
"""
from conftest import make_journal, entry as je


def _run(rows):
    from checkers.tax_detail_checker import check_tax_detail
    return check_tax_detail(make_journal(rows))


class TestReducedRate21:
    """2-1: 軽減税率（8%）対象なのに10%になっている"""

    def test_bento_at_10pct_flagged(self):
        issues = _run([
            je("2026-03-05", "会議費", "現金", 3000, dtax="課対仕入10%", desc="会議用弁当"),
        ])
        assert any(i["check_id"] == "2-1" for i in issues)

    def test_new_keywords_flagged(self):
        """拡充キーワード（パン・牛乳・コーヒー豆）も検知する"""
        for kw in ("パン", "牛乳", "コーヒー豆"):
            issues = _run([
                je("2026-03-05", "福利厚生費", "現金", 1500, dtax="課対仕入10%", desc=f"{kw}購入"),
            ])
            assert any(i["check_id"] == "2-1" for i in issues), f"{kw} が未検知"

    def test_company_names_containing_pan_not_flagged(self):
        """「ジャパン」等は社名であって食パンではない。
        （JDLは取引先名が摘要に入るため、この誤検知が大量に出ていた）"""
        for desc in ("キヤノンマーケテイングジヤパン（カ", "アマゾンジャパン合同会社",
                     "パンフレット印刷代", "Japan Post"):
            issues = _run([
                je("2026-03-05", "広告宣伝費", "未払金", 55000,
                   dtax="課対仕入10%", desc=desc),
            ])
            assert not any(i["check_id"] == "2-1" for i in issues), f"{desc} を誤検知"

    def test_already_8pct_not_flagged(self):
        issues = _run([
            je("2026-03-05", "会議費", "現金", 3000, dtax="課対仕入8%(軽)", desc="会議用弁当"),
        ])
        assert not any(i["check_id"] == "2-1" for i in issues)

    def test_eat_in_excluded(self):
        """外食・店内飲食（10%が正しい）は指摘しない"""
        for desc in ("ランチ弁当会食", "カフェにて打合せ お茶", "レストラン 会食"):
            issues = _run([
                je("2026-03-05", "会議費", "現金", 3000, dtax="課対仕入10%", desc=desc),
            ])
            assert not any(i["check_id"] == "2-1" for i in issues), f"「{desc}」を誤検知"

    def test_placename_chaya_excluded(self):
        """地名（天下茶屋等）は誤検知しない"""
        issues = _run([
            je("2026-03-05", "旅費交通費", "現金", 500, dtax="課対仕入10%", desc="天下茶屋 駐車場"),
        ])
        assert not any(i["check_id"] == "2-1" for i in issues)


class TestGovtFee23:
    """2-3: 印紙・行政手数料の課税誤り"""

    def test_stamp_at_10pct_flagged(self):
        issues = _run([
            je("2026-03-05", "租税公課", "現金", 200, dtax="課対仕入10%", desc="収入印紙"),
        ])
        assert any(i["check_id"] == "2-3" for i in issues)

    def test_store_name_excluded(self):
        """「市役所前店」のような店舗名は誤検知しない"""
        issues = _run([
            je("2026-03-05", "消耗品費", "現金", 1000, dtax="課対仕入10%", desc="コンビニ市役所前店"),
        ])
        assert not any(i["check_id"] == "2-3" for i in issues)

    def test_parking_context_excluded(self):
        """役所近くの駐車場（課税で正しい）は誤検知しない"""
        issues = _run([
            je("2026-03-05", "消耗品費", "現金", 800, dtax="課対仕入10%", desc="市役所 タイムズ駐車場"),
        ])
        assert not any(i["check_id"] == "2-3" for i in issues)

    def test_import_consumption_tax_excluded(self):
        """輸入消費税（国税分）は税関に納付するが仕入税額控除の対象。
        「国税」の語で行政手数料と誤認しない。"""
        for desc in ("ＤＨＬ 海外からの着払い 消費税国税", "輸入消費税 国税", "通関料 国税"):
            issues = _run([
                je("2026-03-05", "荷造運賃", "未払金", 30000,
                   dtax="課対仕入10%", desc=desc),
            ])
            assert not any(i["check_id"] == "2-3" for i in issues), f"{desc} を誤検知"


class TestTaxRateHelpers:
    """税区分文字列の判定"""

    def test_non_taxable_is_not_treated_as_10pct(self):
        """「非課税仕入」は文字列に"課税"を含むが課税10%ではない"""
        from checkers.tax_detail_checker import _has_tax_10
        for v in ("非課税仕入", "非課税売上", "輸出等免税売上", "不課税", "対象外"):
            assert not _has_tax_10(v), f"{v} を10%課税と誤判定"
        assert _has_tax_10("課対仕入10%")
        assert _has_tax_10("仕入対課税売上 税率10%")
        assert not _has_tax_10("仕入対課税売上 軽減税率8%")


class TestOverseas:
    """2-5 / 2-6: 海外ベンダー・海外渡航"""

    def test_2_6_international_flight_flagged(self):
        issues = _run([
            je("2026-03-05", "旅費交通費", "未払金", 180000, dtax="課対仕入10%", desc="国際線航空券"),
        ])
        assert any(i["check_id"] == "2-6" for i in issues)

    def test_2_6_domestic_flight_not_flagged(self):
        issues = _run([
            je("2026-03-05", "旅費交通費", "未払金", 30000, dtax="課対仕入10%", desc="ANA 羽田-伊丹"),
        ])
        assert not any(i["check_id"] == "2-6" for i in issues)

    def test_2_5_registered_vendor_not_flagged(self):
        """適格請求書登録済みの大手（Google等）は指摘しない"""
        issues = _run([
            je("2026-03-05", "広告宣伝費", "未払金", 50000, dtax="課対仕入10%", desc="Google広告"),
        ])
        assert not any(i["check_id"] == "2-5" for i in issues)


class TestNewspaper28:
    """2-8: 新聞代の軽減税率"""

    def test_paper_newspaper_flagged(self):
        issues = _run([
            je("2026-03-05", "新聞図書費", "現金", 4400, dtax="課対仕入10%", desc="日本経済新聞 購読料"),
        ])
        assert any(i["check_id"] == "2-8" for i in issues)

    def test_digital_edition_not_flagged(self):
        """電子版は10%が正しい"""
        issues = _run([
            je("2026-03-05", "新聞図書費", "未払金", 4277, dtax="課対仕入10%", desc="日経電子版"),
        ])
        assert not any(i["check_id"] == "2-8" for i in issues)
