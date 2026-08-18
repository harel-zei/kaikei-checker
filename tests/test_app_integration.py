"""
アプリ全体の統合テスト（HTTPレベル）。

これまでのテストは関数単位だけだったため、
「関数は正しいがアプリが起動しない」「APIが500を返す」といった
実際に使えなくなる不具合を検知できなかった。ここではその穴を埋める。

守る対象:
  - アプリが起動すること（import と ルート登録）
  - 主要APIが期待どおり応答すること
  - CSVアップロードからチェック結果までが通ること（E2E）
  - Excel/CSV出力が壊れていないこと
  - フィードバックAPIが動作し、学習が次の結果に反映されること
"""
import io
import json

import pytest

pytest.importorskip("fastapi", reason="fastapi 未導入の環境ではスキップ")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    """認証なしモードでアプリを起動し、保存先を一時ディレクトリへ隔離する"""
    import os
    os.environ.pop("APP_PASSWORD", None)      # 認証スキップ（ローカル開発モード）
    os.environ.pop("ANTHROPIC_API_KEY", None)  # AIは呼ばない
    os.environ["SUPABASE_URL"] = ""
    os.environ["SUPABASE_SECRET_KEY"] = ""

    import main
    import client_store
    import feedback_store

    tmp = tmp_path_factory.mktemp("appdata")
    client_store.DATA_DIR = tmp
    feedback_store.DATA_DIR = tmp / "_feedback"
    with TestClient(main.app) as c:
        yield c


def _journal_csv(rows=None):
    """弥生raw形式の最小の仕訳帳を作る（6ヶ月分・欠落を1件仕込む）"""
    lines = []
    for m in range(1, 7):
        # 毎月の売上と経費
        lines.append(
            f'"2110",{m},"","R.08/{m:02d}/25","売掛金","A商事","","対象外",500000,0,'
            f'"売上高","","","課税売上10%",500000,45454,"{m}月売上"'
        )
        lines.append(
            f'"2110",{m},"","R.08/{m:02d}/05","水道光熱費","","","課対仕入10%",30000,2727,'
            f'"普通預金","","","対象外",30000,0,"電気代"'
        )
        # 5月だけ欠落する定期費用（6-3 が拾う想定）
        if m != 5:
            lines.append(
                f'"2110",{m},"","R.08/{m:02d}/20","新聞図書費","","","課対仕入10%",4277,388,'
                f'"普通預金","","","対象外",4277,0,"日経電子版"'
            )
    return "\n".join(rows or lines)


class TestAppBoots:
    """アプリが起動し、画面と主要ルートが存在すること"""

    def test_index_page_served(self, client):
        res = client.get("/")
        assert res.status_code == 200
        assert "会計データ" in res.text

    def test_health_of_key_routes(self, client):
        import main
        paths = {getattr(r, "path", "") for r in main.app.routes}
        for p in ["/api/check-auto", "/api/export-xlsx", "/api/clients",
                  "/api/feedback/verdict", "/api/feedback/missed",
                  "/api/feedback/stats", "/api/freee/status"]:
            assert p in paths, f"ルート {p} が登録されていない"


class TestCheckEndToEnd:
    """CSVアップロード → チェック → 結果 までが通ること"""

    def test_check_auto_returns_results(self, client):
        res = client.post(
            "/api/check-auto",
            files=[("files", ("仕訳帳.txt", _journal_csv().encode("cp932"), "text/plain"))],
        )
        assert res.status_code == 200, res.text
        data = res.json()
        assert "summary" in data and "issues" in data
        assert data["summary"]["total_entries"] > 0
        assert data["summary"]["software"]  # ソフト判定が入っている
        # 全ての指摘が必須キーを持つ
        for i in data["issues"]:
            for key in ("level", "category", "account", "month", "message"):
                assert key in i
            assert i["level"] in ("error", "warning", "info")

    def test_check_rejects_empty_upload(self, client):
        res = client.post("/api/check-auto", files=[])
        assert res.status_code in (400, 422)

    def test_check_rejects_non_journal(self, client):
        """仕訳帳として解釈できないファイルは 400 で明確に返す（500にしない）"""
        res = client.post(
            "/api/check-auto",
            files=[("files", ("メモ.txt", "これは仕訳帳ではありません".encode("cp932"), "text/plain"))],
        )
        assert res.status_code == 400
        assert "detail" in res.json()


