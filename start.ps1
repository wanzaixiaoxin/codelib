param(
    [string]$ConfigPath = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectRoot

if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    if (-not [string]::IsNullOrWhiteSpace($env:CODELIB_CONFIG)) {
        $ConfigPath = $env:CODELIB_CONFIG
    } else {
        $ConfigPath = "config/config.yaml"
    }
}
if (-not [System.IO.Path]::IsPathRooted($ConfigPath)) {
    $ConfigPath = Join-Path $ProjectRoot $ConfigPath
}
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw "配置文件不存在：$ConfigPath"
}

$Python = Get-Command python -ErrorAction SilentlyContinue
if (-not $Python) {
    throw "找不到 python。请先安装 Python 并确保它已加入 PATH。"
}

$PreviousAdminUsername = $env:CODELIB_ADMIN_USERNAME
$PreviousAdminPassword = $env:CODELIB_ADMIN_PASSWORD
$ExitCode = 1
try {
    if ([string]::IsNullOrWhiteSpace($env:CODELIB_ADMIN_USERNAME)) {
        $env:CODELIB_ADMIN_USERNAME = "admin"
    }

    $CountCode = "import sys; from app.config import AppConfig; from app.store import Store; print(len(Store(AppConfig.load(sys.argv[1]).db_path).list_users()))"
    $UserCountText = & $Python.Source -c $CountCode $ConfigPath
    if ($LASTEXITCODE -ne 0) {
        throw "读取账号数据库失败，请检查 Python 依赖和配置文件。"
    }
    $UserCount = 0
    if (-not [int]::TryParse(($UserCountText | Select-Object -Last 1), [ref]$UserCount)) {
        throw "无法读取现有账号数量。"
    }

    if ($UserCount -eq 0) {
        if ([string]::IsNullOrEmpty($env:CODELIB_ADMIN_PASSWORD)) {
            Write-Host "首次启动：将创建管理员账号 '$env:CODELIB_ADMIN_USERNAME'。"
            do {
                $SecurePassword = Read-Host "请输入管理员密码（至少 8 位）" -AsSecureString
                if ($SecurePassword.Length -lt 8) {
                    Write-Host "密码至少需要 8 位，请重新输入。" -ForegroundColor Yellow
                }
            } while ($SecurePassword.Length -lt 8)

            $PasswordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecurePassword)
            try {
                $env:CODELIB_ADMIN_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($PasswordPointer)
            } finally {
                [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($PasswordPointer)
            }
        }
        if ($env:CODELIB_ADMIN_PASSWORD.Length -lt 8) {
            throw "CODELIB_ADMIN_PASSWORD 至少需要 8 位。"
        }
    } else {
        Write-Host "发现 $UserCount 个现有账号，将沿用数据库账号。"
    }

    Write-Host "启动 codelib（配置：$ConfigPath）..."
    & $Python.Source -m app --config $ConfigPath serve
    $ExitCode = $LASTEXITCODE
} finally {
    if ($null -eq $PreviousAdminUsername) {
        Remove-Item Env:CODELIB_ADMIN_USERNAME -ErrorAction SilentlyContinue
    } else {
        $env:CODELIB_ADMIN_USERNAME = $PreviousAdminUsername
    }
    if ($null -eq $PreviousAdminPassword) {
        Remove-Item Env:CODELIB_ADMIN_PASSWORD -ErrorAction SilentlyContinue
    } else {
        $env:CODELIB_ADMIN_PASSWORD = $PreviousAdminPassword
    }
}

exit $ExitCode
