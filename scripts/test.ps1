$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
# Совместимый вход: все этапы и код возврата принадлежат общей команде.
& .\.venv\Scripts\python.exe -X utf8 -B scripts/check.py @args
exit $LASTEXITCODE
