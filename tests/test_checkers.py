"""
各チェッカーの単体テスト。
「検知すべきものを検知する」と「検知すべきでないものを検知しない（誤検知防止）」
の両面を確認する。過去に修正したバグの再発防止テストを含む。
"""
import pandas as pd

from conftest import make_journal, entry as je


# ══════════════════════════════════════════════════════════
# ar_ap_checker（4-1 / 4-2）
# ══════════════════════════════════════════════════════════
class TestArAp:
    def test_4_2_stale_suspense_detected(self):
        """90日以上未精算の仮払金を検知する。
        （再発防止: slip_safe の import 漏れで検知時に必ずクラッシュしていた）"""
        from checkers.ar_ap_checker import check_ar_ap
        df = make_journal([
            je("2026-01-10", "仮払金", "普通預金", 100000, dsub="営業部", desc="出張仮払", slip="101"),
            je("2026-06-20", "消耗品費", "現金", 5000, desc="文具", slip="102"),
        ])
        issues = check_ar_ap(df)
        assert any(i["check_id"] == "4-2" for i in issues)

    def test_4_2_settled_not_flagged(self):
        """精算済みの仮払金は指摘しない"""
        from checkers.ar_ap_checker import check_ar_ap
        df = make_journal([
            je("2026-01-10", "仮払金", "普通預金", 100000, dsub="営業部", slip="101"),
            je("2026-01-25", "旅費交通費", "仮払金", 100000, csub="営業部", slip="103"),
            je("2026-06-20", "消耗品費", "現金", 5000, slip="102"),
        ])
        issues = check_ar_ap(df)
        assert not any(i["check_id"] == "4-2" for i in issues)


# ══════════════════════════════════════════════════════════
# governance_checker（5-1 / 5-2）
# ══════════════════════════════════════════════════════════
def _director_pay(amounts):
    return make_journal([
        je(f"2026-{m:02d}-25", "役員報酬", "普通預金", a, desc="役員報酬", slip=str(300 + m))
        for m, a in enumerate(amounts, start=1)
    ])


class TestGovernance:
    def test_5_1_legal_revision_not_flagged(self):
        """期首3ヶ月以内の正規改定は誤検知しない。
        （再発防止: 3ヶ月平均基準だと改定後の全月が誤検知されていた）"""
        from checkers.governance_checker import check_governance
        issues = check_governance(_director_pay([500000, 500000, 600000, 600000, 600000, 600000]))
        assert not any(i["check_id"] == "5-1" for i in issues)

    def test_5_1_mid_year_change_flagged(self):
        """4ヶ月目以降の変動は検知する"""
        from checkers.governance_checker import check_governance
        issues = check_governance(_director_pay([500000, 500000, 600000, 600000, 700000, 600000]))
        assert any(i["check_id"] == "5-1" for i in issues)

    def test_5_2_empty_description_duplicates_flagged(self):
        """摘要が両方空欄の同日・同科目・同額仕訳は重複候補にする。
        （再発防止: 類似度0扱いで検知不能だった）"""
        from checkers.governance_checker import check_governance
        df = make_journal([
            je("2026-03-05", "消耗品費", "未払金", 48000, slip="201"),
            je("2026-03-05", "消耗品費", "未払金", 48000, slip="202"),
        ])
        issues = check_governance(df)
        assert any(i["check_id"] == "5-2" for i in issues)

    def test_5_2_round_trip_not_flagged(self):
        """往復交通費（A〜B と B〜A）は重複ではない"""
        from checkers.governance_checker import check_governance
        df = make_journal([
            je("2026-03-05", "旅費交通費", "現金", 3000, desc="東京〜長岡", slip="201"),
            je("2026-03-05", "旅費交通費", "現金", 3000, desc="長岡〜東京", slip="202"),
        ])
        issues = check_governance(df)
        assert not any(i["check_id"] == "5-2" for i in issues)

    def test_5_2_different_subaccounts_not_flagged(self):
        """補助科目（取引先）が異なる同額仕訳は別取引"""
        from checkers.governance_checker import check_governance
        df = make_journal([
            je("2026-03-05", "外注費", "普通預金", 50000, dsub="A社", desc="外注費", slip="201"),
            je("2026-03-05", "外注費", "普通預金", 50000, dsub="B社", desc="外注費", slip="202"),
        ])
        issues = check_governance(df)
        assert not any(i["check_id"] == "5-2" for i in issues)


