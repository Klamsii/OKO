param([string]$ServerUrl = '')
$ErrorActionPreference = 'Stop'
$taskProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ServerUrl) { $ServerUrl = Read-Host 'Адрес нашего API (HTTPS сервера или http://127.0.0.1:8766 для локальной модели)' }
$taskUri = $null
if (-not [Uri]::TryCreate($ServerUrl, [UriKind]::Absolute, [ref]$taskUri) -or $taskUri.Scheme -notin @('http','https') -or $taskUri.UserInfo -or $taskUri.Query -or $taskUri.Fragment) { throw 'Нужен URL API без пароля и параметров, не адрес Jupyter.' }
$taskSecureKey = Read-Host 'Служебный ключ API модели (ввод скрыт; ключ входа на сайт сохранится)' -AsSecureString
$taskKeyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($taskSecureKey)
try {
    $taskKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($taskKeyPointer)
    if ($taskKey.Length -lt 24 -or $taskKey.Contains("`n") -or $taskKey.Contains("`r")) { throw 'Нужен ключ минимум из 24 символов без перевода строки.' }
    try {
        $taskHealth = Invoke-RestMethod -Uri ($ServerUrl.TrimEnd('/') + '/health') -Headers @{'X-API-Key'=$taskKey} -TimeoutSec 8 -MaximumRedirection 0
        if ($taskHealth.ready -isnot [bool]) { throw 'Неверный API' }
    } catch { throw 'API Qwen не подтвердил подключение. Проверьте адрес и служебный ключ. Прежние настройки сохранены.' }
    & (Join-Path $taskProjectRoot 'setup.ps1')
    $taskEnvPath = Join-Path $taskProjectRoot '.env'
    $taskLines = @(Get-Content -LiteralPath $taskEnvPath -Encoding UTF8 | Where-Object { $_ -notmatch '^(DATATHON_API_KEY|DATATHON_API_URL|ANALYSIS_BACKEND)=' })
    $taskLines += @('ANALYSIS_BACKEND=datathon', "DATATHON_API_URL=$($ServerUrl.TrimEnd('/'))", "DATATHON_API_KEY=$taskKey")
    [IO.File]::WriteAllLines($taskEnvPath, $taskLines, [Text.UTF8Encoding]::new($false))
    Write-Host 'Подключение проверено и сохранено. Перезапустите сайт через .\start.ps1. Пользователи входят с прежним API_KEY сайта.'
    if (-not $taskHealth.ready) { Write-Host 'Qwen ещё загружается; сайт автоматически покажет готовность.' }
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($taskKeyPointer)
    $taskKey = $null; $taskLines = $null
}
