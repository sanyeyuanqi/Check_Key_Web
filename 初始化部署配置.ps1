param(
    [string]$FrontendUrl = "http://localhost:3000",
    [string]$BackendUrl = "http://localhost:8000",
    [string]$AdminUsername = "sanyeyuanqi"
)

$ErrorActionPreference = "Stop"
$targetPath = Join-Path $PSScriptRoot ".env.deploy"

if (Test-Path -LiteralPath $targetPath) {
    throw "部署配置已存在：$targetPath。如需重新生成，请先备份并手动删除该文件。"
}

function New-UrlSafeSecret([int]$byteCount, [bool]$keepPadding = $false) {
    $bytes = [byte[]]::new($byteCount)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $value = [Convert]::ToBase64String($bytes).Replace('+', '-').Replace('/', '_')
    if (-not $keepPadding) { $value = $value.TrimEnd('=') }
    return $value
}

$postgresPassword = New-UrlSafeSecret 24
$adminPassword = New-UrlSafeSecret 24
$sessionSecret = New-UrlSafeSecret 64
$fernetKey = New-UrlSafeSecret 32 $true

$lines = @(
    "POSTGRES_USER=root",
    "POSTGRES_PASSWORD=$postgresPassword",
    "POSTGRES_DB=key_control",
    "DATABASE_URL=postgresql+asyncpg://root:$postgresPassword@postgres:5432/key_control",
    "REDIS_URL=redis://redis:6379/0",
    "",
    "FRONTEND_ORIGINS=$($FrontendUrl.TrimEnd('/'))",
    "NEXT_PUBLIC_API_BASE_URL=$($BackendUrl.TrimEnd('/'))",
    "FRONTEND_BIND_ADDRESS=0.0.0.0",
    "FRONTEND_PORT=3000",
    "BACKEND_BIND_ADDRESS=0.0.0.0",
    "BACKEND_PORT=8000",
    "",
    "BOOTSTRAP_ADMIN_USERNAME=$AdminUsername",
    "BOOTSTRAP_ADMIN_PASSWORD=$adminPassword",
    "SESSION_SECRET=$sessionSecret",
    "FERNET_KEY=$fernetKey",
    "SESSION_COOKIE_SECURE=false",
    "",
    "BURST_WINDOW_SECONDS=10",
    "BURST_REQUEST_LIMIT=20"
)

[IO.File]::WriteAllLines($targetPath, $lines, [Text.UTF8Encoding]::new($false))

Write-Host "部署配置已生成：$targetPath" -ForegroundColor Green
Write-Host "管理员账号：$AdminUsername" -ForegroundColor Cyan
Write-Host "管理员密码：$adminPassword" -ForegroundColor Cyan
Write-Host "请立即安全保存管理员密码；系统不会再次显示该初始明文密码。" -ForegroundColor Yellow