# ══════════════════════════════════════════════════════════
# tax_checker（税区分）
# ══════════════════════════════════════════════════════════
class TestTax:
    def test_debit_side_taxable_bs_account_flagged(self):
        from checkers.tax_checker import check_tax
        df = make_journal([
            je("2026-02-10", "売掛金", "売上高", 100000, dtax="課税売上10%", ctax="課税売上10%"),
        ])
        issues = check_tax(df)
        assert any(i["account"] == "売掛金" and "借方" in i["message"] for i in issues)

    def test_credit_side_taxable_bs_account_flagged(self):
        """貸方側の税区分誤りも検知する（今回の拡張）"""
        from checkers.tax_checker import check_tax
        df = make_journal([
            je("2026-02-10", "仕入高", "買掛金", 100000, dtax="課対仕入10%", ctax="課対仕入10%"),
        ])
        issues = check_tax(df)
        assert any(i["account"] == "買掛金" and "貸方" in i["message"] for i in issues)

    def test_taigai_not_flagged(self):
        """対象外なら指摘しない"""
        from checkers.tax_checker import check_tax
        df = make_journal([
            je("2026-02-10", "仕入高", "買掛金", 100000, dtax="課対仕入10%", ctax="対象外"),
        ])
        issues = check_tax(df)
        assert not any(i["account"] == "買掛金" for i in issues)


# ══════════════════════════════════════════════════════════
# completeness_checker（1-1 / fiscal_period_series）
# ══════════════════════════════════════════════════════════
class TestCompleteness:
    def test_1_1_missing_recurring_account(self):
        """定例科目（地代家賃）の欠落月を検知する"""
        from checkers.completeness_checker import check_completeness
        rows = [je(f"2026-{m:02d}-25", "地代家賃", "普通預金", 200000) for m in (1, 2, 4, 5)]
        rows += [je(f"2026-{m:02d}-10", "売上原価", "買掛金", 1000) for m in (1, 2, 3, 4, 5)]
        issues = check_completeness(make_journal(rows), 1)
        hits = [i for i in issues if i["check_id"] == "1-1" and i["account"] == "地代家賃"]
        assert hits and "2026-03" in hits[0]["month"]

    def test_fiscal_period_series_matches_scalar(self):
        """ベクトル化版が従来のスカラー版と一致する（締め日1・20）"""
        from checkers.completeness_checker import get_fiscal_period, fiscal_period_series
        dates = pd.Series(pd.to_datetime([
            "2026-01-01", "2026-01-20", "2026-01-21", "2026-02-28",
            "2026-12-05", "2025-01-15", None,
        ]))
        for cutoff in (1, 20):
            scalar = dates.apply(
                lambda d: get_fiscal_period(d, cutoff) if pd.notna(d) else pd.NaT
            )
            vector = fiscal_period_series(dates, cutoff)
            for a, b in zip(scalar, vector):
                assert (pd.isna(a) and pd.isna(b)) or a == b


