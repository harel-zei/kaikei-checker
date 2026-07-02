"""
freee 会計 API クライアント。

OAuth2（認可コードフロー）でトークンを取得・更新し、
事業所一覧・仕訳帳CSV・試算表を取得する。

トークンは freee_token_store 経由で Supabase / ローカルに永続化する。
1回の認可でアカウント内の全事業所にアクセスするため、認可URLでは
prompt=select_company を付けない（＝全事業所認可）。
※この方式はfreee公式で将来廃止予定。廃止時は事業所ごと認可に切替。
"""
import os
import time
from datetime import datetime, timezone

import requests

try:
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(Path(__file__).parent.parent / ".env")
except Exception:
    pass

FREEE_CLIENT_ID     = os.environ.get("FREEE_CLIENT_ID", "")
FREEE_CLIENT_SECRET = os.environ.get("FREEE_CLIENT_SECRET", "")
FREEE_REDIRECT_URI  = os.environ.get("FREEE_REDIRECT_URI", "")

AUTH_BASE  = "https://accounts.secure.freee.co.jp/public_api/authorize"
TOKEN_URL  = "https://accounts.secure.freee.co.jp/public_api/token"
API_BASE   = "https://api.freee.co.jp"
_TIMEOUT   = 60


def is_configured() -> bool:
    """OAuthに必要な設定が揃っているか"""
    return bool(FREEE_CLIENT_ID and FREEE_CLIENT_SECRET and FREEE_REDIRECT_URI)


# ── OAuth ────────────────────────────────────────────────
def build_authorize_url(state: str = "") -> str:
    """freee認可画面のURLを生成する。全事業所認可（prompt=select_companyを付けない）。"""
    params = {
        "client_id": FREEE_CLIENT_ID,
        "redirect_uri": FREEE_REDIRECT_URI,
        "response_type": "code",
    }
    if state:
        params["state"] = state
    q = "&".join(f"{k}={requests.utils.quote(str(v), safe='')}" for k, v in params.items())
    return f"{AUTH_BASE}?{q}"


def exchange_code(code: str) -> dict:
    """認可コードをアクセストークンに交換する。"""
    r = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "client_id": FREEE_CLIENT_ID,
        "client_secret": FREEE_CLIENT_SECRET,
        "code": code,
        "redirect_uri": FREEE_REDIRECT_URI,
    }, timeout=_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"トークン取得失敗 [{r.status_code}]: {r.text[:300]}")
    return _with_expiry(r.json())


def refresh(refresh_token: str) -> dict:
    """リフレッシュトークンでアクセストークンを更新する。"""
    r = requests.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "client_id": FREEE_CLIENT_ID,
        "client_secret": FREEE_CLIENT_SECRET,
        "refresh_token": refresh_token,
    }, timeout=_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"トークン更新失敗 [{r.status_code}]: {r.text[:300]}")
    return _with_expiry(r.json())


def _with_expiry(tok: dict) -> dict:
    """expires_in から絶対時刻 expires_at(UNIX秒) を計算して付与。"""
    now = int(time.time())
    tok["expires_at"] = now + int(tok.get("expires_in", 21600)) - 120  # 2分の余裕
    return tok


# ── データ取得 ───────────────────────────────────────────
def _headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}


def list_companies(access_token: str) -> list:
    """アクセス可能な事業所一覧を返す。"""
    r = requests.get(f"{API_BASE}/api/1/companies", headers=_headers(access_token), timeout=_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"事業所一覧取得失敗 [{r.status_code}]: {r.text[:300]}")
    return r.json().get("companies", [])


def get_journal_csv(access_token: str, company_id: int,
                    start_date: str = None, end_date: str = None,
                    max_wait: int = 90) -> str:
    """仕訳帳をCSV（sjis）で取得する。非同期生成をポーリングして完了後にダウンロード。"""
    params = {"company_id": company_id, "download_type": "csv"}
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    r = requests.get(f"{API_BASE}/api/1/journals", headers=_headers(access_token),
                     params=params, timeout=_TIMEOUT)
    if r.status_code not in (200, 202):
        raise RuntimeError(f"仕訳帳依頼失敗 [{r.status_code}]: {r.text[:300]}")
    job = r.json().get("journals", {})
    job_id = job.get("id")
    if not job_id:
        raise RuntimeError(f"仕訳帳のjob_idが取得できません: {r.text[:200]}")

    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(3)
        s = requests.get(f"{API_BASE}/api/1/journals/reports/{job_id}/status",
                         headers=_headers(access_token),
                         params={"company_id": company_id}, timeout=_TIMEOUT)
        status = s.json().get("journals", {}).get("status") if s.status_code == 200 else None
        if status in ("finished", "uploaded"):
            dl = requests.get(f"{API_BASE}/api/1/journals/reports/{job_id}/download",
                              headers=_headers(access_token),
                              params={"company_id": company_id}, timeout=_TIMEOUT)
            if dl.status_code == 200 and len(dl.content) > 50:
                # freeeの仕訳帳CSVは sjis(cp932)
                try:
                    return dl.content.decode("shift_jis")
                except UnicodeDecodeError:
                    return dl.content.decode("cp932", errors="replace")
    raise RuntimeError("仕訳帳の生成がタイムアウトしました。時間をおいて再試行してください。")
