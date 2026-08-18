"""
フィードバック学習（使うほど精度が上がる仕組み）のテスト。

担当者の判断を蓄積し、次回以降のチェックに反映する流れを固定する。
特に「安全側の設計」（error は抑制しない・APIキー無しでも壊れない）を守る。
"""
import json

import pytest


@pytest.fixture
def store(tmp_path, monkeypatch):
    """保存先を一時ディレクトリに隔離した feedback_store を返す"""
    import feedback_store as fs
    monkeypatch.setattr(fs, "DATA_DIR", tmp_path / "_feedback")
    monkeypatch.setattr(fs, "_USE_SB", False)
    return fs


def _issue(check_id="6-3", account="旅費交通費", level="warning", msg="テスト指摘"):
    return {"check_id": check_id, "category": f"{check_id} テスト", "account": account,
            "level": level, "message": msg}


class TestFeedbackStore:
    def test_verdict_recorded_for_client_and_global(self, store):
        """判断は顧問先別と全体の両方に記録される"""
        store.record_verdict("A社", _issue(), store.VERDICT_NOISE)
        assert store.stats("A社")["noise"] == 1
        assert store.stats(store.GLOBAL_KEY)["noise"] == 1

    def test_invalid_verdict_rejected(self, store):
        with pytest.raises(ValueError):
            store.record_verdict("A社", _issue(), "maybe")

    def test_missed_requires_description(self, store):
        with pytest.raises(ValueError):
            store.record_missed("A社", "新聞図書費", "   ")

    def test_noise_pattern_needs_repetition(self, store):
        """1回の「不要」だけでは抑制対象にしない（誤操作で消えないように）"""
        store.record_verdict("A社", _issue(), store.VERDICT_NOISE)
        assert store.noise_patterns("A社") == []
        store.record_verdict("A社", _issue(), store.VERDICT_NOISE)
        assert len(store.noise_patterns("A社")) == 1

    def test_mixed_verdicts_not_suppressed(self, store):
        """「妥当」も混じるパターンは抑制しない"""
        for _ in range(2):
            store.record_verdict("A社", _issue(), store.VERDICT_NOISE)
        for _ in range(2):
            store.record_verdict("A社", _issue(), store.VERDICT_USEFUL)
        assert store.noise_patterns("A社") == []

    def test_no_client_identifiers_stored(self, store):
        """保存内容に顧問先名が含まれない（記録はファイル単位で分離）"""
        store.record_verdict("秘密株式会社", _issue(), store.VERDICT_NOISE)
        path = store.DATA_DIR / "秘密株式会社.json"
        body = json.loads(path.read_text(encoding="utf-8"))
        assert "秘密株式会社" not in json.dumps(body, ensure_ascii=False)


class TestLearnedSuppression:
    def _train_noise(self, store, times=2, **kw):
        for _ in range(times):
            store.record_verdict("A社", _issue(**kw), store.VERDICT_NOISE)

    def test_suppresses_after_learning(self, store):
        import ai_reviewer as ai
        issues = [_issue(), _issue(check_id="7-1", account="未払費用")]
        kept, dropped = ai.apply_learned_suppression(issues, "A社")
        assert (len(kept), dropped) == (2, 0)

        self._train_noise(store)
        kept, dropped = ai.apply_learned_suppression(issues, "A社")
        assert (len(kept), dropped) == (1, 1)
        assert kept[0]["account"] == "未払費用"

    def test_error_level_never_suppressed(self, store):
        """要修正（error）は学習しても抑制しない ― 見逃しは許容しない"""
        import ai_reviewer as ai
        self._train_noise(store, account="工具器具備品", check_id="3-2", level="error")
        kept, dropped = ai.apply_learned_suppression(
            [_issue(check_id="3-2", account="工具器具備品", level="error")], "A社")
        assert (len(kept), dropped) == (1, 0)

    def test_other_client_unaffected(self, store):
        """ある顧問先の学習が他の顧問先に波及しない"""
        import ai_reviewer as ai
        self._train_noise(store)
        kept, dropped = ai.apply_learned_suppression([_issue()], "B社")
        assert (len(kept), dropped) == (1, 0)


class TestFailOpen:
    """APIキーが無い・学習が空でも、チェックを壊さないこと"""

    def test_review_issues_passthrough_without_key(self, store, monkeypatch):
        import ai_reviewer as ai
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        issues = [_issue()]
        assert ai.review_issues(issues, "A社") == issues

    def test_find_missed_returns_empty_without_training(self, store, monkeypatch):
        """見逃し登録が無いうちはAI探索を動かさない（無駄な呼び出しをしない）"""
        import ai_reviewer as ai
        monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
        assert ai.find_missed_issues(None, "A社") == []

    def test_suppression_survives_missing_store(self, tmp_path, monkeypatch):
        """保存先が読めなくても例外にせず素通しする"""
        import feedback_store as fs, ai_reviewer as ai
        monkeypatch.setattr(fs, "DATA_DIR", tmp_path / "nonexistent")
        monkeypatch.setattr(fs, "_USE_SB", False)
        issues = [_issue()]
        assert ai.apply_learned_suppression(issues, "A社") == (issues, 0)


class TestMonthlyMatrix:
    def test_matrix_has_no_descriptions(self):
        """AIに渡すのは科目×月の金額のみ（摘要・取引先は送らない）"""
        import ai_reviewer as ai
        from conftest import make_journal, entry as je
        df = make_journal([
            je("2026-01-25", "新聞図書費", "普通預金", 4277, desc="日経電子版 秘密の取引先"),
            je("2026-02-25", "新聞図書費", "普通預金", 4277, desc="日経電子版"),
        ])
        matrix = ai._monthly_matrix(df)
        body = json.dumps(matrix, ensure_ascii=False)
        assert "新聞図書費" in body
        assert "秘密の取引先" not in body
        assert "日経電子版" not in body
