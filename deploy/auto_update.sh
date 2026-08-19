#!/bin/bash
# ============================================================
# 会計データチェックシステム 自動更新スクリプト
#
# GitHubの指定ブランチに更新があれば取得し、サービスを再起動する。
# systemd タイマーから5分ごとに実行される（deploy/install_auto_update.sh 参照）。
#
# 安全対策:
#   ・取得後に「起動チェック」と「テスト」を行い、失敗したら元に戻す
#   ・再起動後にサービスが立ち上がらなければ元に戻して再起動する
#   → 壊れたコードが本番に残ったままにならない
#
# 手動で実行する場合:  sudo /usr/local/bin/kaikei-update
# 履歴の確認:          tail -30 /var/log/kaikei-update.log
# ============================================================
set -uo pipefail

REPO="${KAIKEI_REPO:-/opt/kaikei}"
BRANCH="${KAIKEI_BRANCH:-deploy/render-5yr4fy}"
VENV="${KAIKEI_VENV:-/opt/kaikei-venv}"
SERVICE="${KAIKEI_SERVICE:-kaikei}"
LOG="${KAIKEI_LOG:-/var/log/kaikei-update.log}"
LOG_MAX_BYTES=1048576   # 1MBを超えたら1世代だけ退避する

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"
}

# ログが肥大化しないように1世代だけローテートする
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt "$LOG_MAX_BYTES" ]; then
    mv -f "$LOG" "$LOG.1"
fi

cd "$REPO" 2>/dev/null || { log "❌ リポジトリが見つかりません: $REPO"; exit 1; }

# root で git を実行するとファイルが root 所有になる。
# アプリは backend/client_data/ に顧問先データを書き込むため、
# 更新後は「サービスを動かしているユーザー」に所有権を揃える必要がある。
# （更新前の所有者に戻すと、元が root 所有だった場合に書き込めなくなる）
SVC_USER="$(systemctl show "$SERVICE" -p User --value 2>/dev/null)"
[ -z "$SVC_USER" ] && SVC_USER=root
SVC_GROUP="$(systemctl show "$SERVICE" -p Group --value 2>/dev/null)"
[ -z "$SVC_GROUP" ] && SVC_GROUP="$(id -gn "$SVC_USER" 2>/dev/null || echo "$SVC_USER")"
OWNER="$SVC_USER:$SVC_GROUP"

# 追跡ブランチ（origin/xxx）の設定が無いリポジトリでも動くよう、
# fetch の結果は FETCH_HEAD で受け取る。
# あわせて refspec を補っておく（git status 等が正しく動くようにするため）。
if ! git config --get remote.origin.fetch >/dev/null 2>&1; then
    git config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'
    log "ℹ️  remote.origin.fetch が未設定だったため補いました"
fi

if ! git fetch --quiet origin "$BRANCH" 2>>"$LOG"; then
    log "❌ GitHubからの取得に失敗しました（ネットワークまたは認証）"
    exit 1
fi

LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse FETCH_HEAD)"

if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0   # 更新なし。ログにも残さない（毎5分の実行で埋まるため）
fi

log "🔄 更新を検出: ${LOCAL:0:7} → ${REMOTE:0:7}"

# デプロイ先なのでローカル変更は保持しない。
# 万一に備え、切り替え直前の状態を backup-before-update に残す。
git branch -f backup-before-update "$LOCAL" >/dev/null 2>&1
if ! git reset --hard --quiet "$REMOTE" 2>>"$LOG"; then
    log "❌ 切り替えに失敗しました。更新を中止します"
    exit 1
fi
[ -n "$OWNER" ] && chown -R "$OWNER" "$REPO" 2>/dev/null

rollback() {
    log "↩️  ${LOCAL:0:7} に差し戻します: $1"
    git reset --hard --quiet "$LOCAL"
    [ -n "$OWNER" ] && chown -R "$OWNER" "$REPO" 2>/dev/null
    systemctl restart "$SERVICE"
    exit 1
}

# requirements.txt が変わったときだけライブラリを更新する（通常は数秒で終わる）
if ! git diff --quiet "$LOCAL" "$REMOTE" -- requirements.txt 2>/dev/null; then
    log "📦 requirements.txt が変更されたためライブラリを更新します"
    if ! "$VENV/bin/pip" install -q -r requirements.txt >>"$LOG" 2>&1; then
        rollback "ライブラリの更新に失敗"
    fi
fi

# ① 起動チェック: アプリが import できること
if ! ( cd "$REPO/backend" && "$VENV/bin/python" -c "import main" ) >>"$LOG" 2>&1; then
    rollback "起動チェックに失敗"
fi

# ② テスト: pytest が入っていれば実行する（未導入の環境では省略）
if [ -x "$VENV/bin/pytest" ]; then
    if ! ( cd "$REPO" && "$VENV/bin/pytest" -q ) >>"$LOG" 2>&1; then
        rollback "テストに失敗"
    fi
fi

systemctl restart "$SERVICE"
sleep 3
if ! systemctl is-active --quiet "$SERVICE"; then
    rollback "再起動後にサービスが起動しない"
fi

log "✅ 更新完了: $(git log --oneline -1)"
