"""
優先度B・Cの追加論点（2-12 / 7-3 / 7-4 / 5-6 / 8-1〜8-5）のテスト。
各チェックについて「検知できること」と「誤検知しないこと」の両方を確認する。
"""
from conftest import make_journal, entry as je


def _filler(months=6):
    return [je(f"2026-{m:02d}-05", "水道光熱費", "普通預金", 30000 + m * 137)
            for m in range(1, months + 1)]


# ══════════════════════════════════════════════════════════
# 2-12: 消費税額の整合（B-1）
# ══════════════════════════════════════════════════════════
class TestTaxAmountConsistency:
    def _run(self, rows):
        from checkers.tax_detail_checker import check_tax_detail
        return [i for i in check_tax_detail(make_journal(rows)) if i["check_id"] == "2-12"]

    def test_wrong_tax_amount_detected(self):
        """税区分10%なのに税額が計算上ありえない仕訳を検知する"""
        rows = [dict(je("2026-03-05", "消耗品費", "現金", 110000, dtax="課対仕入10%"),
                     debit_tax_amt=3000.0)]  # 正しくは10,000円（内税）
        assert self._run(rows)

    def test_inclusive_tax_ok(self):
        """税込金額×10/110 の税額は正常"""
        rows = [dict(je("2026-03-05", "消耗品費", "現金", 110000, dtax="課対仕入10%"),
                     debit_tax_amt=10000.0)]
        assert not self._run(rows)

    def test_exclusive_tax_ok(self):
        """税抜金額×10% の税額（外税）も正常"""
        rows = [dict(je("2026-03-05", "消耗品費", "現金", 100000, dtax="課対仕入10%"),
                     debit_tax_amt=10000.0)]
        assert not self._run(rows)

    def test_reduced_rate_checked(self):
        """8%軽減の税額が10%相当になっている場合を検知する"""
        rows = [dict(je("2026-03-05", "会議費", "現金", 108000, dtax="課対仕入8%(軽)"),
                     debit_tax_amt=9818.0)]  # 10/110相当。正しくは8,000円
        assert self._run(rows)

    def test_zero_tax_amt_skipped(self):
        """税額が0（内訳情報なし）の仕訳は判定しない"""
        rows = [dict(je("2026-03-05", "消耗品費", "現金", 110000, dtax="課対仕入10%"),
                     debit_tax_amt=0.0)]
        assert not self._run(rows)

    def test_rounding_tolerance(self):
        """端数処理（切捨て/四捨五入）の数円差は許容する"""
        rows = [dict(je("2026-03-05", "消耗品費", "現金", 10945, dtax="課対仕入10%"),
                     debit_tax_amt=995.0)]  # 10/110=995.0 前後の丸め
        assert not self._run(rows)


# ══════════════════════════════════════════════════════════
# 7-3: 預り金の納付サイクル（B-2）
# ══════════════════════════════════════════════════════════
class TestWithholdingCycle:
    def _run(self, rows):
        from checkers.reconciliation_checker import check_reconciliation
        return [i for i in check_reconciliation(make_journal(rows)) if i["check_id"] == "7-3"]

    def _cycle(self, months=6, stop_after=None):
        """毎月給与から源泉を預かり、翌月納付するサイクル"""
        rows = []
        for m in range(1, months + 1):
            rows.append(je(f"2026-{m:02d}-25", "給料手当", "預り金", 84000,
                           csub="源泉所得税", desc="源泉所得税預り"))
        for m in range(2, months + 1):
            if stop_after and m > stop_after:
                continue
            rows.append(je(f"2026-{m:02d}-10", "預り金", "普通預金", 84000,
                           dsub="源泉所得税", desc="源泉所得税納付"))
        return rows

    def test_stopped_payment_detected(self):
        """それまで毎月納付されていたのに止まった → 納付漏れの疑い"""
        hits = self._run(self._cycle(stop_after=4))
        assert hits and hits[0]["level"] == "warning"
        assert "納付" in hits[0]["message"]

    def test_normal_cycle_not_flagged(self):
        assert not self._run(self._cycle())

    def test_never_paid_info(self):
        """一度も納付が無い場合は納期特例の可能性込みで情報提供"""
        rows = [je(f"2026-{m:02d}-25", "給料手当", "預り金", 84000,
                   csub="源泉所得税") for m in range(1, 7)]
        hits = self._run(rows)
        assert hits and hits[0]["level"] == "info"
        assert "納期特例" in hits[0]["message"]


