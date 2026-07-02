"""
freee OAuthトークンの永続化。

1回の認可でアカウント内全事業所にアクセスできるため、
事務所アカウント単位でトークン1組（access/refresh/expires_at）を保持する。

保存先は client_store と同じく Supabase（設定時）またはローカルファイルに自動切替。
"""
import os
import json
import time
from pathlib import Path

import freee_client

# Supabase が使えるかは client_store の判定に相乗り
try:
    import supabase_store as _sb
    _USE_SB = _sb.is_enabled()
except Exception:
    _sb = None
    _USE_SB = False

_LOCAL_PATH = Path(__file__).parent / "client_data" / "_freee_oauth.json"
_SB_PATH = "_system/freee_oauth.json"


def _load_raw() -> dict:
    if _USE_SB:
        raw = _sb._get(_SB_PATH)
        return json.loads(raw) if raw else {}
    if _LOCAL_PATH.exists():
        try:
            return json.loads(_LOCAL_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_raw(tok: dict) -> None:
    body = json.dumps(tok, ensure_ascii=False, indent=2)
    if _USE_SB:
        _sb._put(_SB_PATH, body, "application/json")
    else:
        _LOCAL_PATH.parent.mkdir(exist_ok=True)
        _LOCAL_PATH.write_text(body, encoding="utf-8")


def save_token(tok: dict) -> None:
    _save_raw(tok)


def is_connected() -> bool:
    t = _load_raw()
    return bool(t.get("access_token") and t.get("refresh_token"))


def clear() -> None:
    if _USE_SB:
        _sb._delete([_SB_PATH])
    elif _LOCAL_PATH.exists():
        _LOCAL_PATH.unlink()


def get_valid_access_token() -> str:
    """有効なアクセストークンを返す。期限切れならリフレッシュして保存する。"""
    t = _load_raw()
    if not t.get("access_token"):
        raise RuntimeError("freeeと未連携です。先に認可を行ってください。")
    if int(t.get("expires_at", 0)) <= int(time.time()):
        # 期限切れ → リフレッシュ
        new = freee_client.refresh(t["refresh_token"])
        # freeeはリフレッシュトークンもローテーションする場合があるので必ず保存
        _save_raw(new)
        return new["access_token"]
    return t["access_token"]
