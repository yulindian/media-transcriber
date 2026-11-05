param([string]$Runtime = (Join-Path $env:LOCALAPPDATA 'y0-media-transcriber'))
$ErrorActionPreference = 'Stop'
$skillRoot = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath "$Runtime\.venv\Scripts\python.exe")) {
    uv venv --python 3.12 "$Runtime\.venv"
    if ($LASTEXITCODE -ne 0) { throw 'Creating Python environment failed' }
}
uv pip install --python "$Runtime\.venv\Scripts\python.exe" 'sherpa-onnx==2.0.1' 'numpy>=2,<3' 'requests>=2.32' 'yt-dlp>=2026.10.26'
if ($LASTEXITCODE -ne 0) { throw 'Installing runtime dependencies failed' }
& "$Runtime\.venv\Scripts\python.exe" -X utf8 "$PSScriptRoot\setup_models.py" --runtime $Runtime
if ($LASTEXITCODE -ne 0) { throw 'Model setup failed' }
$cfgPath = Join-Path $skillRoot 'config.json'
if (Test-Path -LiteralPath $cfgPath) {
    $cfg = Get-Content -LiteralPath $cfgPath -Raw -Encoding utf8 | ConvertFrom-Json
} else {
    $cfg = Get-Content -LiteralPath (Join-Path $skillRoot 'config.example.json') -Raw -Encoding utf8 | ConvertFrom-Json
    $cfg.output = (Join-Path ([Environment]::GetFolderPath('MyDocuments')) '音视频转录').Replace('\','/')
}
$cfg.runtime = $Runtime.Replace('\','/')
$cfg | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $cfgPath -Encoding utf8
& "$Runtime\.venv\Scripts\python.exe" -X utf8 "$PSScriptRoot\transcribe.py" doctor