# ══════════════════════════════════════════════════════════
# 7-4: 前払費用の按分進行（C-2）
# ══════════════════════════════════════════════════════════
class TestPrepaidAmortization:
    def _run(self, rows):
        from checkers.reconciliation_checker import check_reconciliation
        return [i for i in check_reconciliation(make_journal(rows)) if i["check_id"] == "7-4"]

    def test_unamortized_prepaid_detected(self):
        """年払い保険料が計上されたまま取崩しゼロ → 確認喚起"""
        rows = _filler()
        rows.append(je("2026-01-15", "前払費用", "普通預金", 360000, desc="年払保険料"))
        hits = self._run(rows)
        assert hits and "按分" in hits[0]["message"]

    def test_amortizing_not_flagged(self):
        """毎月取り崩していれば指摘しない"""
        rows = _filler()
        rows.append(je("2026-01-15", "前払費用", "普通預金", 360000, desc="年払保険料"))
        for m in range(2, 7):
            rows.append(je(f"2026-{m:02d}-28", "保険料", "前払費用", 30000, desc="月次按分"))
        assert not self._run(rows)

    def test_recent_prepaid_not_flagged(self):
        """計上から日が浅いものは指摘しない"""
        rows = _filler()
        rows.append(je("2026-05-15", "前払費用", "普通預金", 360000, desc="年払保険料"))
        assert not self._run(rows)


# ══════════════════════════════════════════════════════════
# 5-6: 交際費の年間限度額（B-3）
# ══════════════════════════════════════════════════════════
class TestEntertainmentLimit:
    def _run(self, rows):
        from checkers.governance_checker import check_governance
        return [i for i in check_governance(make_journal(rows)) if i["check_id"] == "5-6"]

    def test_over_limit_warning(self):
        rows = [je(f"2026-{m:02d}-15", "接待交際費", "普通預金", 1_500_000)
                for m in range(1, 7)]  # 累計900万
        hits = self._run(rows)
        assert hits and hits[0]["level"] == "warning"
        assert "800万" in hits[0]["message"]

    def test_approaching_info(self):
        rows = [je(f"2026-{m:02d}-15", "接待交際費", "普通預金", 1_100_000)
                for m in range(1, 7)]  # 累計660万
        hits = self._run(rows)
        assert hits and hits[0]["level"] == "info"

    def test_normal_not_flagged(self):
        rows = [je(f"2026-{m:02d}-15", "接待交際費", "普通預金", 300_000)
                for m in range(1, 7)]  # 累計180万
        assert not self._run(rows)


# ══════════════════════════════════════════════════════════
# 8-1: 借入金と支払利息（B-4）
# ══════════════════════════════════════════════════════════
class TestLoanInterest:
    def _run(self, rows):
        from checkers.consistency_checker import check_consistency
        return [i for i in check_consistency(make_journal(rows)) if i["check_id"] == "8-1"]

    def test_repayment_without_interest_detected(self):
        """返済があるのに利息の無い月が続く → 検知"""
        rows = _filler()
        for m in range(1, 7):
            rows.append(je(f"2026-{m:02d}-27", "長期借入金", "普通預金", 200000, desc="借入返済"))
            if m <= 2:  # 3月以降、利息が計上されていない
                rows.append(je(f"2026-{m:02d}-27", "支払利息", "普通預金", 15000))
        hits = self._run(rows)
        assert hits and hits[0]["level"] == "warning"

    def test_normal_repayment_not_flagged(self):
        rows = _filler()
        for m in range(1, 7):
            rows.append(je(f"2026-{m:02d}-27", "長期借入金", "普通預金", 200000))
            rows.append(je(f"2026-{m:02d}-27", "支払利息", "普通預金", 15000))
        assert not self._run(rows)

    def test_officer_loan_excluded(self):
        """役員借入金の返済は無利息が普通なので対象外"""
        rows = _filler()
        for m in range(1, 7):
            rows.append(je(f"2026-{m:02d}-27", "役員短期借入金", "普通預金", 200000))
        assert not self._run(rows)

    def test_interest_without_loan_info(self):
        """借入金の動きが無いのに支払利息が続く → 科目確認の情報提供"""
        rows = _filler()
        for m in range(1, 7):
            rows.append(je(f"2026-{m:02d}-27", "支払利息", "普通預金", 15000))
        hits = self._run(rows)
        assert hits and hits[0]["level"] == "info"


