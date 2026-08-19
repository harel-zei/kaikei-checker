"""
実務のチェックシートで担当者が指摘したがシステムが拾えなかった観点のテスト。

対象（2026年6月分チェックシートより）:
  ① 新聞図書費: 毎月計上されている日経電子版購読料の入力がない
     → 補助科目が無く、科目自体は他取引で毎月あるため 6-1/6-2 では検知不可
  ② リース料（自動車ヴィッツ）: 計上がない（解約されたか？）
     → 期の途中で途絶えたため「全期間の75%以上」条件の 6-1/6-2 では検知不可
  ③ 未払費用（経費精算・清水）: 計上と支払に差額（8,031円多く支払）
     → 補助科目の期首残高が無いと bs_checker は判定をスキップするため検知不可
  ④ 未払金（アメックスカード）: ③と同額が残っており科目の取り違え
     → 科目をまたいだ同額の突き合わせを行っていなかったため検知不可
"""
from conftest import make_journal, entry as je


def _trend(rows, cid=None):
    from checkers.trend_checker import check_trend
    issues = check_trend(make_journal(rows), 1)
    return [i for i in issues if cid is None or i["check_id"] == cid]


def _recon(rows, cid=None):
    from checkers.reconciliation_checker import check_reconciliation
    issues = check_reconciliation(make_journal(rows))
    return [i for i in issues if cid is None or i["check_id"] == cid]


def _filler(month, rows):
    """科目全体は毎月存在させるための埋め草仕訳"""
    rows.append(je(f"2026-{month:02d}-05", "水道光熱費", "普通預金", 30000))


# ══════════════════════════════════════════════════════════
# ① 6-3: 摘要レベルの定期費用の欠落
# ══════════════════════════════════════════════════════════
class TestDescriptionLevelGap:
    def test_subscription_gap_detected(self):
        """補助科目が無くても、摘要で識別できる定期購読の欠落を検知する"""
        rows = []
        for m in range(1, 7):
            _filler(m, rows)
            if m != 5:
                rows.append(je(f"2026-{m:02d}-25", "新聞図書費", "普通預金", 4277,
                               desc=f"日経電子版 {m}月分"))
            # 同じ科目に別の取引が毎月あるため、科目全体では欠落に見えない
            rows.append(je(f"2026-{m:02d}-10", "新聞図書費", "現金", 1500 + m * 10, desc="書籍代"))
        hits = _trend(rows, "6-3")
        assert hits, "摘要レベルの定期費用の欠落が検知されていない"
        assert "2026-05" in hits[0]["month"]
        assert "日経電子版" in hits[0]["message"]

    def test_blank_description_detected_by_amount(self):
        """摘要が空欄でも、毎月同額であれば定期取引として識別し欠落を検知する。

        弥生では摘要が未入力の仕訳も多く、摘要キーだけでは識別できない。
        （実データで検知できなかった事象の再発防止）
        """
        rows = []
        for m in range(1, 7):
            _filler(m, rows)
            if m != 5:
                rows.append(je(f"2026-{m:02d}-25", "新聞図書費", "普通預金", 4277, desc=""))
            rows.append(je(f"2026-{m:02d}-10", "新聞図書費", "現金", 1500 + m * 230,
                           desc="書籍代"))
        hits = _trend(rows, "6-3")
        assert hits, "摘要が空欄の定期購読の欠落が検知されていない"
        assert "4,277" in hits[0]["message"]

    def test_unstable_amount_not_flagged(self):
        """金額が毎月大きく変わる取引は定期購読ではないので指摘しない"""
        rows = []
        for m in range(1, 7):
            _filler(m, rows)
            if m != 4:
                rows.append(je(f"2026-{m:02d}-15", "消耗品費", "現金", 1000 * m * 3,
                               desc="事務用品購入"))
        assert not _trend(rows, "6-3")

    def test_frequent_same_amount_not_flagged(self):
        """同じ金額が月内に何度も現れる取引は定期取引ではないので指摘しない"""
        rows = []
        for m in range(1, 7):
            _filler(m, rows)
            for k in range(0 if m == 4 else 5):
                rows.append(je(f"2026-{m:02d}-{10 + k:02d}", "旅費交通費", "現金", 1200,
                               desc="交通費"))
        assert not _trend(rows, "6-3")

    def test_no_duplicate_with_6_1(self):
        """科目全体が欠落している場合は 6-1 が扱い、6-3 で重複指摘しない"""
        rows = []
        for m in range(1, 7):
            _filler(m, rows)
            if m != 4:
                rows.append(je(f"2026-{m:02d}-25", "通信費", "普通預金", 15000, desc="携帯電話料金"))
        assert _trend(rows, "6-1"), "6-1 が検知しているはず"
        assert not _trend(rows, "6-3"), "6-1 と重複して 6-3 が指摘している"


