"""
記帳パターンの網羅テスト。

顧問先ごとに記帳の仕方は異なる（補助科目の付け方・精算のタイミング・
摘要の書き方・消費税端数など）。特定の記帳パターンを前提にした判定は
実データを取りこぼすため、実務でありうるパターンを一通り並べて
「検知できること」と「誤検知しないこと」の両方を固定する。

このテストは、実データで検知できなかった事象の再発防止を目的とする。
"""
import pytest

from conftest import make_journal, entry as je

# 経費精算・カード利用は毎月金額が変動するのが実態
ACCRUAL = [48200, 51300, 49800, 50100, 52000, 47600,
           49100, 50800, 51900, 48700, 50300, 49500]
DIFF = 8031


def _filler(months=6):
    """科目全体を毎月存在させるための埋め草"""
    return [je(f"2026-{m:02d}-05", "水道光熱費", "普通預金", 30000 + m * 137)
            for m in range(1, months + 1)]


def _recon_ids(rows, cid="7-1"):
    from checkers.reconciliation_checker import check_reconciliation
    return [i for i in check_reconciliation(make_journal(rows)) if i["check_id"] == cid]


def _trend_ids(rows, cids=("6-1", "6-2", "6-3", "6-4")):
    from checkers.trend_checker import check_trend
    return [i for i in check_trend(make_journal(rows), 1) if i["check_id"] in cids]


