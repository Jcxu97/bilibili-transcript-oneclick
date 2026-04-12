# 首次把本仓库推到你的 GitHub（需已安装 Git、GitHub CLI，且本目录已 git init + commit）
# 用法（在仓库根目录）:
#   powershell -ExecutionPolicy Bypass -File ".\一键推送GitHub.ps1"
#
# 会执行: gh auth login（浏览器登录）→ gh repo create（新建公开仓库并 push）

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

$env:Path = "C:\Program Files\Git\bin;C:\Program Files\GitHub CLI;" + $env:Path

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Error "未找到 git。请先安装 Git for Windows。"
}
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Error "未找到 gh。请执行: winget install GitHub.cli"
}

$null = & git rev-parse --git-dir 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Error "当前目录不是 Git 仓库。请先执行: git init"
}

Write-Host ""
Write-Host ">>> 步骤 1/3：登录 GitHub（将打开浏览器，按提示授权）" -ForegroundColor Cyan
& gh auth login -h github.com -p https -w

Write-Host ""
Write-Host ">>> 步骤 2/3：填写新仓库名称（直接回车则用默认名）" -ForegroundColor Cyan
$defaultName = "bilibili-transcript-oneclick"
$repoName = Read-Host "仓库名 [$defaultName]"
if ([string]::IsNullOrWhiteSpace($repoName)) { $repoName = $defaultName }

Write-Host ""
Write-Host ">>> 步骤 3/3：创建公开仓库并推送 main 分支…" -ForegroundColor Cyan
if (& git remote get-url origin 2>$null) {
    Write-Host "已存在 remote origin，跳过 repo create，改为 push。"
    & git push -u origin main
} else {
    & gh repo create $repoName --public --source . --remote origin --push
}

Write-Host ""
$login = ""
try { $login = (& gh api user --jq .login 2>$null) } catch { }
if ($login) {
    Write-Host "完成: https://github.com/$login/$repoName" -ForegroundColor Green
} else {
    Write-Host "完成。请在 GitHub 网页上查看新建的仓库。" -ForegroundColor Green
}
