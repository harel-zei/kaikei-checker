"""
意図に照らした精査（review_by_intent）のテスト。

ルールは「条件」でしかないため、条件に合致しても実態として問題ないもの、
条件から外れるが意図に照らせば問題のものが生じる。その両方を扱えることと、
AIが誤った応答を返しても安全側に倒れることを固定する。

APIは呼ばず、Claudeの応答をモックして検証する。
"""
import json
from types import SimpleNamespace

import pytest

from conftest import make_journal, entry as je


@pytest.fixture
def ai(monkeypatch):
    import ai_reviewer
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    return ai_reviewer


def _mock_response(ai, monkeypatch, payload: dict, capture: dict = None):
    """Claudeの応答をモックする。capture を渡すと送信内容を記録する。"""
    def fake(catalog, issues_payload, matrix, feedback_hint):
        if capture is not None:
            capture["catalog"] = catalog
            capture["issues"] = issues_payload
            capture["matrix"] = matrix
        return payload
    monkeypatch.setattr(ai, "_ask_intent_review", fake)


def _df():
    rows = []
    for m in range(1, 7):
        rows += [
            je(f"2026-{m:02d}-25", "地代家賃", "普通預金", 200000),
            je(f"2026-{m:02d}-05", "水道光熱費", "普通預金", 30000 + m * 100),
        ]
    return make_journal(rows)


def _issue(level="warning", check_id="6-1", account="通信費", msg="テスト指摘"):
    return {"level": level, "check_id": check_id, "category": f"{check_id} テスト",
            "account": account, "month": "2026-05", "message": msg}


class TestIntentCatalog:
    """意図カタログが、AIが判断できる内容になっていること"""

    def test_catalog_covers_main_check_groups(self):
        import check_intents
        text = check_intents.catalog_text()
        for cid in ("6-1", "7-1", "2-", "5-1", "3-", "8-"):
            assert cid in text, f"意図カタログにチェック {cid} が含まれていない"

    def test_catalog_states_intent_not_just_condition(self):
        """「守りたいこと」「ルールでは見えないこと」が明記されていること"""
        import check_intents
        text = check_intents.catalog_text()
        assert "守りたいこと" in text
        assert "ルールでは見えないこと" in text
        assert "意図に照らして追加で見るべきこと" in text

    def test_stance_discourages_speculation(self):
        """推測での指摘を戒める姿勢が含まれていること（ノイズ防止）"""
        import check_intents
        assert "推測" in check_intents.GENERAL_STANCE
        assert "数字" in check_intents.GENERAL_STANCE


class TestReconsider:
    """A: 意図に照らして「確認不要」と判断されたときの扱い"""

    def test_demoted_not_deleted(self, ai, monkeypatch):
        """AIが不要と判断しても指摘は消さず、根拠を添えて参考扱いにする"""
        _mock_response(ai, monkeypatch, {
            "review": [{"index": 0, "keep": False,
                        "reason": "同月に別科目で同額が計上されており振替と判断"}],
            "additional": [],
        })
        issues = [_issue()]
        kept, extra = ai.review_by_intent(issues, _df(), "A社")
        assert len(kept) == 1, "指摘が消えてしまっている（消さずに降格する方針）"
        assert kept[0]["level"] == "info"
        assert "要確認度: 低" in kept[0]["category"]
        assert "振替と判断" in kept[0]["message"]

    def test_reason_required_to_demote(self, ai, monkeypatch):
        """根拠が無い keep=false は降格しない（言いっぱなしを許さない）"""
        _mock_response(ai, monkeypatch, {
            "review": [{"index": 0, "keep": False, "reason": ""}], "additional": [],
        })
        kept, _ = ai.review_by_intent([_issue()], _df(), "A社")
        assert kept[0]["level"] == "warning"

    def test_error_level_never_sent_for_review(self, ai, monkeypatch):
        """要修正（error）はAIの再評価対象にしない"""
        cap = {}
        _mock_response(ai, monkeypatch, {"review": [], "additional": []}, cap)
        issues = [_issue(level="error", check_id="3-2"), _issue(level="warning")]
        kept, _ = ai.review_by_intent(issues, _df(), "A社")
        sent_ids = [i["check_id"] for i in cap["issues"]]
        assert "3-2" not in sent_ids, "errorの指摘がAIに送られている"
        assert all(i["level"] == "error" for i in kept if i["check_id"] == "3-2")

    def test_keep_true_adds_supplement(self, ai, monkeypatch):
        _mock_response(ai, monkeypatch, {
            "review": [{"index": 0, "keep": True, "reason": "金額が大きく確認価値あり"}],
            "additional": [],
        })
        kept, _ = ai.review_by_intent([_issue()], _df(), "A社")
        assert kept[0]["level"] == "warning"
        assert "AI補足" in kept[0]["message"]


