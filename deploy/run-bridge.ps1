param(
    [string]$UIrepo = (Join-Path $PSScriptRoot "MPP_Project"),
    [string]$DataDir = (Join-Path $PSScriptRoot "data"),
    [int]$Port = 8791,
    [string]$EnvFile = (Join-Path $PSScriptRoot ".env")
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

if (-not (Test-Path (Join-Path $UIrepo "web\index.html"))) {
    throw "MPP_Project UI not found at $UIrepo. Clone it first or pass -UIrepo."
}
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

if (Test-Path $EnvFile) {
    Get-Content $EnvFile -Encoding utf8 | ForEach-Object {
        if ($_ -match "^\s*#" -or $_ -match "^\s*$") { return }
        if ($_ -match "^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$") {
            $name = $Matches[1]
            $value = $Matches[2].Trim()
            if ($value -match "^`".*`"$" -or $value -match "^'.*'$") {
                $value = $value.Substring(1, $value.Length - 2)
            }
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
}

if (-not $env:MPPKB_KB_DB) { $env:MPPKB_KB_DB = Join-Path $DataDir "kb.sqlite" }
if (-not $env:MPPKB_CORPUS_DB) { $env:MPPKB_CORPUS_DB = Join-Path $DataDir "corpus.sqlite" }
if (-not $env:MPPKB_CORPUS_DIR) { $env:MPPKB_CORPUS_DIR = Join-Path $DataDir "pdfs" }
if (-not $env:MPPKB_CHROMA_DIR) { $env:MPPKB_CHROMA_DIR = Join-Path $DataDir "chroma" }
if (-not $env:MPPKB_ONTOLOGY) { $env:MPPKB_ONTOLOGY = Join-Path $Root "ontology.yaml" }
if (-not $env:MPPKB_RETRIEVAL_MODE) { $env:MPPKB_RETRIEVAL_MODE = "lexical" }
if (-not $env:MPPKB_REVIEWER) { $env:MPPKB_REVIEWER = "clinical-reviewer" }
$env:PYTHONIOENCODING = "utf-8"

New-Item -ItemType Directory -Force -Path $env:MPPKB_CORPUS_DIR | Out-Null

Write-Host "mppkb UI bridge"
Write-Host "  repo:   $UIrepo"
Write-Host "  data:   $DataDir"
Write-Host "  KB:     $env:MPPKB_KB_DB"
Write-Host "  URL:    http://127.0.0.1:$Port/"
Write-Host ""

Push-Location $Root
try {
    python -m mppkb.ui_bridge --repo $UIrepo --host 127.0.0.1 --port $Port
} finally {
    Pop-Location
}
