param([int]$OldPid = 0)

# Wait for current Telegram response to be delivered
Start-Sleep -Seconds 4

# Stop old bot process if running
if ($OldPid -gt 0) {
    Stop-Process -Id $OldPid -Force -ErrorAction SilentlyContinue
}

# Run updated bot
Set-Location 'C:\Users\Chatty\Projects\AntigravityTG'
& 'C:\Users\Chatty\AppData\Local\Programs\Python\Python313\python.exe' bot.py
