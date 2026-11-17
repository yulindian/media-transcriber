param([string]$Runtime, [string]$Output, [string]$Python, [switch]$Plan)
$ErrorActionPreference = 'Stop'
$setupArgs = @((Join-Path $PSScriptRoot 'setup.py'))
if ($Runtime) { $setupArgs += @('--runtime', $Runtime) }
if ($Output) { $setupArgs += @('--output', $Output) }
if ($Plan) { $setupArgs += '--plan' }
if ($Python) { & $Python -X utf8 @setupArgs }
elseif (Get-Command python -ErrorAction SilentlyContinue) { & python -X utf8 @setupArgs }
elseif (Get-Command uv -ErrorAction SilentlyContinue) { & uv run --no-project --python 3.12 @setupArgs }
else { throw 'Python or uv is required to prepare this skill. See references/installation.md.' }
if ($LASTEXITCODE -ne 0) { throw 'Skill setup failed; see the returned error.' }