# ══════════════════════════════════════════════════════════
# trend_checker（6-1 / 6-2 損益推移）
# ══════════════════════════════════════════════════════════
class TestTrend:
    def _base_rows(self):
        rows = []
        for m in range(1, 7):
            rows.append(je(f"2026-{m:02d}-25", "水道光熱費", "普通預金", 30000))
        return rows

    def test_6_1_gap_detected(self):
        """毎月あった費用の欠落月を検知する"""
        from checkers.trend_checker import check_trend
        rows = self._base_rows()
        rows += [je(f"2026-{m:02d}-20", "通信費", "普通預金", 15000) for m in (1, 2, 3, 5, 6)]
        issues = check_trend(make_journal(rows), 1)
        hits = [i for i in issues if i["check_id"] == "6-1" and i["account"] == "通信費"]
        assert hits and "2026-04" in hits[0]["month"]

    def test_6_1_defers_to_1_1_for_fixed_list(self):
        """1-1が扱う定例科目（地代家賃）は6-1では重複指摘しない"""
        from checkers.trend_checker import check_trend
        rows = self._base_rows()
        rows += [je(f"2026-{m:02d}-25", "地代家賃", "普通預金", 200000) for m in (1, 2, 3, 5, 6)]
        issues = check_trend(make_journal(rows), 1)
        assert not any(i["account"] == "地代家賃" for i in issues)

    def test_6_1_non_pl_excluded(self):
        """BS科目（買掛金の支払等）は対象外"""
        from checkers.trend_checker import check_trend
        rows = self._base_rows()
        rows += [je(f"2026-{m:02d}-25", "買掛金", "普通預金", 88000) for m in (1, 2, 3, 5, 6)]
        issues = check_trend(make_journal(rows), 1)
        assert not any(i["account"] == "買掛金" for i in issues)

    def test_6_2_subaccount_gap_detected(self):
        """科目自体は毎月あるが、特定の取引先だけ欠落した月を検知する"""
        from checkers.trend_checker import check_trend
        rows = self._base_rows()
        for m in range(1, 7):
            if m != 3:
                rows.append(je(f"2026-{m:02d}-25", "外注費", "普通預金", 100000, dsub="B社"))
            rows.append(je(f"2026-{m:02d}-25", "外注費", "普通預金", 50000, dsub="C社"))
        issues = check_trend(make_journal(rows), 1)
        hits = [i for i in issues if i["check_id"] == "6-2" and i["account"] == "外注費"]
        assert hits and "2026-03" in hits[0]["month"]

    def test_6_2_no_subaccount_stream_detected(self):
        """補助科目なしの定期費用の欠落も検知する（今回の拡張）"""
        from checkers.trend_checker import check_trend
        rows = self._base_rows()
        for m in range(1, 7):
            if m != 5:
                rows.append(je(f"2026-{m:02d}-25", "広告宣伝費", "普通預金", 8000, desc="月額掲載料"))
            rows.append(je(f"2026-{m:02d}-25", "広告宣伝費", "普通預金", 300, dsub="クラウドA"))
        issues = check_trend(make_journal(rows), 1)
        hits = [i for i in issues if i["check_id"] == "6-2" and "補助科目なし" in i["message"]]
        assert hits and "2026-05" in hits[0]["month"]

    def test_scope_pl_and_cogs(self):
        """損益・製造原価は対象、棚卸資産（完全一致）・BS科目は対象外"""
        from checkers.trend_checker import _is_pl_account
        assert _is_pl_account("商品仕入高")
        assert _is_pl_account("材料費")
        assert _is_pl_account("外注加工費")
        assert _is_pl_account("期末商品棚卸高")
        assert not _is_pl_account("商品")
        assert not _is_pl_account("仕掛品")
        assert not _is_pl_account("買掛金")
        assert not _is_pl_account("普通預金")


# ══════════════════════════════════════════════════════════
# asset_checker（3-1 / 3-2）
# ══════════════════════════════════════════════════════════
class TestAssets:
    def test_3_2_small_asset_flagged(self):
        """固定資産科目に10万円未満の計上 → 費用化すべき"""
        from checkers.asset_checker import check_assets
        df = make_journal([je("2026-04-10", "工具器具備品", "未払金", 80000, slip="1")])
        issues = check_assets(df)
        assert any(i["check_id"] == "3-2" for i in issues)

    def test_3_1_sme_range_flagged(self):
        """10万〜30万の固定資産計上 → 中小企業特例の提案"""
        from checkers.asset_checker import check_assets
        df = make_journal([je("2026-04-10", "工具器具備品", "未払金", 250000, slip="1")])
        issues = check_assets(df)
        assert any(i["check_id"] == "3-1" for i in issues)

    def test_over_300k_not_flagged(self):
        """30万円以上の資産計上は正常（指摘しない）"""
        from checkers.asset_checker import check_assets
        df = make_journal([je("2026-04-10", "工具器具備品", "未払金", 500000, slip="1")])
        issues = check_assets(df)
        assert not any(i["check_id"] in ("3-1", "3-2") for i in issues)


# ══════════════════════════════════════════════════════════
# bs_checker（現預金マイナス残高）
# ══════════════════════════════════════════════════════════
class TestBs:
    def test_negative_cash_balance_flagged(self):
        """期首残高を加味した口座残高がマイナス → 検知"""
        from checkers.bs_checker import check_bs
        df = make_journal([
            je("2026-01-15", "消耗品費", "普通預金", 150000),
            je("2026-02-15", "消耗品費", "普通預金", 100000),
        ])
        issues = check_bs(df, {"普通預金": 200000.0})
        assert any("マイナス" in i["message"] for i in issues)

    def test_positive_cash_balance_not_flagged(self):
        from checkers.bs_checker import check_bs
        df = make_journal([
            je("2026-01-15", "消耗品費", "普通預金", 150000),
        ])
        issues = check_bs(df, {"普通預金": 200000.0})
        assert not any("マイナス" in i["message"] for i in issues)
