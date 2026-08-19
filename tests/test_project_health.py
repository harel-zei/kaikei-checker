"""
プロジェクトの健全性チェック。

「関数は正しいのに動かない」類の事故を機械的に防ぐ。
過去に実際に起きた事故を再発させないことが目的:

  - requirements.txt に無いライブラリを import してしまい、
    CI や本番で ModuleNotFoundError になった
  - 画面のJavaScriptに構文エラーが入っても誰も気づかなかった
  - 指摘の必須キーが欠けた形式のまま画面に渡っていた
"""
import ast
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend" / "index.html"

# import 名 と pip パッケージ名が異なるもの
IMPORT_TO_PACKAGE = {
    "dotenv": "python-dotenv",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
}


def _external_imports() -> set:
    """backend 配下が import している外部ライブラリ名を集める"""
    std = set(sys.stdlib_module_names)
    local = {p.stem for p in BACKEND.rglob("*.py")} | {"checkers", "parsers", "tools"}
    found = set()
    for f in BACKEND.rglob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    found.add(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return found - std - local


def _requirements() -> set:
    names = set()
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        # "uvicorn[standard]==0.32.1" → "uvicorn"
        names.add(re.split(r"[=<>\[]", line)[0].strip().lower())
    return names


class TestDependencies:
    """import しているものが必ず requirements.txt に載っていること"""

    def test_all_imports_declared(self):
        missing = []
        reqs = _requirements()
        for imp in _external_imports():
            pkg = IMPORT_TO_PACKAGE.get(imp, imp).lower()
            if pkg not in reqs:
                missing.append(f"{imp}（requirements.txt に {pkg} が無い）")
        assert not missing, (
            "requirements.txt に記載の無いライブラリを import しています:\n  "
            + "\n  ".join(missing)
            + "\nこのまま本番・CIへ出すと ModuleNotFoundError になります。"
        )

    # 最小構成のCIジョブが入れておく必要があるもの
    # （backend の共通モジュールが読み込むため、欠けるとテストが総崩れになる）
    MINIMAL_PACKAGES = ("pandas", "numpy", "pytest", "requests", "python-dotenv")

    def test_ci_has_minimal_dependency_job(self):
        """依存の宣言漏れを検知するための「最小構成ジョブ」がCIにあること"""
        wf = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
        install_lines = [l for l in wf.splitlines() if "pip install" in l]
        assert install_lines, "CIに pip install が見当たりません"
        ok = [l for l in install_lines
              if all(pkg in l for pkg in self.MINIMAL_PACKAGES)]
        assert ok, (
            "最小構成でテストを回すジョブがありません。\n"
            f"次を全て含む pip install 行が必要です: {', '.join(self.MINIMAL_PACKAGES)}\n"
            "（このジョブが無いと、requirements.txt への記載漏れを検知できません）")

    def test_ci_runs_full_dependency_job(self):
        """本番と同じ依存（requirements.txt）で回すジョブもあること"""
        wf = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
        assert "requirements.txt" in wf, (
            "本番同等の依存でテストするジョブがありません。"
            "これが無いと、アプリ起動やAPIの不具合を検知できません。")


class TestBackendSyntax:
    """backend の全 .py が構文エラー無しで読めること"""

    def test_all_python_files_parse(self):
        broken = []
        for f in BACKEND.rglob("*.py"):
            try:
                ast.parse(f.read_text(encoding="utf-8-sig"))
            except SyntaxError as e:
                broken.append(f"{f.relative_to(ROOT)}:{e.lineno} {e.msg}")
        assert not broken, "構文エラー:\n  " + "\n  ".join(broken)


class TestFrontend:
    """画面のJavaScriptが壊れていないこと（構文エラーは画面全体を止める）"""

    def _script_body(self) -> str:
        html = FRONTEND.read_text(encoding="utf-8")
        blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
        assert blocks, "index.html にインラインscriptが見つかりません"
        return "\n".join(blocks)

    @pytest.mark.skipif(shutil.which("node") is None, reason="node が無い環境ではスキップ")
    def test_inline_javascript_parses(self, tmp_path):
        js = tmp_path / "app.js"
        js.write_text(self._script_body(), encoding="utf-8")
        proc = subprocess.run([shutil.which("node"), "--check", str(js)],
                              capture_output=True, text=True)
        assert proc.returncode == 0, f"画面のJavaScriptに構文エラー:\n{proc.stderr}"

    def test_functions_called_from_html_are_defined(self):
        """onclick等から呼ばれる関数が定義されていること（未定義だとボタンが無反応）"""
        html = FRONTEND.read_text(encoding="utf-8")
        called = set(re.findall(r'on(?:click|change|input)="(\w+)\(', html))
        body = self._script_body()
        defined = set(re.findall(r"(?:function\s+(\w+)|(?:async\s+)?function\s+(\w+))", body))
        defined = {n for pair in defined for n in pair if n}
        defined |= set(re.findall(r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(", body))
        missing = sorted(called - defined)
        assert not missing, f"HTMLから呼ばれているが未定義の関数: {missing}"


REQUIRED_ISSUE_KEYS = ("level", "category", "account", "month", "message")
VALID_LEVELS = ("error", "warning", "info")


class TestIssueFormatStatic:
    """指摘の形式をソースコード上で検査する。

    実行時のテストは「そのチェックが発火するデータ」でしか検証できないため、
    条件が揃わない指摘の形式崩れを見逃す。ここでは issues.append({...}) の
    辞書リテラルを全て静的に検査し、発火条件によらず形式を保証する。
    """

    def _issue_dicts(self):
        """checkers 配下の issues.append({...}) の辞書リテラルを集める"""
        found = []
        for f in (BACKEND / "checkers").glob("*.py"):
            tree = ast.parse(f.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "append"
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id.startswith("issues")
                        and node.args
                        and isinstance(node.args[0], ast.Dict)):
                    continue
                d = node.args[0]
                keys = [k.value for k in d.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)]
                found.append((f.name, node.lineno, keys, d))
        return found

    def test_at_least_one_issue_dict_found(self):
        """検査対象を取りこぼしていないこと（テスト自体の空振り防止）"""
        assert len(self._issue_dicts()) >= 20

    def test_all_issue_dicts_have_required_keys(self):
        problems = []
        for fname, lineno, keys, _ in self._issue_dicts():
            for req in REQUIRED_ISSUE_KEYS:
                if req not in keys:
                    problems.append(f"{fname}:{lineno} に必須キー '{req}' が無い（keys={keys}）")
        assert not problems, (
            "指摘の形式が崩れています（画面・Excel出力が壊れます）:\n  "
            + "\n  ".join(problems))

    def test_all_levels_are_valid(self):
        problems = []
        for fname, lineno, keys, d in self._issue_dicts():
            for k, v in zip(d.keys, d.values):
                if (isinstance(k, ast.Constant) and k.value == "level"
                        and isinstance(v, ast.Constant)
                        and v.value not in VALID_LEVELS):
                    problems.append(f"{fname}:{lineno} level='{v.value}' は不正")
        assert not problems, "\n  ".join(problems)


class TestIssueFormat:
    """全チェッカーが返す指摘が、画面・Excel出力が期待する形式であること"""

    def test_every_checker_returns_valid_issues(self):
        sys.path.insert(0, str(BACKEND))
        from conftest import make_journal, entry as je  # noqa: E402

        rows = []
        for m in range(1, 7):
            rows += [
                je(f"2026-{m:02d}-25", "売掛金", "売上高", 500000, dsub="A商事",
                   ctax="課税売上10%", desc=f"{m}月売上"),
                je(f"2026-{m:02d}-05", "水道光熱費", "普通預金", 30000,
                   dtax="課対仕入10%", desc="電気代"),
                je(f"2026-{m:02d}-10", "工具器具備品", "未払金", 80000, desc="備品"),
            ]
        df = make_journal(rows)

        from checkers.bs_checker import check_bs
        from checkers.pl_checker import check_pl
        from checkers.tax_checker import check_tax
        from checkers.completeness_checker import check_completeness
        from checkers.tax_detail_checker import check_tax_detail
        from checkers.asset_checker import check_assets
        from checkers.ar_ap_checker import check_ar_ap
        from checkers.governance_checker import check_governance
        from checkers.trend_checker import check_trend
        from checkers.reconciliation_checker import check_reconciliation
        from checkers.consistency_checker import check_consistency

        checkers = {
            "bs": lambda: check_bs(df, {}), "pl": lambda: check_pl(df),
            "tax": lambda: check_tax(df), "completeness": lambda: check_completeness(df, 1),
            "tax_detail": lambda: check_tax_detail(df), "assets": lambda: check_assets(df),
            "ar_ap": lambda: check_ar_ap(df), "governance": lambda: check_governance(df),
            "trend": lambda: check_trend(df, 1),
            "reconciliation": lambda: check_reconciliation(df),
            "consistency": lambda: check_consistency(df, {}),
        }
        problems = []
        for name, fn in checkers.items():
            try:
                issues = fn()
            except Exception as e:
                problems.append(f"{name}: 例外 {type(e).__name__}: {e}")
                continue
            for i in issues:
                for key in ("level", "category", "account", "month", "message"):
                    if key not in i:
                        problems.append(f"{name}: 必須キー {key} が無い → {i}")
                if i.get("level") not in ("error", "warning", "info"):
                    problems.append(f"{name}: level が不正 → {i.get('level')}")
                if i.get("detail") is not None:
                    try:
                        json.dumps(i["detail"])
                    except TypeError:
                        problems.append(f"{name}: detail がJSON化できない")
        assert not problems, "指摘の形式に問題:\n  " + "\n  ".join(problems)