# ══════════════════════════════════════════════════════════
# ② 6-4: 定期取引の途絶（解約確認）
# ══════════════════════════════════════════════════════════
class TestDiscontinued:
    def test_discontinued_lease_detected(self):
        """期の途中で計上が止まった定期取引を検知する"""
        rows = []
        for m in range(1, 7):
            _filler(m, rows)
            if m <= 3:
                rows.append(je(f"2026-{m:02d}-25", "リース料", "普通預金", 38000,
                               dsub="自動車ヴィッツ"))
            # 同じ科目の別契約は継続しているため科目全体では気づけない
            rows.append(je(f"2026-{m:02d}-25", "リース料", "普通預金", 12000, dsub="複合機"))
        hits = _trend(rows, "6-4")
        assert hits, "途絶した定期取引が検知されていない"
        assert "自動車ヴィッツ" in hits[0]["message"]

    def test_ongoing_not_flagged(self):
        """期末まで継続している取引は指摘しない"""
        rows = []
        for m in range(1, 7):
            _filler(m, rows)
            rows.append(je(f"2026-{m:02d}-25", "リース料", "普通預金", 38000, dsub="複合機"))
        assert not _trend(rows, "6-4")

    def test_sporadic_not_flagged(self):
        """散発的な単発取引（3ヶ月未満）は指摘しない"""
        rows = []
        for m in range(1, 7):
            _filler(m, rows)
        for m in (1, 2):
            rows.append(je(f"2026-{m:02d}-20", "支払報酬", "普通預金", 50000, dsub="スポット案件"))
        assert not _trend(rows, "6-4")


# ══════════════════════════════════════════════════════════
# ③④ 7-1 / 7-2: 経過勘定の未清算差額と科目取り違え
# ══════════════════════════════════════════════════════════
# 経費精算は毎月金額が変動するのが実態のため、変動する金額でテストする
EXPENSE_AMOUNTS = [48200, 51300, 49800, 50100, 52000, 47600]


def _expense_settlement_rows(overpay_month=None, overpay=0):
    """毎月計上し翌月に支払う経費精算のサイクルを作る（金額は毎月変動）"""
    rows = []
    for m in range(1, 7):
        # 日付は全ての月に存在する28日を使う（2/30 等の無効日付を避ける）
        rows.append(je(f"2026-{m:02d}-28", "給料手当", "未払費用", EXPENSE_AMOUNTS[m - 1],
                       csub="経費精算(清水)", desc="経費精算計上"))
    for m in range(2, 7):
        amt = EXPENSE_AMOUNTS[m - 2] + (overpay if m == overpay_month else 0)
        rows.append(je(f"2026-{m:02d}-10", "未払費用", "普通預金", amt,
                       dsub="経費精算(清水)", desc="経費精算支払"))
    return rows


# カード利用額も毎月変動する
CARD_AMOUNTS = [118400, 131200, 109700, 124300, 127600, 115900]


def _card_rows(extra_amount=0, extra_desc="", extra_slip=""):
    """毎月のカード利用と翌月引落のサイクルを作る（金額は毎月変動）"""
    rows = []
    for m in range(1, 7):
        rows.append(je(f"2026-{m:02d}-27", "消耗品費", "未払金", CARD_AMOUNTS[m - 1],
                       csub="アメックスカード", desc="カード利用"))
    for m in range(2, 7):
        rows.append(je(f"2026-{m:02d}-10", "未払金", "普通預金", CARD_AMOUNTS[m - 2],
                       dsub="アメックスカード", desc="カード引落"))
    if extra_amount:
        rows.append(je("2026-05-31", "交際費", "未払金", extra_amount,
                       csub="アメックスカード", desc=extra_desc, slip=extra_slip))
    return rows


