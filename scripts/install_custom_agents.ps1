param(
    [string]$Destination = (Join-Path $HOME '.codex\agents')
)

$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot '..\assets\custom-agents'
$source = [System.IO.Path]::GetFullPath($source)
$destinationPath = [System.IO.Path]::GetFullPath($Destination)

if (-not (Test-Path -LiteralPath $source -PathType Container)) {
    throw "Custom agent source directory not found: $source"
}

New-Item -ItemType Directory -Path $destinationPath -Force | Out-Null

$installed = @()
Get-ChildItem -LiteralPath $source -Filter '*.toml' -File | ForEach-Object {
    $target = Join-Path $destinationPath $_.Name
    Copy-Item -LiteralPath $_.FullName -Destination $target -Force
    $installed += $target
}

if ($installed.Count -eq 0) {
    throw "No custom agent TOML files found in: $source"
}

Write-Output "Installed $($installed.Count) custom agents to $destinationPath"
$installed | ForEach-Object { Write-Output "- $_" }
Write-Output 'Restart Codex before invoking $multi-agent-data-analysis-skill.'
