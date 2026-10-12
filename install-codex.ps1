$ErrorActionPreference = 'Stop'
$scnetRoot = $PSScriptRoot
$scnetPython = Join-Path $scnetRoot '.venv/Scripts/python.exe'
$scnetConfig = Join-Path $scnetRoot 'config.toml'
if (-not (Test-Path -LiteralPath $scnetConfig)) {
    throw 'Copy config.example.toml to config.toml and configure your SSH alias first.'
}
if (-not (Test-Path -LiteralPath $scnetPython)) {
    throw 'Local runtime is missing. Run uv sync --locked in this directory first.'
}
& codex mcp get scnet-hpc --json 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    throw 'scnet-hpc is already registered. Review its existing configuration before replacing it.'
}
$scnetArguments = @('mcp', 'add', 'scnet-hpc')
foreach ($scnetName in @('USERDOMAIN', 'WINDIR', 'PROGRAMDATA', 'COMSPEC', 'TMP')) {
    $scnetValue = [Environment]::GetEnvironmentVariable($scnetName)
    if ($scnetValue) { $scnetArguments += @('--env', "$scnetName=$scnetValue") }
}
$scnetArguments += @('--', $scnetPython, '-X', 'utf8', '-m', 'scnet_mcp.server', '--config', $scnetConfig)
& codex @scnetArguments
if ($LASTEXITCODE -ne 0) { throw 'Codex MCP registration failed.' }
$scnetCodexRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$scnetCodexConfig = Join-Path $scnetCodexRoot 'config.toml'
& $scnetPython -X utf8 (Join-Path $scnetRoot 'scripts/tune_codex.py') $scnetCodexConfig $scnetPython
if ($LASTEXITCODE -ne 0) { throw 'MCP registered, but timeout configuration failed. Review codex-config.example.toml.' }
Write-Host 'scnet-hpc registered. Restart the MCP connection or open a new Codex chat.'
