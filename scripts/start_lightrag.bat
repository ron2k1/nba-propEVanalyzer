@echo off
REM Start LightRAG server with Ollama backend
REM Usage: start_lightrag.bat [port] [llm_model] [embedding_model]

set PORT=%1
if "%PORT%"=="" set PORT=9621

set LLM_MODEL=%2
if "%LLM_MODEL%"=="" set LLM_MODEL=gpt-oss:20b

set EMBEDDING_MODEL=%3
if "%EMBEDDING_MODEL%"=="" set EMBEDDING_MODEL=nomic-embed-text

powershell -ExecutionPolicy Bypass -File "%~dp0start_lightrag.ps1" -Port %PORT% -LlmModel "%LLM_MODEL%" -EmbeddingModel "%EMBEDDING_MODEL%"
