"""
Supabase ストレージによるクライアントデータ永続化。

環境変数 SUPABASE_URL / SUPABASE_SECRET_KEY が設定されている場合に有効。
client_store.py から委譲される。ローカル版と同じ関数シグネチャを提供する。

ストレージ構成（バケット内）:
  {client}/prior_journal.txt
  {client}/prior_bal_main.txt
  {client}/prior_bal_sub.txt
  {client}/meta.json
  {client}/settings.json
"""
import os
import json
import hashlib
from datetime import datetime
from typing import Optional

import requests

# .env をロード（ローカル開発用。Renderでは環境変数が直接渡る）
try:
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(Path(__file__).parent.parent / ".env")
except Exception:
    pass

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SECRET_KEY") or ""
BUCKET = os.environ.get("SUPABASE_BUCKET", "kaikei-client-data")

PRIOR_FILES = {
    "prior_journal":   "前期仕訳帳",
    "prior_bal_main":  "前期首残高（主科目）",
    "prior_bal_sub":   "前期首補助残高",
}

_TIMEOUT = 30


def is_enabled() -> bool:
    """Supabase 連携が設定されているか"""
    return bool(SUPABASE_URL and SUPABASE_KEY)


def _headers(extra: dict = None) -> dict:
    h = {"Authorization": f"Bearer {SUPABASE_KEY}", "apikey": SUPABASE_KEY}
    if extra:
        h.update(extra)
    return h


def _safe(client_name: str) -> str:
    """
    クライアント名をストレージキー用の安全なフォルダ名に変換する。
    Supabaseのオブジェクトキーは日本語等を許可しないため、
    クライアント名のハッシュ（16桁hex）をフォルダ名に使う。
    本名は meta.json の "client_name" に保存して復元する。
    """
    return "c_" + hashlib.sha1(client_name.strip().encode("utf-8")).hexdigest()[:16]


# ── 低レベル：オブジェクト操作 ───────────────────────────────
def _put(path: str, content: str, content_type: str = "text/plain") -> None:
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{path}"
    r = requests.post(
        url,
        headers=_headers({"Content-Type": content_type, "x-upsert": "true"}),
        data=content.encode("utf-8"),
        timeout=_TIMEOUT,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Supabase put失敗 [{r.status_code}] {path}: {r.text[:200]}")


def _get(path: str) -> Optional[str]:
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{path}"
    r = requests.get(url, headers=_headers(), timeout=_TIMEOUT)
    if r.status_code == 200:
        return r.content.decode("utf-8")
    if r.status_code in (400, 404):
        return None
    raise RuntimeError(f"Supabase get失敗 [{r.status_code}] {path}: {r.text[:200]}")


def _delete(paths: list[str]) -> None:
    if not paths:
        return
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}"
    requests.delete(
        url,
        headers=_headers({"Content-Type": "application/json"}),
        json={"prefixes": paths},
        timeout=_TIMEOUT,
    )


def _list(prefix: str = "") -> list[dict]:
    """指定プレフィックス直下のオブジェクト一覧を返す"""
    url = f"{SUPABASE_URL}/storage/v1/object/list/{BUCKET}"
    r = requests.post(
        url,
        headers=_headers({"Content-Type": "application/json"}),
        json={"prefix": prefix, "limit": 1000, "sortBy": {"column": "name", "order": "asc"}},
        timeout=_TIMEOUT,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Supabase list失敗 [{r.status_code}]: {r.text[:200]}")
    return r.json()


# ── メタ情報 ───────────────────────────────
def _load_meta(client: str) -> dict:
    raw = _get(f"{client}/meta.json")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {}


def _save_meta(client: str, meta: dict) -> None:
    _put(f"{client}/meta.json", json.dumps(meta, ensure_ascii=False, indent=2), "application/json")


# ── 高レベル：client_store と同じ API ───────────────────────
def list_clients() -> list[dict]:
    clients = []
    for item in _list(""):
        # フォルダは id が None で返る
        folder = item.get("name")
        if not folder or item.get("id") is not None:
            continue
        meta = _load_meta(folder)
        # 本名は meta.json に保存されている（無ければフォルダ名で代替）
        name = meta.get("client_name") or folder
        clients.append({
            "name":        name,
            "saved_files": meta.get("saved_files", {}),
            "updated_at":  meta.get("updated_at", ""),
        })
    return sorted(clients, key=lambda c: c["name"])


def save_prior_files(client_name: str, files: dict) -> dict:
    client = _safe(client_name)
    meta = _load_meta(client)
    saved = meta.get("saved_files", {})

    for key, (filename, content) in files.items():
        if key not in PRIOR_FILES:
            continue
        _put(f"{client}/{key}.txt", content)
        saved[key] = {
            "original_name": filename,
            "saved_at": datetime.now().strftime("%Y/%m/%d %H:%M"),
        }

    meta["client_name"] = client_name
    meta["saved_files"] = saved
    meta["updated_at"]  = datetime.now().strftime("%Y/%m/%d %H:%M")
    _save_meta(client, meta)
    return {"client": client_name, "saved": list(files.keys()), "status": "ok"}


def load_prior_files(client_name: str) -> dict:
    client = _safe(client_name)
    return {key: _get(f"{client}/{key}.txt") for key in PRIOR_FILES}


def get_client_info(client_name: str) -> Optional[dict]:
    client = _safe(client_name)
    meta = _load_meta(client)
    if not meta:
        return None
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
    client = _safe(client_name)
    _delete([f"{client}/{file_key}.txt"])
    meta = _load_meta(client)
    meta.get("saved_files", {}).pop(file_key, None)
    _save_meta(client, meta)
    return True


def delete_client(client_name: str) -> bool:
    client = _safe(client_name)
    paths = [f"{client}/{it['name']}" for it in _list(f"{client}/") if it.get("name")]
    paths += [f"{client}/{k}.txt" for k in PRIOR_FILES]
    paths += [f"{client}/meta.json", f"{client}/settings.json"]
    _delete(list(set(paths)))
    return True


DEFAULT_SETTINGS = {
    "exclude_accounts":  [],
    "fiscal_cutoff_day": 1,
}


def get_client_settings(client_name: str) -> dict:
    client = _safe(client_name)
    raw = _get(f"{client}/settings.json")
    if not raw:
        return dict(DEFAULT_SETTINGS)
    try:
        data = json.loads(raw)
        for k, v in DEFAULT_SETTINGS.items():
            data.setdefault(k, v)
        return data
    except Exception:
        return dict(DEFAULT_SETTINGS)


def save_client_settings(client_name: str, settings: dict) -> dict:
    client = _safe(client_name)
    safe = {k: settings.get(k, v) for k, v in DEFAULT_SETTINGS.items()}
    _put(f"{client}/settings.json", json.dumps(safe, ensure_ascii=False, indent=2), "application/json")
    # 設定のみ保存したクライアントも一覧に出るよう meta に本名を記録
    meta = _load_meta(client)
    if not meta.get("client_name"):
        meta["client_name"] = client_name
        meta.setdefault("saved_files", {})
        meta["updated_at"] = datetime.now().strftime("%Y/%m/%d %H:%M")
        _save_meta(client, meta)
    return safe