# ══════════════════════════════════════════════════════════
# 7-1: 経過勘定の差額 ― 記帳パターン別に検知できること
# ══════════════════════════════════════════════════════════
def _settlement_cycle(months=6, lag=1, dsub="経費精算(清水)", csub="経費精算(清水)",
                      diff_month=5, opening=None, split=False, compound=False,
                      shortfall=False):
    """計上→精算のサイクルを作る。diff_month の精算に DIFF 円の差額を混ぜる。"""
    rows = []
    for m in range(1, months + 1):
        # 日付は全ての月に存在する28日を使う（2/30 等の無効日付を避ける）
        rows.append(je(f"2026-{m:02d}-28", "" if compound else "給料手当", "未払費用",
                       ACCRUAL[m - 1], csub=csub, desc="経費精算計上"))
    if opening:
        rows.append(je("2026-01-10", "未払費用", "普通預金", opening,
                       dsub=dsub, desc="前期分支払"))
    for m in range(1 + lag, months + 1):
        amt = ACCRUAL[m - 1 - lag]
        if m == diff_month:
            amt += -DIFF if shortfall else DIFF
        if split:
            rows.append(je(f"2026-{m:02d}-10", "未払費用", "普通預金", amt // 2, dsub=dsub))
            rows.append(je(f"2026-{m:02d}-20", "未払費用", "普通預金", amt - amt // 2, dsub=dsub))
        else:
            rows.append(je(f"2026-{m:02d}-10", "未払費用", "普通預金", amt,
                           dsub=dsub, desc="経費精算支払"))
    return rows


@pytest.mark.parametrize("name,kwargs", [
    ("補助科目が両側にある・翌月精算", {}),
    ("補助科目が支払側だけ無い",       {"dsub": ""}),
    ("補助科目が計上側だけ無い",       {"csub": ""}),
    ("補助科目なし（科目のみ）",       {"dsub": "", "csub": ""}),
    ("当月精算",                       {"lag": 0}),
    ("翌々月精算",                     {"lag": 2}),
    ("期首残高あり",                   {"opening": 47000}),
    ("12ヶ月データ",                   {"months": 12, "diff_month": 9}),
    ("複合仕訳（相手科目が空欄）",     {"compound": True}),
    ("精算が月2回に分割",              {"split": True}),
    ("差額が最終月に発生",             {"diff_month": 6}),
    ("差額が期央に発生",               {"diff_month": 3}),
    ("支払不足（少なく払った）",       {"shortfall": True}),
])
def test_7_1_detects_difference(name, kwargs):
    hits = _recon_ids(_settlement_cycle(**kwargs))
    assert hits, f"{name}: 差額が検知されていない"
    assert abs(hits[0]["detail"]["amount"] - DIFF) < 1, \
        f"{name}: 差額が {hits[0]['detail']['amount']:,.0f}円 と誤っている"


@pytest.mark.parametrize("name,kwargs", [
    ("正常サイクル（翌月精算）",   {}),
    ("正常サイクル（当月精算）",   {"lag": 0}),
    ("正常サイクル（翌々月精算）", {"lag": 2}),
    ("正常＋期首残高あり",         {"opening": 47000}),
    ("正常・12ヶ月",               {"months": 12}),
])
def test_7_1_no_false_positive(name, kwargs):
    assert not _recon_ids(_settlement_cycle(diff_month=None, **kwargs)), f"{name}: 誤検知"


def test_7_1_last_month_unpaid_not_flagged():
    """期末月の支払がまだなのは正常（差額ではない）"""
    rows = [r for r in _settlement_cycle(diff_month=None)
            if not (r["debit_account"] == "未払費用" and r["date"].startswith("2026-06"))]
    assert not _recon_ids(rows)


def test_7_1_two_months_unpaid_not_flagged():
    """期末2ヶ月分が未払でも正常"""
    rows = [r for r in _settlement_cycle(diff_month=None)
            if not (r["debit_account"] == "未払費用" and r["date"][:7] in ("2026-05", "2026-06"))]
    assert not _recon_ids(rows)


def test_7_1_irregular_payments_not_judged():
    """支払が不規則で消込サイクルと言えない場合は判定しない"""
    import random
    rng = random.Random(7)
    rows = [je(f"2026-{m:02d}-28", "給料手当", "未払費用", ACCRUAL[m - 1], csub="W")
            for m in range(1, 7)]
    for m in range(2, 7):
        rows.append(je(f"2026-{m:02d}-10", "未払費用", "普通預金",
                       rng.randint(20000, 80000), dsub="W"))
    assert not _recon_ids(rows)


def test_7_1_many_clean_subaccounts_not_flagged():
    """補助科目が多数あってもすべて正常なら指摘しない"""
    rows = []
    for s in range(10):
        amt = 30000 + s * 1000
        for m in range(1, 7):
            rows.append(je(f"2026-{m:02d}-28", "外注費", "未払金", amt, csub=f"社{s}"))
        for m in range(2, 7):
            rows.append(je(f"2026-{m:02d}-10", "未払金", "普通預金", amt, dsub=f"社{s}"))
    assert not _recon_ids(rows)


def test_7_1_detects_in_noisy_account():
    """同じ科目に補助科目が複数ぶら下がり、金額も月ごとに揺れる
    （実データに近い）状況でも差額を検知する。

    テストデータが整いすぎていると、実データで通用しない判定を
    「動いている」と誤認してしまうため、意図的に雑音を入れて確認する。
    """
    import random
    rng = random.Random(1)
    rows = _settlement_cycle()  # 清水さんの経費精算（5月に DIFF 円の過払い）
    # 同じ未払費用に別の補助科目がぶら下がる（実務では普通）
    for sub, base in (("社会保険料", 380000), ("役員退職金", 30000000), ("水道光熱費", 62000)):
        for m in range(1, 7):
            rows.append(je(f"2026-{m:02d}-28", "法定福利費", "未払費用",
                           base + rng.randint(-3000, 3000), csub=sub))
        if sub == "役員退職金":
            continue  # 未払のまま残る（正常）
        for m in range(2, 7):
            rows.append(je(f"2026-{m:02d}-10", "未払費用", "普通預金",
                           base + rng.randint(-3000, 3000), dsub=sub))
    hits = [h for h in _recon_ids(rows) if "清水" in h["account"]]
    assert hits, "雑音のある実データ相当の状況で差額が検知されていない"
    assert abs(hits[0]["detail"]["amount"] - DIFF) < 1


def test_7_1_gradual_opening_paydown_not_flagged():
    """期首残高を数ヶ月かけて返済しているだけの場合は指摘しない。

    記帳の誤りは「ある月に一度だけ生じる段差」として現れるのに対し、
    返済は毎月少しずつ動くという違いで判別する。
    """
    rows = []
    for m in range(1, 7):
        rows.append(je(f"2026-{m:02d}-28", "外注費", "未払金", 100000, csub="V社"))
    # 期首残高 900,000円 を毎月 150,000円ずつ返済（正常）
    for m in range(1, 7):
        rows.append(je(f"2026-{m:02d}-10", "未払金", "普通預金", 250000, dsub="V社"))
    assert not _recon_ids(rows)


def test_7_1_small_rounding_difference_not_flagged():
    """消費税端数レベルの差額（しきい値未満）は指摘しない"""
    rows = _settlement_cycle(diff_month=None)
    for i, r in enumerate(rows):
        if r["debit_account"] == "未払費用" and r["date"].startswith("2026-04"):
            rows[i] = dict(r, debit_amount=r["debit_amount"] + 50,
                           credit_amount=r["credit_amount"] + 50)
    assert not _recon_ids(rows)


# ══════════════════════════════════════════════════════════
# 7-2: 科目取り違え ― 相手科目のバリエーション
# ══════════════════════════════════════════════════════════
CARD = [118400, 131200, 109700, 124300, 127600, 115900]


def _misposting(account="未払金", sub="アメックスカード", month=5,
                desc="手土産代 大木製薬訪問", slip="810"):
    rows = _settlement_cycle()
    for m in range(1, 7):
        rows.append(je(f"2026-{m:02d}-27", "消耗品費", account, CARD[m - 1], csub=sub))
    for m in range(2, 7):
        rows.append(je(f"2026-{m:02d}-10", account, "普通預金", CARD[m - 2], dsub=sub))
    if month:
        rows.append(je(f"2026-{month:02d}-20", "交際費", account, DIFF,
                       csub=sub, desc=desc, slip=slip))
    return rows


@pytest.mark.parametrize("name,kwargs", [
    ("未払金（カード）",       {}),
    ("預り金",                 {"account": "預り金", "sub": "社員立替"}),
    ("立替金（資産側）",       {"account": "立替金", "sub": "社員"}),
    ("摘要も伝票番号も無い",   {"desc": "", "slip": ""}),
    ("補助科目なし",           {"sub": ""}),
    ("取り違えが別の月",       {"month": 2}),
])
def test_7_2_detects_misposting(name, kwargs):
    assert _recon_ids(_misposting(**kwargs), "7-2"), f"{name}: 科目取り違えが検知されていない"


def test_7_2_no_counterpart_not_flagged():
    """同額の仕訳が他科目に無ければ取り違えとは言えない"""
    assert not _recon_ids(_misposting(month=None), "7-2")


# ══════════════════════════════════════════════════════════
# 6-3: 定期費用の欠落 ― 摘要・金額のバリエーション
# ══════════════════════════════════════════════════════════
def _subscription(months=6, miss=5, amount=lambda m: 4277,
                  desc=lambda m: "日経電子版購読料", dsub="", other=True):
    rows = _filler(months)
    for m in range(1, months + 1):
        if m != miss:
            rows.append(je(f"2026-{m:02d}-25", "新聞図書費", "普通預金",
                           amount(m), desc=desc(m), dsub=dsub))
        if other:
            # 同科目に別の取引が毎月あるため、科目単位では欠落に気づけない
            rows.append(je(f"2026-{m:02d}-10", "新聞図書費", "現金",
                           1500 + m * 230, desc="書籍代"))
    return rows


@pytest.mark.parametrize("name,kwargs", [
    ("摘要が毎月同じ",             {}),
    ("摘要が空欄",                 {"desc": lambda m: ""}),
    ("摘要に月が入る",             {"desc": lambda m: f"日経電子版 {m}月分"}),
    ("摘要に取引先名が混在",       {"desc": lambda m: "日経電子版" if m % 2
                                    else "（株）日本経済新聞社 日経電子版"}),
    ("金額が端数で1円ブレる",      {"amount": lambda m: 4277 if m % 2 else 4278}),
    ("摘要空欄かつ金額がブレる",   {"desc": lambda m: "",
                                    "amount": lambda m: 4277 if m % 2 else 4278}),
    ("補助科目あり",               {"dsub": "日経"}),
    ("12ヶ月・期央が欠落",         {"months": 12, "miss": 9}),
    ("欠落が最終月",               {"miss": 6}),
    ("欠落が初月",                 {"miss": 1}),
    ("同科目に他取引が無い",       {"other": False}),
])
def test_recurring_expense_gap_detected(name, kwargs):
    """補助科目の有無や摘要の書き方によらず、いずれかのチェックが欠落を捉える"""
    assert _trend_ids(_subscription(**kwargs)), f"{name}: 欠落が検知されていない"


@pytest.mark.parametrize("name,rows_fn", [
    ("金額が毎月大きく変動", lambda: _filler() + [
        je(f"2026-{m:02d}-15", "消耗品費", "現金", 1000 * m * 3, desc="事務用品")
        for m in range(1, 7) if m != 4]),
    ("同額が月内に多発", lambda: _filler() + [
        je(f"2026-{m:02d}-{10 + k:02d}", "旅費交通費", "現金", 1200, desc="交通費")
        for m in range(1, 7) for k in range(0 if m == 4 else 5)]),
])
def test_6_3_no_false_positive(name, rows_fn):
    from checkers.trend_checker import check_trend
    issues = check_trend(make_journal(rows_fn()), 1)
    assert not [i for i in issues if i["check_id"] == "6-3"], f"{name}: 誤検知"


# ══════════════════════════════════════════════════════════
# 6-4: 定期取引の途絶
# ══════════════════════════════════════════════════════════
def _lease(months=6, until=3, dsub="自動車ヴィッツ", other=True):
    rows = _filler(months)
    for m in range(1, months + 1):
        if m <= until:
            rows.append(je(f"2026-{m:02d}-25", "リース料", "普通預金", 38000, dsub=dsub))
        if other:
            rows.append(je(f"2026-{m:02d}-25", "リース料", "普通預金", 12000, dsub="複合機"))
    return rows


@pytest.mark.parametrize("name,kwargs", [
    ("期央で途絶（補助科目あり）", {}),
    ("補助科目なし",               {"dsub": ""}),
    ("直近2ヶ月が途絶",            {"until": 4}),
    ("12ヶ月・期央で途絶",         {"months": 12, "until": 6}),
    ("同科目に他契約が無い",       {"other": False}),
])
def test_6_4_detects_discontinued(name, kwargs):
    assert _trend_ids(_lease(**kwargs)), f"{name}: 途絶が検知されていない"


def test_one_month_gap_covered_by_1_4():
    """直近1ヶ月だけの欠落は 1-4（既存チェック）が担当する"""
    from checkers.completeness_checker import check_completeness
    df = make_journal(_lease(until=5))
    hits = [i for i in check_completeness(df, 1) if "ヴィッツ" in i["message"]]
    assert hits and hits[0]["check_id"] == "1-4"


@pytest.mark.parametrize("name,kwargs", [
    ("期末まで継続", {"until": 6}),
    ("最初から無い", {"until": 0}),
])
def test_6_4_no_false_positive(name, kwargs):
    from checkers.trend_checker import check_trend
    issues = check_trend(make_journal(_lease(**kwargs)), 1)
    assert not [i for i in issues if i["check_id"] == "6-4"], f"{name}: 誤検知"
