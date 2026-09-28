# sub-hub Windows 一键启动（由仓库根 start.bat 调用，也可直接 powershell -File 运行）
#
# 【编码说明，改文件前必读】本文件必须以「UTF-8 with BOM」保存：
# Windows 自带的 PowerShell 5.1 依靠 BOM 识别 UTF-8，无 BOM 时中文会乱码。
param([string]$Mode = "")

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"        # Python 重定向到文件时也输出 UTF-8；控制台走 Win32 API 不受代码页影响
$env:PYTHONUNBUFFERED = "1"

# ---------- 启动参数（当前为实盘值，可按需修改） ----------
$env:SUBHUB_HOST     = "0.0.0.0"
$env:SUBHUB_PORT     = "18399"
$env:SUBHUB_NAS_HOST = "192.168.10.100"
# -----------------------------------------------------------

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Fail($msg) {
    Write-Host "[!!] $msg" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "===== sub-hub 一键启动 ====="
Write-Host ""

# ---------- 1) 虚拟环境：有则用，无则建 ----------
$py = Join-Path $root ".venv\Scripts\python.exe"
if (Test-Path $py) {
    Write-Host "[OK] 使用虚拟环境 .venv"
} else {
    Write-Host "[..] 未发现 .venv，尝试用系统 Python 创建（需要 3.12+）..."
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        Fail "未找到 python 命令：请先安装 Python 3.12+，安装时勾选「Add python.exe to PATH」。"
    }
    & python -m venv (Join-Path $root ".venv")
    if ($LASTEXITCODE -ne 0) { Fail "创建虚拟环境 .venv 失败。" }
    Write-Host "[OK] 已创建虚拟环境 .venv"
}

& $py -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 12) else 1)"
if ($LASTEXITCODE -ne 0) {
    Fail "Python 版本需 3.12+（当前 $(& $py --version)）。微软商店的 python 占位符也会导致此项失败。"
}

# ---------- 2) 依赖：能 import 就跳过，否则 pip install ----------
$needInstall = ($Mode -ieq "reinstall")
if (-not $needInstall) {
    & $py -c "import fastapi,uvicorn,jinja2,apscheduler,yaml,cryptography,httpx,qrcode" 2>$null
    $needInstall = ($LASTEXITCODE -ne 0)
}
if ($needInstall) {
    Write-Host "[..] 正在安装依赖（首次较慢）..."
    & $py -m pip install -r (Join-Path $root "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        Fail "依赖安装失败。请检查网络后重试，或手动执行：.venv\Scripts\python.exe -m pip install -r requirements.txt"
    }
    Write-Host "[OK] 依赖安装完成"
} else {
    Write-Host "[OK] 依赖已就绪"
}

