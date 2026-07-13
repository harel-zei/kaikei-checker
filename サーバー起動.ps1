# 会計チェックシステム ローカルサーバー起動スクリプト
# - すでに起動中なら何もせずブラウザだけ開く
# - 未起動ならバックグラウンドで起動（ウィンドウ非表示）してブラウザを開く

$port = 8000
$backend = Join-Path $PSScriptRoot "backend"

$running = (Test-NetConnection -ComputerName localhost -Port $port -WarningAction SilentlyContinue).TcpTestSucceeded

if (-not $running) {
    Start-Process python -ArgumentList "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "$port", "--reload" `
        -WorkingDirectory $backend -WindowStyle Hidden
    # 起動を最大15秒待つ
    foreach ($i in 1..15) {
        Start-Sleep -Seconds 1
        if ((Test-NetConnection -ComputerName localhost -Port $port -WarningAction SilentlyContinue).TcpTestSucceeded) { break }
    }
}

Start-Process "http://localhost:$port"
