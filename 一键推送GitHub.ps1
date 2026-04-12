# Push this repo to GitHub (needs Git + GitHub CLI, repo already has commits).
# Run from repo root:
#   powershell -ExecutionPolicy Bypass -File ".\一键推送GitHub.ps1"
#
# Steps: gh auth login (browser) -> gh repo create -> push main
# Note: User-visible messages are ASCII-only so Windows PowerShell 5.1 -File works without UTF-8 BOM issues.

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

$env:Path = "C:\Program Files\Git\bin;C:\Program Files\GitHub CLI;" + $env:Path

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Error "git not found. Install Git for Windows first."
}
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Error "gh not found. Run: winget install GitHub.cli"
}

$null = & git rev-parse --git-dir 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Error "Not a git repository. Run: git init"
}

Write-Host ""
Write-Host ">>> Step 1/3: GitHub login (browser will open)" -ForegroundColor Cyan
& gh auth login -h github.com -p https -w

Write-Host ""
Write-Host ">>> Step 2/3: New repository name [Enter = default]" -ForegroundColor Cyan
$defaultName = "bilibili-transcript-oneclick"
$repoName = Read-Host "Repo name [$defaultName]"
if ([string]::IsNullOrWhiteSpace($repoName)) { $repoName = $defaultName }

Write-Host ""
Write-Host ">>> Step 3/3: Create public repo and push branch main" -ForegroundColor Cyan
if (& git remote get-url origin 2>$null) {
    Write-Host "Remote origin exists; pushing only."
    & git push -u origin main
} else {
    & gh repo create $repoName --public --source . --remote origin --push
}

Write-Host ""
$login = ""
try { $login = (& gh api user --jq .login 2>$null) } catch { }
if ($login) {
    Write-Host "Done: https://github.com/$login/$repoName" -ForegroundColor Green
} else {
    Write-Host "Done. Open GitHub in the browser to see the new repository." -ForegroundColor Green
}