# ---------- 3) mihomo（纯净度探测用）：环境变量 -> 仓库 .tools -> PATH -> 自动下载 ----------
$mihomo = $null
if (-not [string]::IsNullOrEmpty($env:SUBHUB_MIHOMO_PATH) -and (Test-Path $env:SUBHUB_MIHOMO_PATH)) {
    $mihomo = $env:SUBHUB_MIHOMO_PATH
} elseif (Test-Path (Join-Path $root ".tools\mihomo.exe")) {
    $mihomo = Join-Path $root ".tools\mihomo.exe"
} else {
    $cmd = Get-Command mihomo -ErrorAction SilentlyContinue
    if ($cmd) { $mihomo = $cmd.Source }
}
if (-not $mihomo) {
    # 自动下载：加速镜像依序尝试 -> GitHub 直连垫底（与 Dockerfile 同一策略）
    $mihomoVersion = if ($env:SUBHUB_MIHOMO_VERSION) { $env:SUBHUB_MIHOMO_VERSION } else { "v1.19.31" }
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "windows-arm64" } else { "windows-amd64" }
    $asset = "mihomo-$arch-$mihomoVersion.zip"
    $direct = "https://github.com/MetaCubeX/mihomo/releases/download/$mihomoVersion/$asset"
    $urls = @("https://ghfast.top/$direct", "https://gh-proxy.com/$direct", "https://ghproxy.net/$direct", $direct)
    $toolsDir = Join-Path $root ".tools"
    $zipPath = Join-Path $toolsDir $asset
    $tmpDir = Join-Path $toolsDir "_mihomo_extract"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null
        foreach ($u in $urls) {
            Write-Host "[..] mihomo 未找到，正在下载：$u"
            $ok = $false
            try {
                if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
                    & curl.exe -sS -L -m 300 -o $zipPath $u
                    $ok = ($LASTEXITCODE -eq 0)
                    if (-not $ok) { Write-Host "     curl 失败（exit $LASTEXITCODE），换下一个源" }
                } else {
                    Invoke-WebRequest -UseBasicParsing -Uri $u -OutFile $zipPath -TimeoutSec 300
                    $ok = $true
                }
            } catch {
                Write-Host "     下载异常：$($_.Exception.Message)"
            }
            if ($ok -and (Test-Path $zipPath)) {
                try {
                    $zipSize = (Get-Item $zipPath).Length
                    if ($zipSize -lt 1MB) { throw "文件仅 $zipSize 字节，疑似错误页而非安装包" }
                    Expand-Archive -Path $zipPath -DestinationPath $tmpDir -Force
                    $exe = Get-ChildItem $tmpDir -Recurse -Filter "mihomo*.exe" | Select-Object -First 1
                    if ($exe) {
                        $mihomo = Join-Path $toolsDir "mihomo.exe"
                        Move-Item -Force $exe.FullName $mihomo
                    } else {
                        Write-Host "     解压后未找到 mihomo.exe，换下一个源"
                    }
                } catch {
                    Write-Host "     解压失败：$($_.Exception.Message)"
                }
            }
            if ($mihomo) { break }
        }
    } finally {
        if (Test-Path $tmpDir) { Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue }
        if (Test-Path $zipPath) { Remove-Item -Force $zipPath -ErrorAction SilentlyContinue }
    }
    if ($mihomo) { Write-Host "[OK] 已自动下载 mihomo：$mihomo" }
}
if ($mihomo) {
    $env:SUBHUB_MIHOMO_PATH = $mihomo
    $ver = try { (& $mihomo -v | Select-Object -First 1) } catch { "" }
    Write-Host "[OK] 纯净度扫描就绪：$mihomo $ver"
} else {
    Write-Host "[i] mihomo 自动准备失败：纯净度扫描将标记「不可用」，其余功能不受影响；"
    Write-Host "    可手动下载后设置环境变量 SUBHUB_MIHOMO_PATH 指向二进制再试。"
}

# ---------- 4) 防双实例：端口已被监听则拒绝启动 ----------
$conn = Get-NetTCPConnection -LocalPort $env:SUBHUB_PORT -State Listen -ErrorAction SilentlyContinue
if ($conn) {
    Write-Host "[!!] 端口 $($env:SUBHUB_PORT) 已有进程在监听，疑似 sub-hub 已在运行（勿起双实例）：" -ForegroundColor Yellow
    foreach ($c in $conn) {
        Write-Host ("     监听 {0}  PID {1}" -f $c.LocalAddress, $c.OwningProcess)
    }
    Write-Host "    确认后可先结束旧实例（Stop-Process -Id 上面的PID -Force）再运行本脚本。"
    exit 1
}

# ---------- 5) 启动 ----------
Write-Host ""
Write-Host "[启动] 数据目录 $(Join-Path $root 'data')"
Write-Host "[启动] 监听 $($env:SUBHUB_HOST):$($env:SUBHUB_PORT)，局域网基址 http://$($env:SUBHUB_NAS_HOST):$($env:SUBHUB_PORT)/"
Write-Host "[启动] 管理页 http://127.0.0.1:$($env:SUBHUB_PORT)/  按 Ctrl+C 停止服务"
Write-Host ""
& $py -m app.main
Write-Host ""
Write-Host "[i] sub-hub 已停止。"
