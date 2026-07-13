# 会計チェックシステム - Web自動アップロード
# このファイルをダブルクリックすると、最新の修正がWebに反映されます

Set-Location "C:\Users\iwasa\Documents\kaikei-checker"

# コミットメッセージを入力
$msg = Read-Host "今回の修正内容を一言で入力してください（例: 消費税チェック修正）"
if (-not $msg) { $msg = "システム更新" }

Write-Host ""
Write-Host "📦 ファイルをまとめています..." -ForegroundColor Cyan
git add .

Write-Host "💬 メモを保存しています..." -ForegroundColor Cyan
git commit -m $msg

Write-Host "🚀 Webにアップロード中..." -ForegroundColor Cyan
git push

Write-Host ""
Write-Host "✅ 完了！ 3〜5分後に反映されます" -ForegroundColor Green
Write-Host "🌐 https://kaikei-checker.onrender.com/" -ForegroundColor Yellow
Write-Host ""
Read-Host "Enterキーで閉じる"
