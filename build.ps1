<#
.SYNOPSIS
    yys_auto 打包脚本

.DESCRIPTION
    按固定规则打包：输出到  ../yys_auto_app/yys_auto_v<版本号>/
    打包完成后自动补齐运行时需要的 config.example.json / templates / README.md / logs
    （不复制本机 config.json，用户首次运行会自动生成）

    目录布局：
        Mycodes/
        ├── yys_auto/                 源码
        └── yys_auto_app/             发布目录
            ├── yys_auto_v1.0/        历史版本
            └── yys_auto_v1.1/        本次打包产物

.EXAMPLE
    .\build.ps1 -Version 1.1

.NOTES
    版本号只写数字，如 1.1（不要写成 v1.1）
#>
param(
    [Parameter(Mandatory = $true, HelpMessage = "版本号，如 1.1")]
    [string]$Version
)

$ErrorActionPreference = 'Stop'

$root    = $PSScriptRoot
$outBase = Join-Path (Split-Path $root -Parent) 'yys_auto_app'
$target  = Join-Path $outBase "yys_auto_v$Version"

if (Test-Path $target) {
    throw "目标目录已存在，请先删除或更换版本号: $target"
}

Write-Host "==== 打包 yys_auto v$Version ====" -ForegroundColor Cyan

# ---- 1) 调用 PyInstaller（版本号经环境变量传给 spec）----
$env:YYS_APP_VERSION = $Version
Push-Location $root
try {
    # PyInstaller 的日志走 stderr，在 Stop 策略下会被误判为错误，这里临时放宽
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        pyinstaller yys_auto.spec --distpath $outBase --workpath build --noconfirm 2>&1 |
            ForEach-Object { Write-Host $_ }
        $pyiExit = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $prevEAP
    }
    if ($pyiExit -ne 0) { throw "PyInstaller 执行失败，退出码 $pyiExit" }
}
finally {
    Pop-Location
    Remove-Item Env:\YYS_APP_VERSION -ErrorAction SilentlyContinue
}

# ---- 2) 清理 PyInstaller 留在发布根目录的中间 exe ----
$strayExe = Join-Path $outBase 'yys_auto.exe'
if (Test-Path $strayExe) { Remove-Item $strayExe -Force }

# ---- 3) 补齐运行时资源 ----
# 只放样例配置：本机 config.json 含窗口名等信息，不打进发布包，
# 用户首次运行时程序会自动从 config.example.json 生成一份
Copy-Item (Join-Path $root 'config.example.json') $target -Force
Copy-Item (Join-Path $root 'README.md')   $target -Force
Copy-Item (Join-Path $root 'templates')   $target -Recurse -Force
New-Item -ItemType Directory -Path (Join-Path $target 'logs') -Force | Out-Null

Write-Host ""
Write-Host "打包完成 -> $target" -ForegroundColor Green
Get-ChildItem $target | Select-Object Name | Format-Table -AutoSize
