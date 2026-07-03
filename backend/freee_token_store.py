"""
freee OAuthトークンの永続化（事業所別）。

freeeの標準フロー（prompt=select_company）では、1回の認可で1事業所分の
トークンが発行される。そのため事業所ID(company_id)ごとにトークンを保持する。
顧問先を1社ずつ連携すれば、それぞれ確実にアクセスできる。

保存先は client_store と同じく Supabase（設定時）またはローカルファイル。
形式: { "<company_id>": {access_token, refresh_token, expires_at}, ... }
"""
import os
import json
import time
from pathlib import Path

import freee_client

try:
    import supabase_store as _sb
    _USE_SB = _sb.is_enabled()
except Exception:
    _sb = None
    _USE_SB = False

_LOCAL_PATH = Path(__file__).parent / "client_data" / "_freee_tokens.json"
_SB_PATH = "_system/freee_tokens.json"


def _load_all() -> dict:
    if _USE_SB:
        raw = _sb._get(_SB_PATH)
        return json.loads(raw) if raw else {}
    if _LOCAL_PATH.exists():
        try:
            return json.loads(_LOCAL_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_all(data: dict) -> None:
    body = json.dumps(data, ensure_ascii=False, indent=2)
    if _USE_SB:
        _sb._put(_SB_PATH, body, "application/json")
    else:
        _LOCAL_PATH.parent.mkdir(exist_ok=True)
        _LOCAL_PATH.write_text(body, encoding="utf-8")


def save_token(tok: dict) -> None:
    """交換で得たトークンを、そのトークンの company_id をキーに保存する。"""
    cid = str(tok.get("company_id") or "").strip()
    if not cid:
        raise RuntimeError("トークンに company_id が含まれていません")
    data = _load_all()
    data[cid] = tok
    _save_all(data)


def is_connected() -> bool:
    return bool(_load_all())


def connected_company_ids() -> list:
    return [int(k) for k in _load_all().keys() if k.isdigit()]


def clear() -> None:
    """全事業所のトークンを削除。"""
    if _USE_SB:
        _sb._delete([_SB_PATH])
    elif _LOCAL_PATH.exists():
        _LOCAL_PATH.unlink()


def _valid(tok: dict) -> str:
    """トークンが期限切れならリフレッシュして返す。呼び出し側で保存する。"""
    if int(tok.get("expires_at", 0)) <= int(time.time()):
        new = freee_client.refresh(tok["refresh_token"])
        # リフレッシュ応答に company_id が無い場合は元の値を引き継ぐ
        new.setdefault("company_id", tok.get("company_id"))
        return new
    return tok


def get_valid_access_token(company_id: int) -> str:
    """指定事業所の有効なアクセストークンを返す。未連携なら例外。"""
    cid = str(company_id)
    data = _load_all()
    tok = data.get(cid)
    if not tok:
        raise RuntimeError(
            "この事業所はまだ連携されていません。"
            "「freeeと連携する」から、この事業所を選んで認可してください。"
        )
    fresh = _valid(tok)
    if fresh is not tok:
        data[cid] = fresh
        _save_all(data)
    return fresh["access_token"]


def get_any_access_token() -> str:
    """事業所一覧の取得用に、任意の有効トークンを返す（一覧はユーザー単位で全社返る）。"""
    data = _load_all()
    if not data:
        raise RuntimeError("freeeと未連携です。先に連携してください。")
    cid, tok = next(iter(data.items()))
    fresh = _valid(tok)
    if fresh is not tok:
        data[cid] = fresh
        _save_all(data)
    return fresh["access_token"]
