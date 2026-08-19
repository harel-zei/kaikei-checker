#!/bin/bash
# ============================================================
# 自動更新の起動用スクリプト（/usr/local/bin/kaikei-update に配置）
#
# 更新処理の本体は リポジトリ内の deploy/auto_update.sh 側にある。
# ここから呼び出すことで、更新スクリプト自体の改良も自動で反映される
# （導入スクリプトを毎回サーバーで実行し直す必要がない）。
#
# 実行中に git reset --hard で自分自身が書き換わると、bashが途中まで
# 読んだ状態で内容が変わり誤動作する。それを避けるため一時ファイルに
# コピーしてから実行する。
# ============================================================
set -u

REPO="${KAIKEI_REPO:-/opt/kaikei}"
SRC="$REPO/deploy/auto_update.sh"

if [ ! -r "$SRC" ]; then
    echo "更新スクリプトが見つかりません: $SRC" >&2
    exit 1
fi

TMP="$(mktemp /tmp/kaikei-update.XXXXXX)"
trap 'rm -f "$TMP"' EXIT
cp "$SRC" "$TMP"
bash "$TMP"
