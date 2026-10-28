param(
    [string]$UIrepo = "",
    [int]$Port = 8793
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Data = Join-Path $PSScriptRoot "data"

if (-not (Test-Path (Join-Path $Data "kb.sqlite"))) {
    throw "演示数据不存在。请先在仓库根目录运行: python tools\seed_demo.py"
}

if (-not $UIrepo) {
    $UIrepo = Join-Path $env:TEMP "MPP_Project_inspect"
}
if (-not (Test-Path (Join-Path $UIrepo "web\index.html"))) {
    throw "找不到 MPP_Project 前端。请克隆后传 -UIrepo，例如: .\demo\start_demo.ps1 -UIrepo ..\MPP_Project"
}

$env:MPPKB_KB_DB = Join-Path $Data "kb.sqlite"
$env:MPPKB_CORPUS_DB = Join-Path $Data "corpus.sqlite"
$env:MPPKB_CORPUS_DIR = Join-Path $Data "pdfs"
$env:MPPKB_ONTOLOGY = Join-Path $Root "ontology.yaml"
$env:MPPKB_RETRIEVAL_MODE = "lexical"
$env:MPPKB_ORCHESTRATION_V2 = "1"
$env:MPPKB_DEMO_MODE = "1"
$env:PYTHONIOENCODING = "utf-8"

Write-Host "测试版 · 合成演示数据 · 不可用于临床"
Write-Host "  data: $Data"
Write-Host "  URL : http://127.0.0.1:$Port/"
Write-Host ""
Set-Location $Root
python -m mppkb.ui_bridge --repo $UIrepo --host 127.0.0.1 --port $Port