# ══════════════════════════════════════════════════════════
# 8-2: 期ズレの兆候（C-1）
# ══════════════════════════════════════════════════════════
class TestPeriodShift:
    def _run(self, rows):
        from checkers.consistency_checker import check_consistency
        return [i for i in check_consistency(make_journal(rows)) if i["check_id"] == "8-2"]

    def test_month_end_concentration_detected(self):
        """月間売上の6割以上が月末3日に集中 → 情報提供"""
        rows = _filler()
        for d in range(1, 26):  # 月中は少額
            rows.append(je(f"2026-03-{d:02d}", "売掛金", "売上高", 20000))
        for k in range(10):     # 月末に大口が集中
            rows.append(je("2026-03-30", "売掛金", "売上高", 200000))
        hits = [i for i in self._run(rows) if "集中" in i["message"]]
        assert hits

    def test_even_sales_not_flagged(self):
        rows = _filler()
        for d in range(1, 29, 2):
            rows.append(je(f"2026-03-{d:02d}", "売掛金", "売上高", 100000))
        assert not [i for i in self._run(rows) if "集中" in i["message"]]

    def test_beginning_reversal_detected(self):
        """月初の売上取消（借方売上）を検知する"""
        rows = _filler()
        rows.append(je("2026-04-01", "売上高", "売掛金", 500000, desc="3月売上取消"))
        hits = [i for i in self._run(rows) if "取消" in i["message"]]
        assert hits and hits[0]["level"] == "warning"

    def test_returns_excluded(self):
        """返品・値引は通常の商行為なので指摘しない"""
        rows = _filler()
        rows.append(je("2026-04-01", "売上高", "売掛金", 500000, desc="商品返品"))
        assert not [i for i in self._run(rows) if "取消" in i["message"]]


# ══════════════════════════════════════════════════════════
# 8-3: 役員貸付金・役員借入金（C-3）
# ══════════════════════════════════════════════════════════
class TestOfficerLoans:
    def _run(self, rows):
        from checkers.consistency_checker import check_consistency
        return [i for i in check_consistency(make_journal(rows)) if i["check_id"] == "8-3"]

    def test_officer_lending_detected(self):
        rows = _filler()
        rows.append(je("2026-02-10", "役員貸付金", "普通預金", 2_000_000))
        hits = self._run(rows)
        assert hits and "認定利息" in hits[0]["message"]

    def test_officer_borrowing_increase_info(self):
        rows = _filler()
        for m in range(1, 4):
            rows.append(je(f"2026-{m:02d}-10", "普通預金", "役員借入金", 1_000_000))
        hits = [i for i in self._run(rows) if i["account"] == "役員借入金"]
        assert hits and hits[0]["level"] == "info"

    def test_no_officer_loans_not_flagged(self):
        assert not self._run(_filler())


# ══════════════════════════════════════════════════════════
# 8-4: 減価償却の開始（C-4）
# ══════════════════════════════════════════════════════════
class TestDepreciationStart:
    def _run(self, rows):
        from checkers.consistency_checker import check_consistency
        return [i for i in check_consistency(make_journal(rows)) if i["check_id"] == "8-4"]

    def test_unchanged_depreciation_detected(self):
        """期中に資産取得したのに償却月額が変わらない → 確認喚起"""
        rows = _filler()
        for m in range(1, 7):
            rows.append(je(f"2026-{m:02d}-28", "減価償却費", "減価償却累計額", 50000))
        rows.append(je("2026-03-15", "機械装置", "未払金", 1_200_000))
        hits = self._run(rows)
        assert hits and "登録" in hits[0]["message"]

    def test_increased_depreciation_not_flagged(self):
        """取得後に償却月額が増えていれば正常"""
        rows = _filler()
        for m in range(1, 7):
            amt = 50000 if m <= 3 else 70000
            rows.append(je(f"2026-{m:02d}-28", "減価償却費", "減価償却累計額", amt))
        rows.append(je("2026-03-15", "機械装置", "未払金", 1_200_000))
        assert not self._run(rows)

    def test_annual_depreciation_not_judged(self):
        """月次償却をしていない（決算一括）会社は対象外"""
        rows = _filler()
        rows.append(je("2026-03-15", "機械装置", "未払金", 1_200_000))
        assert not self._run(rows)


# ══════════════════════════════════════════════════════════
# 8-5: 現金残高の過大（C-5）
# ══════════════════════════════════════════════════════════
class TestCashBalance:
    def _run(self, rows, ob=None):
        from checkers.consistency_checker import check_consistency
        return [i for i in check_consistency(make_journal(rows), ob or {})
                if i["check_id"] == "8-5"]

    def test_large_cash_detected(self):
        rows = _filler()
        for m in range(1, 7):
            rows.append(je(f"2026-{m:02d}-15", "現金", "売上高", 100000))
        hits = self._run(rows, {"現金": 2_000_000.0})
        assert hits and "実査" in hits[0]["message"]

    def test_small_cash_not_flagged(self):
        rows = _filler()
        for m in range(1, 7):
            rows.append(je(f"2026-{m:02d}-15", "現金", "売上高", 10000))
            rows.append(je(f"2026-{m:02d}-20", "消耗品費", "現金", 8000))
        assert not self._run(rows, {"現金": 100_000.0})

    def test_no_opening_balance_skipped(self):
        """期首残高が無い場合は判定しない（誤検知防止）"""
        rows = _filler()
        for m in range(1, 7):
            rows.append(je(f"2026-{m:02d}-15", "現金", "売上高", 500000))
        assert not self._run(rows, {})
