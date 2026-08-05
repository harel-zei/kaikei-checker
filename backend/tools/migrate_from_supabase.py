"""
Supabaseストレージ → サーバー内保存（backend/client_data/）への移行スクリプト。

Supabaseをやめてサーバー内保存に一本化する際に、保存済みの
・各クライアントの前期データ（前期仕訳帳・期首残高・補助残高）
・クライアント設定（除外科目・締め日）
・freee連携トークン
をローカルへ丸ごと持ってくる。

使い方（サーバー上で実行）:

    cd /path/to/kaikei-checker
    # 一時的にSupabaseの接続情報を渡して実行する（.env に書かなくてよい）
    SUPABASE_URL=https://xxxx.supabase.co \
    SUPABASE_SECRET_KEY=xxxxx \
    python backend/tools/migrate_from_supabase.py

    # 中身を確認するだけ（書き込まない）
    ... python backend/tools/migrate_from_supabase.py --dry-run

移行後は .env の SUPABASE_URL / SUPABASE_SECRET_KEY を空にして再起動すれば、
以降はサーバー内保存（backend/client_data/）だけで動作する。

※ 既存のローカルファイルは上書きされる。事前にバックアップを取ること:
    cp -r backend/client_data backend/client_data.bak
"""
import sys
from pathlib import Path

# backend/ を import パスに追加（このファイルは backend/tools/ にある）
BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

import supabase_store as sb  # noqa: E402

DATA_DIR = BACKEND_DIR / "client_data"

# クライアントごとに移行するファイル
CLIENT_FILES = [
    "prior_journal.txt",
    "prior_bal_main.txt",
    "prior_bal_sub.txt",
    "meta.json",
    "settings.json",
]
# システム領域のファイル（Supabase上のパス → ローカルの相対パス）
SYSTEM_FILES = {
    "_system/freee_tokens.json": "_freee_tokens.json",
}


def _local_dir_name(client_name: str) -> str:
    """client_store._client_dir と同じ規則でフォルダ名を作る
    （ファイル名に使えない文字を除去）"""
    return "".join(c for c in client_name if c not in r'\/:*?"<>|')


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    if not sb.is_enabled():
        print("エラー: SUPABASE_URL / SUPABASE_SECRET_KEY が設定されていません。")
        print("       環境変数で渡して実行してください（ファイル冒頭の使い方を参照）。")
        return 1

    print(f"接続先: {sb.SUPABASE_URL}  バケット: {sb.BUCKET}")
    print(f"保存先: {DATA_DIR}")
    if dry_run:
        print("※ --dry-run のため、実際の書き込みは行いません\n")
    else:
        print()

    # Supabaseはフォルダ名をクライアント名のハッシュ（c_xxxx）にしており、
    # 実際のクライアント名は meta.json の client_name に入っている。
    # 一方ローカルはクライアント名そのものがフォルダ名になるため、変換して保存する。
    try:
        folders = [
            item["name"] for item in sb._list("")
            if item.get("name") and item.get("id") is None
            and item["name"] != "_system"
        ]
    except Exception as e:
        print(f"エラー: クライアント一覧の取得に失敗しました: {e}")
        return 1

    if not folders:
        print("Supabase上にクライアントデータが見つかりませんでした。")

    total_files = 0
    migrated = 0
    for folder in folders:
        meta = sb._load_meta(folder)
        client_name = (meta.get("client_name") or folder).strip()
        local_name = _local_dir_name(client_name)
        print(f"■ {client_name}    （{folder} → client_data/{local_name}）")

        dest_dir = DATA_DIR / local_name
        got = 0
        for fname in CLIENT_FILES:
            try:
                content = sb._get(f"{folder}/{fname}")
            except Exception as e:
                print(f"    ! {fname}: 取得エラー {e}")
                continue
            if content is None:
                continue
            if not dry_run:
                dest_dir.mkdir(parents=True, exist_ok=True)
                (dest_dir / fname).write_text(content, encoding="utf-8")
            size = len(content.encode("utf-8"))
            print(f"    ✓ {fname}  ({size:,} bytes)")
            got += 1
            total_files += 1
        if got == 0:
            print("    （ファイルなし）")
        else:
            migrated += 1

    # freeeトークン等のシステムファイル
    print("■ システム領域")
    for remote, local in SYSTEM_FILES.items():
        try:
            content = sb._get(remote)
        except Exception as e:
            print(f"    ! {remote}: 取得エラー {e}")
            continue
        if content is None:
            print(f"    － {remote}（未保存）")
            continue
        if not dry_run:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            (DATA_DIR / local).write_text(content, encoding="utf-8")
        print(f"    ✓ {remote} → client_data/{local}")
        total_files += 1

    print(f"\n完了: {migrated}クライアント / {total_files}ファイル")
    if dry_run:
        print("※ --dry-run のため書き込みはしていません。問題なければ外して再実行してください。")
    else:
        print("次の手順:")
        print("  1. .env の SUPABASE_URL / SUPABASE_SECRET_KEY を空にする")
        print("  2. アプリを再起動する")
        print("  3. 画面でクライアント一覧と前期データが表示されることを確認する")
        print("  4. freee連携が引き継げていない場合は画面から再連携する")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
