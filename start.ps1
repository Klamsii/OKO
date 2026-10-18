param([int]$Port = 8000, [switch]$Docker)
$ErrorActionPreference = 'Stop'
$taskProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $taskProjectRoot 'setup.ps1')
Push-Location $taskProjectRoot
try {
    if ($Docker) {
        if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Docker Desktop не найден.' }
        docker compose -f compose.datathon.yaml up --build -d
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось запустить Docker Compose.' }
        Write-Host 'Сервис запущен: http://localhost:8000/'
    } else {
        $taskPython = Join-Path $taskProjectRoot '.venv/Scripts/python.exe'
        if (-not (Test-Path -LiteralPath $taskPython)) {
            py -m venv .venv
            if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать Python окружение.' }
        }
        $taskRequirements = Join-Path $taskProjectRoot 'requirements.txt'
        $taskRequirementsHash = (Get-FileHash -LiteralPath $taskRequirements -Algorithm SHA256).Hash
        $taskStamp = Join-Path $taskProjectRoot '.venv/requirements.sha256'
        $taskPreviousHash = if (Test-Path -LiteralPath $taskStamp) { (Get-Content -LiteralPath $taskStamp -Raw).Trim() } else { '' }
        if ($taskRequirementsHash -ne $taskPreviousHash) {
            & $taskPython -m pip install -r $taskRequirements
            if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости.' }
            Set-Content -LiteralPath $taskStamp -Value $taskRequirementsHash
        }
        Write-Host "OKO: http://127.0.0.1:$Port/ (подключение: .\connect-server.ps1; ключ находится в .env)"
        & $taskPython -m app --port $Port
    }
} finally { Pop-Location }
