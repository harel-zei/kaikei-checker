"""
クライアント管理モジュール
前期データ（仕訳・残高）をクライアント別に保存・読み込みする。

保存先は2種類を自動切替:
  - Supabaseストレージ（環境変数 SUPABASE_URL/SUPABASE_SECRET_KEY 設定時）
    → Render等のクラウドでも再デプロイで消えない
  - ローカルファイル（未設定時）
    → localhost運用。PC内に保存され消えない
"""
import json
import shutil
from pathlib import Path
from datetime import datetime
from typing import Optional

import supabase_store as _sb

# Supabase連携が有効か（起動時に1回判定）
_USE_SB = _sb.is_enabled()

# クライアントデータの保存先（ローカル時）
DATA_DIR = Path(__file__).parent / "client_data"
DATA_DIR.mkdir(exist_ok=True)

# 保存するファイルの種類と表示名
PRIOR_FILES = {
    "prior_journal":   "前期仕訳帳",
    "prior_bal_main":  "前期首残高（主科目）",
    "prior_bal_sub":   "前期首補助残高",
}


def _client_dir(client_name: str) -> Path:
    """クライアントのデータディレクトリを返す（なければ作成）"""
    # ファイル名に使えない文字を除去
    safe_name = "".join(c for c in client_name if c not in r'\/:*?"<>|')
    d = DATA_DIR / safe_name
    d.mkdir(exist_ok=True)
    return d


def list_clients() -> list[dict]:
    """保存済みクライアント一覧を返す"""
    if _USE_SB:
        return _sb.list_clients()
    clients = []
    for d in sorted(DATA_DIR.iterdir()):
        if not d.is_dir():
            continue
        meta = _load_meta(d)
        clients.append({
            "name":        d.name,
            "saved_files": meta.get("saved_files", {}),
            "updated_at":  meta.get("updated_at", ""),
        })
    return clients


def save_prior_files(client_name: str, files: dict[str, tuple[str, str]]) -> dict:
    """
    前期ファイルを保存する。
    files: { "prior_journal": (filename, content), ... }
    Returns: 保存結果の辞書
    """
    if _USE_SB:
        return _sb.save_prior_files(client_name, files)
    d = _client_dir(client_name)
    meta = _load_meta(d)
    saved = meta.get("saved_files", {})

    for key, (filename, content) in files.items():
        if key not in PRIOR_FILES:
            continue
        # ファイルを保存
        (d / f"{key}.txt").write_text(content, encoding="utf-8")
        saved[key] = {
            "original_name": filename,
            "saved_at": datetime.now().strftime("%Y/%m/%d %H:%M"),
        }

    meta["saved_files"] = saved
    meta["updated_at"]  = datetime.now().strftime("%Y/%m/%d %H:%M")
    _save_meta(d, meta)

    return {
        "client": client_name,
        "saved":  list(files.keys()),
        "status": "ok",
    }


def load_prior_files(client_name: str) -> dict[str, Optional[str]]:
    """
    保存済みの前期ファイルを読み込んで返す。
    Returns: { "prior_journal": content or None, ... }
    """
    if _USE_SB:
        return _sb.load_prior_files(client_name)
    d = _client_dir(client_name)
    result = {}
    for key in PRIOR_FILES:
        path = d / f"{key}.txt"
        result[key] = path.read_text(encoding="utf-8") if path.exists() else None
    return result


def get_client_info(client_name: str) -> Optional[dict]:
    """クライアントの保存情報を返す。存在しなければ None"""
    if _USE_SB:
        return _sb.get_client_info(client_name)
    d = DATA_DIR / client_name
    if not d.is_dir():
        return None
    meta = _load_meta(d)
    saved = meta.get("saved_files", {})
    return {
        "name":        client_name,
        "updated_at":  meta.get("updated_at", ""),
        "saved_files": {
            k: {
                "label":         PRIOR_FILES[k],
                "original_name": saved.get(k, {}).get("original_name", ""),
                "saved_at":      saved.get(k, {}).get("saved_at", ""),
                "exists":        k in saved,
            }
            for k in PRIOR_FILES
        },
    }


def delete_prior_file(client_name: str, file_key: str) -> bool:
    """特定の前期ファイルを削除する"""
    if _USE_SB:
        return _sb.delete_prior_file(client_name, file_key)
    d = DATA_DIR / client_name
    if not d.is_dir():
        return False
    path = d / f"{file_key}.txt"
    if path.exists():
        path.unlink()
    meta = _load_meta(d)
    meta.get("saved_files", {}).pop(file_key, None)
    _save_meta(d, meta)
    return True


def delete_client(client_name: str) -> bool:
    """クライアントのデータをすべて削除する"""
    if _USE_SB:
        return _sb.delete_client(client_name)
    d = DATA_DIR / client_name
    if not d.is_dir():
        return False
    shutil.rmtree(d)
    return True


# ── クライアント設定（除外科目など）──────────────────────────

DEFAULT_SETTINGS = {
    "exclude_accounts":   [],  # チェック対象外の勘定科目リスト（例: ["売掛金","受取手形"]）
    "fiscal_cutoff_day":  1,   # 月次締め日（1=月末, 20=20日締めなど）
}


def get_client_settings(client_name: str) -> dict:
    """クライアントのチェック設定を取得する。存在しなければデフォルトを返す"""
    if _USE_SB:
        return _sb.get_client_settings(client_name)
    d = DATA_DIR / client_name
    if not d.is_dir():
        return dict(DEFAULT_SETTINGS)
    settings_path = d / "settings.json"
    if not settings_path.exists():
        return dict(DEFAULT_SETTINGS)
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        # デフォルトキーを補完
        for k, v in DEFAULT_SETTINGS.items():
            data.setdefault(k, v)
        return data
    except Exception:
        return dict(DEFAULT_SETTINGS)


def save_client_settings(client_name: str, settings: dict) -> dict:
    """クライアントのチェック設定を保存する"""
    if _USE_SB:
        return _sb.save_client_settings(client_name, settings)
    d = _client_dir(client_name)
    # 既知キーのみ保存（不正なキーを除外）
    safe = {k: settings.get(k, v) for k, v in DEFAULT_SETTINGS.items()}
    (d / "settings.json").write_text(
        json.dumps(safe, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    return safe


# ── 内部ユーティリティ ──
def _load_meta(d: Path) -> dict:
    meta_path = d / "meta.json"
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_meta(d: Path, meta: dict) -> None:
    (d / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
