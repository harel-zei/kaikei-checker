#!/bin/bash
# ============================================================
# 自動更新の導入スクリプト（サーバーで1回だけ実行する）
#
#   sudo bash deploy/install_auto_update.sh
#
# 行うこと:
#   1. git の safe.directory 設定（root で git を実行できるようにする）
#   2. 更新スクリプトを /usr/local/bin/kaikei-update に配置
#   3. systemd タイマーを作成して5分ごとに実行
#   4. その場で1回実行して最新化
# ============================================================
set -euo pipefail

REPO="${KAIKEI_REPO:-/opt/kaikei}"
BRANCH="${KAIKEI_BRANCH:-deploy/render-5yr4fy}"
VENV="${KAIKEI_VENV:-/opt/kaikei-venv}"
SERVICE="${KAIKEI_SERVICE:-kaikei}"
INTERVAL="${KAIKEI_INTERVAL:-1min}"
BIN=/usr/local/bin/kaikei-update

if [ "$(id -u)" -ne 0 ]; then
    echo "❌ root で実行してください（sudo bash deploy/install_auto_update.sh）"
    exit 1
fi

echo "── 自動更新の導入 ──────────────────────────"
echo "  リポジトリ : $REPO"
echo "  ブランチ   : $BRANCH"
echo "  サービス   : $SERVICE"
echo "  実行間隔   : $INTERVAL"
echo

# 1. root から git を実行できるようにする（所有者が異なる場合の安全装置を解除）
if ! git config --global --get-all safe.directory 2>/dev/null | grep -qx "$REPO"; then
    git config --global --add safe.directory "$REPO"
    echo "✅ git safe.directory に $REPO を追加しました"
fi

# 2. 更新スクリプトを配置
install -m 0755 "$REPO/deploy/auto_update.sh" "$BIN"
echo "✅ 更新スクリプトを $BIN に配置しました"

# 3. systemd のサービスとタイマーを作成
cat > /etc/systemd/system/kaikei-update.service <<EOF
[Unit]
Description=Kaikei checker auto update
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
Environment=KAIKEI_REPO=$REPO
Environment=KAIKEI_BRANCH=$BRANCH
Environment=KAIKEI_VENV=$VENV
Environment=KAIKEI_SERVICE=$SERVICE
ExecStart=$BIN
EOF

cat > /etc/systemd/system/kaikei-update.timer <<EOF
[Unit]
Description=Kaikei checker auto update timer

[Timer]
OnBootSec=2min
OnUnitActiveSec=$INTERVAL
AccuracySec=30s

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now kaikei-update.timer >/dev/null
echo "✅ タイマーを有効化しました（${INTERVAL}ごとに確認）"
echo

# 4. その場で1回実行する
echo "── 初回の更新を実行します ──────────────────"
KAIKEI_REPO="$REPO" KAIKEI_BRANCH="$BRANCH" KAIKEI_VENV="$VENV" \
    KAIKEI_SERVICE="$SERVICE" "$BIN" || true

echo
echo "── 現在の状態 ──────────────────────────────"
cd "$REPO" && git log --oneline -1
systemctl is-active "$SERVICE" | sed 's/^/  サービス: /'
echo
echo "✅ 自動更新を有効化しました。以降は最大${INTERVAL}で自動反映されます。"
echo "   履歴の確認: tail -30 /var/log/kaikei-update.log"
echo "   今すぐ反映: sudo $BIN"
echo "   停止したい: sudo systemctl disable --now kaikei-update.timer"