class TestExport:
    """出力機能が壊れていないこと（担当者が実際に使う成果物）"""

    ISSUES = [{
        "level": "warning", "category": "6-3 定期費用の欠落（摘要）", "check_id": "6-3",
        "account": "新聞図書費", "month": "2026-05", "message": "テスト指摘",
    }]

    def test_xlsx_export(self, client):
        res = client.post("/api/export-xlsx", json={
            "issues": self.ISSUES, "period": "2026年5月分", "client_name": "テスト商事",
        })
        assert res.status_code == 200
        assert res.content[:2] == b"PK"          # xlsx は zip 形式
        assert len(res.content) > 2000

    def test_csv_export(self, client):
        res = client.post("/api/export-csv", json={
            "issues": self.ISSUES, "period": "2026年5月分", "client_name": "テスト商事",
        })
        assert res.status_code == 200
        assert len(res.content) > 0

    def test_export_with_no_issues(self, client):
        """指摘ゼロでも落ちない"""
        res = client.post("/api/export-xlsx", json={
            "issues": [], "period": "", "client_name": "",
        })
        assert res.status_code == 200


class TestFeedbackApi:
    """学習フィードバックのAPIが動き、次のチェックに反映されること"""

    def _issue(self):
        return {"level": "warning", "check_id": "PL", "category": "PL",
                "account": "水道光熱費", "month": "全期間", "message": "テスト指摘"}

    def test_verdict_recorded_and_stats_updated(self, client):
        res = client.post("/api/feedback/verdict", json={
            "client_name": "統合テスト社", "issue": self._issue(), "verdict": "noise",
        })
        assert res.status_code == 200, res.text
        assert res.json()["stats"]["noise"] >= 1

    def test_invalid_verdict_is_400_not_500(self, client):
        res = client.post("/api/feedback/verdict", json={
            "client_name": "統合テスト社", "issue": self._issue(), "verdict": "?",
        })
        assert res.status_code == 400

    def test_missed_requires_description(self, client):
        res = client.post("/api/feedback/missed", json={
            "client_name": "統合テスト社", "account": "新聞図書費", "description": "  ",
        })
        assert res.status_code == 400

    def test_missed_recorded(self, client):
        res = client.post("/api/feedback/missed", json={
            "client_name": "統合テスト社", "account": "新聞図書費",
            "month": "2026-06", "description": "毎月ある日経電子版の計上がない",
        })
        assert res.status_code == 200
        assert res.json()["stats"]["missed"] >= 1

    def test_stats_endpoint(self, client):
        res = client.get("/api/feedback/stats", params={"client_name": "統合テスト社"})
        assert res.status_code == 200
        body = res.json()
        assert "client" in body and "all" in body

    def test_learning_affects_next_check(self, client):
        """「不要」を2回記録すると、次のチェック結果からそのパターンが消える"""
        target = {"level": "info", "check_id": "PL", "category": "PL",
                  "account": "支払手数料", "month": "全期間", "message": "テスト"}
        csv = _journal_csv()
        files = [("files", ("仕訳帳.txt", csv.encode("cp932"), "text/plain"))]

        def run():
            res = client.post("/api/check-auto", data={"client_name": "学習テスト社"},
                              files=[("files", ("仕訳帳.txt", csv.encode("cp932"), "text/plain"))])
            assert res.status_code == 200, res.text
            return res.json()

        before = run()
        # 実際に出ている warning/info の指摘を1つ選び、それを「不要」と2回記録する
        candidates = [i for i in before["issues"] if i["level"] != "error"]
        if not candidates:
            pytest.skip("抑制対象にできる指摘が出ていない")
        pick = candidates[0]
        for _ in range(2):
            r = client.post("/api/feedback/verdict", json={
                "client_name": "学習テスト社", "issue": pick, "verdict": "noise"})
            assert r.status_code == 200

        after = run()
        key = (pick.get("check_id") or pick.get("category"), pick["account"])
        remaining = [i for i in after["issues"]
                     if (i.get("check_id") or i.get("category"), i["account"]) == key]
        assert not remaining, "「不要」と学習した指摘が次回も出ている"
        assert any("学習済み抑制" in log for log in after["summary"]["classification_log"])


class TestClientApi:
    """クライアント管理APIが動くこと"""

    def test_list_clients(self, client):
        res = client.get("/api/clients")
        assert res.status_code == 200
        assert isinstance(res.json(), (list, dict))

    def test_settings_roundtrip(self, client):
        name = "設定テスト社"
        res = client.post(f"/api/clients/{name}/settings",
                          json={"exclude_accounts": ["仮払金"], "fiscal_cutoff_day": 20})
        assert res.status_code == 200
        res = client.get(f"/api/clients/{name}/settings")
        assert res.status_code == 200
        body = res.json()
        assert body.get("exclude_accounts") == ["仮払金"]
        assert body.get("fiscal_cutoff_day") == 20


class TestFreeeStatus:
    """freee未設定でもエラーにならず状態を返すこと"""

    def test_status_without_config(self, client):
        res = client.get("/api/freee/status")
        assert res.status_code == 200
        assert "configured" in res.json()
