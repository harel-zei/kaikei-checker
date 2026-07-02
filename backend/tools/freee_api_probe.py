"""
freee API 検証用プローブ（下調べ・接続確認のための使い捨てスクリプト）

使い方:
  1. freee Developers で開発者登録 → 開発用テスト事業所とアクセストークンを取得
  2. 環境変数にトークンをセットして実行
     PowerShell:  $env:FREEE_TOKEN = "eyJ..."; python tools/freee_api_probe.py
     bash:        FREEE_TOKEN=eyJ... python tools/freee_api_probe.py

このスクリプトは本番機能ではなく、APIで何が取得できるかを確かめるための検証用。
取得できた項目を確認したうえで、本実装（OAuth・トークン管理・パーサ連携）に進む。
"""
import os
import sys
import io
import json
import time

import requests

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

BASE = "https://api.freee.co.jp"
TOKEN = os.environ.get("FREEE_TOKEN", "").strip()


def _headers():
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json",
        "X-Api-Version": "2020-06-15",
    }


def _get(path, params=None):
    r = requests.get(f"{BASE}{path}", headers=_headers(), params=params or {}, timeout=30)
    return r


def main():
    if not TOKEN:
        print("環境変数 FREEE_TOKEN が未設定です。開発用アクセストークンをセットしてください。")
        return

    # 1. 事業所一覧
    r = _get("/api/1/companies")
    print(f"[事業所一覧] HTTP {r.status_code}")
    if r.status_code != 200:
        print(r.text[:500])
        return
    companies = r.json().get("companies", [])
    for c in companies:
        print(f"  company_id={c['id']}  {c.get('display_name') or c.get('name')}")
    if not companies:
        print("  事業所が見つかりません。")
        return

    # 権限のある事業所（開発用テスト事業所）を優先して選ぶ
    test_co = next((c for c in companies if "開発用テスト" in (c.get("display_name") or c.get("name") or "")), None)
    target = test_co or companies[0]
    cid = target["id"]
    print(f"\n▼ company_id={cid}（{target.get('display_name') or target.get('name')}）で試算表・仕訳帳を取得テスト\n")

    # 2. 試算表(PL) — 正しくは /api/1/reports/trial_pl
    r = _get("/api/1/reports/trial_pl", {"company_id": cid})
    print(f"[試算表PL] HTTP {r.status_code}")
    if r.status_code == 200:
        bals = r.json().get("trial_pl", {}).get("balances", [])
        print(f"  勘定科目 {len(bals)}件（先頭5件）:")
        for b in bals[:5]:
            print(f"    {b.get('account_item_name')}: 期間残高 {b.get('closing_balance')}")
    else:
        print("  " + r.text[:300])

    # 3. 試算表(BS) — 正しくは /api/1/reports/trial_bs
    r = _get("/api/1/reports/trial_bs", {"company_id": cid})
    print(f"[試算表BS] HTTP {r.status_code}")
    if r.status_code == 200:
        bals = r.json().get("trial_bs", {}).get("balances", [])
        print(f"  勘定科目 {len(bals)}件")
    else:
        print("  " + r.text[:200])

    # 4. 仕訳帳（非同期ダウンロード）
    print("[仕訳帳] ダウンロード依頼 (download_type=csv)")
    r = _get("/api/1/journals", {"company_id": cid, "download_type": "csv"})
    print(f"  受付 HTTP {r.status_code}")
    if r.status_code in (200, 202):
        job = r.json().get("journals", {})
        job_id = job.get("id")
        print(f"  job_id={job_id} status={job.get('status')}")
        # ステータス確認をポーリング（最大5回）
        for _ in range(5):
            time.sleep(3)
            s = _get(f"/api/1/journals/{job_id}/status", {"company_id": cid})
            if s.status_code == 200:
                st = s.json().get("journals", {})
                print(f"  status={st.get('status')}")
                if st.get("status") == "finished":
                    dl = _get(f"/api/1/journals/{job_id}/download", {"company_id": cid})
                    print(f"  ダウンロード HTTP {dl.status_code}, 先頭200字:")
                    print("  " + dl.text[:200].replace("\n", " "))
                    break
    else:
        print("  " + r.text[:300])


if __name__ == "__main__":
    main()