class TestUnsettledDifference:
    def test_overpayment_detected(self):
        """計上額より支払額が多い（払い過ぎ）ケースを検知する。
        補助科目の期首残高が無くても、当期の計上と精算の差から判定できる。"""
        hits = _recon(_expense_settlement_rows(overpay_month=5, overpay=8031), "7-1")
        assert hits, "支払超過が検知されていない"
        assert "8,031" in hits[0]["message"]

    def test_clean_cycle_not_flagged(self):
        """計上と支払が一致していれば指摘しない"""
        assert not _recon(_expense_settlement_rows(), "7-1")

    def test_variable_amounts_with_difference_detected(self):
        """毎月の金額が変動していても差額を検知する。

        経費精算やカード利用は毎月の金額が変動するのが実態であり、
        「毎月ほぼ定額」を前提にした判定では実データを取りこぼす。
        累計で突き合わせることで金額の変動に影響されずに判定する。
        （実データで検知できなかった事象の再発防止）
        """
        rows = _expense_settlement_rows(overpay_month=5, overpay=8031)
        hits = _recon(rows, "7-1")
        assert hits, "金額が変動する経過勘定の差額が検知されていない"
        assert "8,031" in hits[0]["message"]

    def test_variable_amounts_clean_not_flagged(self):
        """金額が毎月変動しても、計上と精算が対応していれば指摘しない"""
        rows = []
        for m in range(1, 7):
            rows.append(je(f"2026-{m:02d}-28", "外注費", "未払金", 100000 * m, csub="変動社"))
        for m in range(2, 7):
            rows.append(je(f"2026-{m:02d}-10", "未払金", "普通預金", 100000 * (m - 1),
                           dsub="変動社"))
        assert not _recon(rows, "7-1")

    def test_last_month_unpaid_not_flagged(self):
        """期末月の支払がまだ行われていないのは正常（差額ではない）"""
        rows = [r for r in _expense_settlement_rows()
                if not (r["debit_account"] == "未払費用" and r["date"].startswith("2026-06"))]
        assert not _recon(rows, "7-1")

    def test_delayed_payment_resolved_not_flagged(self):
        """支払が一時的に遅れても、翌月にまとめて精算されていれば指摘しない"""
        rows = []
        for m in range(1, 7):
            rows.append(je(f"2026-{m:02d}-28", "給料手当", "未払費用", 50000, csub="Y社"))
        # 3月は支払わず、4月に2ヶ月分をまとめて精算
        for m, amt in ((2, 50000), (4, 100000), (5, 50000), (6, 50000)):
            rows.append(je(f"2026-{m:02d}-10", "未払費用", "普通預金", amt, dsub="Y社"))
        assert not _recon(rows, "7-1")


class TestAccountMisposting:
    def test_same_amount_across_accounts_detected(self):
        """異なる科目に同額の未清算端数が残っている＝科目取り違えの可能性"""
        rows = _expense_settlement_rows(overpay_month=5, overpay=8031)
        rows += _card_rows(extra_amount=8031, extra_desc="手土産代 大木製薬訪問",
                           extra_slip="810")
        hits = _recon(rows, "7-2")
        assert hits, "科目をまたいだ同額の未清算残高が検知されていない"
        msg = hits[0]["message"]
        assert "8,031" in msg
        assert "未払費用" in msg and "未払金" in msg
        # 手掛かりとして該当仕訳（伝票番号・摘要）を提示する
        assert "810" in msg and "手土産代" in msg

    def test_single_account_not_flagged(self):
        """1科目にしか差額がなければ取り違えではない"""
        rows = _expense_settlement_rows(overpay_month=5, overpay=8031)
        assert not _recon(rows, "7-2")

    def test_same_account_different_subs_not_flagged(self):
        """同一科目内の補助科目同士で同額でも科目取り違えではない"""
        rows = []
        for sub in ("X社", "Y社"):
            for m in range(1, 7):
                rows.append(je(f"2026-{m:02d}-30", "外注費", "未払金", 100000, csub=sub))
            for m in range(2, 7):
                amt = 105000 if m == 5 else 100000
                rows.append(je(f"2026-{m:02d}-10", "未払金", "普通預金", amt, dsub=sub))
        assert not _recon(rows, "7-2")
