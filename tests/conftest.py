"""
pytest 共通設定。
backend/ 配下のモジュール（checkers, parsers）を import できるようにする。
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))


# ── 仕訳DataFrameを簡潔に作るヘルパー ──────────────────────
BASE_ROW = {
    "date": None, "slip_no": "",
    "debit_account": "", "debit_sub": "", "debit_tax": "",
    "debit_amount": 0.0, "debit_tax_amt": 0.0,
    "credit_account": "", "credit_sub": "", "credit_tax": "",
    "credit_amount": 0.0, "credit_tax_amt": 0.0,
    "description": "",
}


def make_journal(rows):
    """部分的な辞書のリストから、パーサー出力と同じ列構成の仕訳DataFrameを作る"""
    recs = []
    for r in rows:
        rec = dict(BASE_ROW)
        rec.update(r)
        recs.append(rec)
    df = pd.DataFrame(recs)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df


def entry(date, debit, credit, amount, *, dsub="", csub="", dtax="", ctax="",
          desc="", slip=""):
    """1仕訳を作る簡易ヘルパー"""
    return {
        "date": date, "slip_no": slip,
        "debit_account": debit, "debit_sub": dsub, "debit_tax": dtax,
        "debit_amount": float(amount),
        "credit_account": credit, "credit_sub": csub, "credit_tax": ctax,
        "credit_amount": float(amount),
        "description": desc,
    }


@pytest.fixture
def journal():
    return make_journal


@pytest.fixture
def je():
    return entry
