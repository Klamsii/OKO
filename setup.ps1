$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$envPath = Join-Path $projectRoot '.env'
$modelDir = Join-Path $projectRoot 'model'
$runtimePath = Join-Path $modelDir 'model_runtime.py'

if (-not (Test-Path -LiteralPath $envPath)) {
    $random = [byte[]]::new(32)
    $taskRandomGenerator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $taskRandomGenerator.GetBytes($random) } finally { $taskRandomGenerator.Dispose() }
    $secret = [Convert]::ToBase64String($random).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    $template = Get-Content -LiteralPath (Join-Path $projectRoot '.env.example') -Raw -Encoding UTF8
    $configured = $template.Replace('replace-with-a-long-random-secret-key', $secret)
    Set-Content -LiteralPath $envPath -Value $configured -Encoding UTF8
    Write-Host 'Создан .env с новым API ключом.'
}

if (Select-String -LiteralPath $envPath -Pattern '^ANALYSIS_BACKEND=datathon$' -Quiet) {
    Write-Host 'OKO использует наш GPU API. Подключение: .\connect-server.ps1'
    return
}
if (-not (Test-Path -LiteralPath $modelDir)) {
    New-Item -ItemType Directory -Path $modelDir | Out-Null
}
if (-not (Test-Path -LiteralPath $runtimePath)) {
    Copy-Item -LiteralPath (Join-Path $projectRoot 'model_template/model_runtime.py') -Destination $runtimePath
    Write-Host 'Создан шаблон model/model_runtime.py для подключения модели.'
}
