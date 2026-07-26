"""
全チェッカー回帰テスト（ゴールデンスナップショット方式）。

シード固定の合成仕訳データに対して全チェッカーを実行し、
カテゴリ別の指摘件数が「期待値（ゴールデン）」と一致することを確認する。

チェックロジックを意図的に変更して件数が変わった場合は、
変更内容を確認したうえで下の GOLDEN_COUNTS を更新すること。
（意図しない変更でこのテストが落ちたら、それは退行＝バグの兆候）
"""
import random

import pandas as pd

from parsers.csv_parser import parse_csv


def _gen_content(n=4000, seed=42):
    """パーサーテストより大きめの決定的データを生成（弥生raw形式）"""
    rng = random.Random(seed)
    accounts_d = ["現金", "普通預金", "売掛金", "仮払消費税", "旅費交通費", "消耗品費",
                  "材料仕入高", "買掛金", "未払金", "外注費", "支払手数料", "地代家賃", "給料手当"]
    accounts_c = ["売上高", "普通預金", "買掛金", "現金", "未払金", "売掛金",
                  "仮受消費税", "預り金", "未払費用"]
    subs = ["", "A商事", "B工業", "C株式会社", "D建設", "", "", "楽天", "三井住友", "みずほ"]
    taxes = ["課対仕入10%", "対象外", "課税売上10%", "", "非課税", "課対仕入8%(軽)"]
    descs = ["12月分", "商品売上", "材料仕入", "振込手数料", "家賃", "旅費精算", "外注費支払", "消耗品購入"]
    lines = []
    for i in range(n):
        mm, dd = rng.randint(1, 6), rng.randint(1, 28)
        da, ca = rng.choice(accounts_d), rng.choice(accounts_c)
        ds, cs = rng.choice(subs), rng.choice(subs)
        amt = rng.choice([1000, 2500, 10800, 55000, 128000, 3300, 9800, 220000])
        tax = int(amt * 10 / 110)
        dtax, ctax = rng.choice(taxes), rng.choice(taxes)
        desc = rng.choice(descs)
        lines.append(
            f'"2110",{2110 + i % 4},"","R.08/{mm:02d}/{dd:02d}","{da}","{ds}","",'
            f'"{dtax}",{amt},{tax},"{ca}","{cs}","","{ctax}",{amt},{tax},"{desc}"'
        )
    return "\n".join(lines)


def _run_all_checks(df):
    from checkers.bs_checker import check_bs
    from checkers.pl_checker import check_pl
    from checkers.tax_checker import check_tax
    from checkers.completeness_checker import check_completeness
    from checkers.tax_detail_checker import check_tax_detail
    from checkers.asset_checker import check_assets
    from checkers.ar_ap_checker import check_ar_ap
    from checkers.governance_checker import check_governance
    from checkers.trend_checker import check_trend

    issues = []
    issues += check_bs(df, {})
    issues += check_pl(df)
    issues += check_tax(df)
    issues += check_completeness(df, 1)
    issues += check_tax_detail(df)
    issues += check_assets(df)
    issues += check_ar_ap(df)
    issues += check_governance(df)
    issues += check_trend(df, 1)
    return issues


# ── ゴールデン（期待値）─────────────────────────────────
# 4000行・seed=42 の合成データに対するカテゴリ別指摘件数。
# チェックロジックを意図的に変えたときのみ、確認のうえ更新する。
GOLDEN_COUNTS = {
    "1-2 期間帰属": 2,
    "1-3 部門未設定": 1,
    "2-9 振込手数料返還": 1,
    "5-2 重複仕訳": 33,
    "6-2 定期取引の欠落": 3,
    "BS": 1,
    "PL": 2,
    "消費税": 10,
}


def test_full_pipeline_golden_counts():
    content = _gen_content()
    df, software = parse_csv(content)
    assert software == "yayoi_raw"
    assert len(df) == 4000

    issues = _run_all_checks(df)

    from collections import Counter
    counts = dict(Counter(i["category"] for i in issues))
    assert counts == GOLDEN_COUNTS, (
        f"指摘件数がゴールデンと不一致。\n実測: {counts}\n期待: {GOLDEN_COUNTS}\n"
        "意図的なロジック変更の場合は GOLDEN_COUNTS を更新してください。"
    )


def test_all_issues_have_required_keys():
    """全指摘が必須キー（level/category/account/month/message）を持つ"""
    content = _gen_content(n=1000)
    df, _ = parse_csv(content)
    for issue in _run_all_checks(df):
        for key in ("level", "category", "account", "month", "message"):
            assert key in issue, f"必須キー {key} 欠落: {issue}"
        assert issue["level"] in ("error", "warning", "info")