class TestAdditionalFindings:
    """B: 意図に照らしてルールの条件外の観点を拾えること"""

    def test_additional_issue_is_created(self, ai, monkeypatch):
        _mock_response(ai, monkeypatch, {
            "review": [],
            "additional": [{
                "account": "支払手数料", "month": "2026-04",
                "message": "毎月同額の計上が4月だけ2倍になっています",
                "basis": "1-3月 8,000円 / 4月 16,000円",
            }],
        })
        _, extra = ai.review_by_intent([_issue()], _df(), "A社")
        assert len(extra) == 1
        assert extra[0]["check_id"] == "AI-2"
        assert extra[0]["level"] == "info", "AIの追加指摘は情報レベルにとどめる"
        assert "8,000円" in extra[0]["message"], "根拠が本文に含まれていない"
        assert "確定的な指摘ではありません" in extra[0]["message"]

    def test_empty_message_skipped(self, ai, monkeypatch):
        _mock_response(ai, monkeypatch, {
            "review": [], "additional": [{"account": "X", "month": "", "message": "  "}],
        })
        _, extra = ai.review_by_intent([_issue()], _df(), "A社")
        assert extra == []

    def test_additional_capped(self, ai, monkeypatch):
        many = [{"account": f"科目{i}", "month": "2026-01",
                 "message": f"指摘{i}", "basis": "x"} for i in range(30)]
        _mock_response(ai, monkeypatch, {"review": [], "additional": many})
        _, extra = ai.review_by_intent([_issue()], _df(), "A社")
        assert len(extra) <= ai._INTENT_MAX_EXTRA


class TestSafety:
    """AIが壊れた応答を返しても、チェックを壊さないこと"""

    def test_disabled_without_api_key(self, monkeypatch):
        import ai_reviewer
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        issues = [_issue()]
        assert ai_reviewer.review_by_intent(issues, _df(), "A社") == (issues, [])

    def test_api_error_is_fail_open(self, ai, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("API障害")
        monkeypatch.setattr(ai, "_ask_intent_review", boom)
        issues = [_issue()]
        kept, extra = ai.review_by_intent(issues, _df(), "A社")
        assert kept == issues and extra == []

    def test_malformed_index_ignored(self, ai, monkeypatch):
        """存在しない index や不正な型を返されても落ちない"""
        _mock_response(ai, monkeypatch, {
            "review": [{"index": 999, "keep": False, "reason": "x"},
                       {"index": "abc", "keep": False, "reason": "y"},
                       {"keep": False, "reason": "z"}],
            "additional": [],
        })
        kept, extra = ai.review_by_intent([_issue()], _df(), "A社")
        assert len(kept) == 1 and kept[0]["level"] == "warning"

    def test_missing_keys_in_response(self, ai, monkeypatch):
        _mock_response(ai, monkeypatch, {})
        issues = [_issue()]
        kept, extra = ai.review_by_intent(issues, _df(), "A社")
        assert kept == issues and extra == []

    def test_empty_dataframe(self, ai, monkeypatch):
        _mock_response(ai, monkeypatch, {"review": [], "additional": []})
        import pandas as pd
        kept, extra = ai.review_by_intent([_issue()], pd.DataFrame(), "A社")
        assert len(kept) == 1


class TestPrivacy:
    """AIに送る情報が必要最小限であること"""

    def test_no_descriptions_or_counterparties_sent(self, ai, monkeypatch):
        cap = {}
        _mock_response(ai, monkeypatch, {"review": [], "additional": []}, cap)
        rows = [je(f"2026-{m:02d}-25", "外注費", "未払金", 100000,
                   dsub="秘密取引先", desc="秘密の摘要") for m in range(1, 7)]
        ai.review_by_intent([_issue()], make_journal(rows), "A社")
        body = json.dumps(cap["matrix"], ensure_ascii=False)
        assert "秘密の摘要" not in body
        assert "秘密取引先" not in body
        assert "外注費" in body, "科目名は判断に必要なので送られるべき"

    def test_intent_catalog_is_sent(self, ai, monkeypatch):
        """判断の土台となる意図カタログが渡されていること"""
        cap = {}
        _mock_response(ai, monkeypatch, {"review": [], "additional": []}, cap)
        ai.review_by_intent([_issue()], _df(), "A社")
        assert "守りたいこと" in cap["catalog"]
