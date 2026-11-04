<#
Install cua-direct-mcp: create .venv, install requirements, optionally register in Claude Code.
  .\install.ps1              # venv + dependencies only
  .\install.ps1 -Register    # also add/update cua_direct in ~/.claude.json (backup is kept)
  .\install.ps1 -Register -Raw   # register the bare proxy instead of the tools server
#>
param([switch]$Register, [switch]$Raw)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { python -m venv (Join-Path $root '.venv') }
& $py -m pip install --quiet -r (Join-Path $root 'requirements.txt')
$v = (Get-Content (Join-Path $root 'VERSION')).Trim()
Write-Host "cua-direct-mcp $v installed in $root"
if ($Register) {
  $extra = @(); if ($Raw) { $extra += '--raw' }
  & $py (Join-Path $root 'scripts\register_claude.py') @extra
  Write-Host 'Registered. Restart Claude Code to load the tools.'
} else {
  Write-Host 'Not registered. Run .\install.ps1 -Register, or see README.md.'
}
