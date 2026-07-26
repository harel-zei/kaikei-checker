"""
弥生会計rawパーサー（一括読み込み版）のテスト。
- 旧実装（行単位パース）との等価性
- 複合仕訳・埋め込みカンマ・和暦/西暦・短い行などのエッジケース
"""
import random

import pandas as pd
import pytest

from parsers.csv_parser import (
    detect_software,
    parse_csv,
    parse_yayoi_raw,
    _parse_yayoi_raw_linewise,
)


def _gen_yayoi_raw(n=300, seed=42):
    """テスト用の弥生raw形式データを生成する（シード固定で決定的）"""
    rng = random.Random(seed)
    accounts_d = ["現金", "普通預金", "売掛金", "旅費交通費", "消耗品費", "材料仕入高", "買掛金"]
    accounts_c = ["売上高", "普通預金", "買掛金", "現金", "未払金", "売掛金"]
    subs = ["", "A商事", "B工業", "C株式会社"]
    taxes = ["課対仕入10%", "対象外", "課税売上10%", ""]
    descs = ["12月分", "商品売上", "材料仕入", "振込手数料", "家賃"]
    lines = []
    for i in range(n):
        mm, dd = rng.randint(1, 6), rng.randint(1, 28)
        da, ca = rng.choice(accounts_d), rng.choice(accounts_c)
        ds, cs = rng.choice(subs), rng.choice(subs)
        amt = rng.choice([1000, 2500, 10800, 55000, 128000])
        dtax, ctax = rng.choice(taxes), rng.choice(taxes)
        desc = rng.choice(descs)
        lines.append(
            f'"2110",{2110 + i % 4},"","R.08/{mm:02d}/{dd:02d}","{da}","{ds}","",'
            f'"{dtax}",{amt},{int(amt * 10 / 110)},"{ca}","{cs}","","{ctax}",'
            f'{amt},{int(amt * 10 / 110)},"{desc}"'
        )
    return "\n".join(lines)


def _assert_frames_equivalent(new: pd.DataFrame, old: pd.DataFrame):
    """新旧パーサーの出力が実質同一であることを確認する。
    （旧実装は空欄を文字列 'nan' にする既知の癖があるため '' と同一視する）"""
    assert len(new) == len(old)
    assert list(new.columns) == list(old.columns)
    for col in old.columns:
        a, b = new[col], old[col]
        if col == "date":
            assert a.fillna(pd.Timestamp("1900")).eq(b.fillna(pd.Timestamp("1900"))).all()
        elif a.dtype.kind in "fi":
            assert (a - b).abs().max() < 1e-6
        else:
            aa = a.astype(str).replace("nan", "")
            bb = b.astype(str).replace("nan", "")
            assert (aa == bb).all(), f"列 {col} が不一致"


def test_bulk_parser_matches_linewise():
    content = _gen_yayoi_raw()
    _assert_frames_equivalent(
        parse_yayoi_raw(content), _parse_yayoi_raw_linewise(content)
    )


def test_detects_yayoi_raw():
    content = _gen_yayoi_raw(n=5)
    assert detect_software(content) == "yayoi_raw"
    df, software = parse_csv(content)
    assert software == "yayoi_raw"
    assert len(df) == 5


def test_reiwa_date_conversion():
    line = '"2110",1,"","R.08/05/15","現金","","","対象外",1000,0,"売上高","","","課税売上10%",1000,90,"x"'
    df = parse_yayoi_raw(line)
    assert df["date"].iloc[0] == pd.Timestamp("2026-05-15")  # R.08 = 2026年


def test_western_date_fallback():
    line = '"2110",1,"","2026/05/01","現金","","","対象外",1000,0,"売上高","","","課税売上10%",1000,90,"x"'
    df = parse_yayoi_raw(line)
    assert df["date"].iloc[0] == pd.Timestamp("2026-05-01")


def test_embedded_comma_in_description():
    line = ('"2110",1,"","R.08/05/01","現金","","","対象外",1000,0,'
            '"売上高","","","課税売上10%",1000,90,"5月分, 商品A・B"')
    df = parse_yayoi_raw(line)
    assert df["description"].iloc[0] == "5月分, 商品A・B"


def test_short_lines_skipped():
    good = ('"2110",1,"","R.08/05/01","現金","","","対象外",1000,0,'
            '"売上高","","","課税売上10%",1000,90,"ok"')
    content = '"2110",1,"","R.08/05/01","現金"\n' + good
    df = parse_yayoi_raw(content)
    assert len(df) == 1
    assert df["description"].iloc[0] == "ok"


def test_compound_entry_blank_credit_kept():
    """複合仕訳（貸方が空欄の借方行）は列数さえ足りていれば保持される"""
    line = '"2110",5,"","R.08/05/01","材料仕入高","A商事","","課対仕入10%",50000,4545,"","","","",,'
    df = parse_yayoi_raw(line)
    assert len(df) == 1
    assert df["debit_amount"].iloc[0] == 50000
    assert df["credit_amount"].iloc[0] == 0.0


def test_empty_input():
    assert len(parse_yayoi_raw("")) == 0
    assert len(parse_yayoi_raw("\n\n  \n")) == 0
