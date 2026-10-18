param([string]$ServerUrl = '')
$ErrorActionPreference = 'Stop'
$taskProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ServerUrl) { $ServerUrl = Read-Host 'Адрес нашего API (например http://127.0.0.1:8765)' }
$taskUri = $null
if (-not [Uri]::TryCreate($ServerUrl, [UriKind]::Absolute, [ref]$taskUri) -or $taskUri.Scheme -notin @('http','https') -or $taskUri.UserInfo -or $taskUri.Query -or $taskUri.Fragment) { throw 'Нужен URL API без пароля и параметров, не адрес Jupyter.' }
$taskSecureKey = Read-Host 'Ключ сервера (ввод скрыт; этот же ключ используется для входа на сайт)' -AsSecureString
$taskKeyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($taskSecureKey)
try {
    $taskKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($taskKeyPointer)
    if ($taskKey.Length -lt 24 -or $taskKey.Contains("`n") -or $taskKey.Contains("`r")) { throw 'Нужен ключ минимум из 24 символов без перевода строки.' }
    & (Join-Path $taskProjectRoot 'setup.ps1')
    $taskEnvPath = Join-Path $taskProjectRoot '.env'
    $taskLines = @(Get-Content -LiteralPath $taskEnvPath -Encoding UTF8 | Where-Object { $_ -notmatch '^(API_KEY|DATATHON_API_KEY|DATATHON_API_URL|ANALYSIS_BACKEND)=' })
    $taskLines += @('ANALYSIS_BACKEND=datathon', "DATATHON_API_URL=$($ServerUrl.TrimEnd('/'))", "API_KEY=$taskKey", "DATATHON_API_KEY=$taskKey")
    [IO.File]::WriteAllLines($taskEnvPath, $taskLines, [Text.UTF8Encoding]::new($false))
    Write-Host 'Подключение сохранено локально. Запустите .\start.ps1; на сайте введите тот же ключ.'
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($taskKeyPointer)
    $taskKey = $null; $taskLines = $null
}
