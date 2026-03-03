<#
.SYNOPSIS
    Start the LightRAG server with Ollama backend.
.DESCRIPTION
    Launches LightRAG on port 9621 using Ollama for LLM + nomic-embed-text
    for embeddings. Data stored in data/lightrag_storage/.
#>
param(
    [int]$Port = 9621,
    [string]$LlmModel = "gpt-oss:20b",
    [string]$EmbeddingModel = "nomic-embed-text"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not $RepoRoot) { $RepoRoot = (Get-Location).Path }

$StorageDir = Join-Path (Join-Path $RepoRoot "data") "lightrag_storage"
if (-not (Test-Path $StorageDir)) {
    New-Item -ItemType Directory -Path $StorageDir -Force | Out-Null
    Write-Host "[lightrag] Created storage dir: $StorageDir"
}

# Check Ollama is running
try {
    $null = Invoke-RestMethod -Uri "http://localhost:11434/api/tags" -TimeoutSec 3
    Write-Host "[lightrag] Ollama is running"
} catch {
    Write-Error "[lightrag] Ollama not reachable at localhost:11434. Start it first."
    exit 1
}

# Check embedding model is pulled
$models = (Invoke-RestMethod -Uri "http://localhost:11434/api/tags").models
$hasEmbed = $models | Where-Object { $_.name -like "$EmbeddingModel*" }
if (-not $hasEmbed) {
    Write-Host "[lightrag] Pulling $EmbeddingModel ..."
    & ollama pull $EmbeddingModel
    if ($LASTEXITCODE -ne 0) {
        Write-Error "[lightrag] Failed to pull $EmbeddingModel"
        exit 1
    }
}

# Set environment variables for LightRAG
$env:LLM_BINDING = "ollama"
$env:LLM_BINDING_HOST = "http://localhost:11434"
$env:LLM_MODEL = $LlmModel
$env:EMBEDDING_BINDING = "ollama"
$env:EMBEDDING_BINDING_HOST = "http://localhost:11434"
$env:EMBEDDING_MODEL = $EmbeddingModel
$env:LIGHTRAG_WORKING_DIR = $StorageDir
$env:PORT = $Port

Write-Host "[lightrag] Starting LightRAG on port $Port ..."
Write-Host "[lightrag]   LLM: $LlmModel | Embeddings: $EmbeddingModel"
Write-Host "[lightrag]   Storage: $StorageDir"
Write-Host ""

& "$RepoRoot\.venv\Scripts\python.exe" -m lightrag.api.lightrag_server `
    --host 0.0.0.0 `
    --port $Port `
    --working-dir $StorageDir `
    --llm-binding ollama `
    --embedding-binding ollama
