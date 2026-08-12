[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateSet('Personal', 'Project')]
    [string]$Scope = 'Personal',
    [string]$ProjectPath = (Get-Location).Path,
    [string]$Destination,
    [switch]$Force,
    [switch]$DryRun,
    [switch]$Uninstall,
    [switch]$Purge,
    [switch]$Json,
    [string]$StagingRoot,
    [string]$PythonExecutable = 'python'
)

$ErrorActionPreference = 'Stop'
$installer = Join-Path $PSScriptRoot 'install_skill.py'
if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
    throw "Cross-platform installer not found: $installer"
}
if ($Purge -and -not $Uninstall) {
    throw '-Purge is valid only with -Uninstall.'
}

$isDryRun = $DryRun -or [bool]$WhatIfPreference
$command = if ($Uninstall) { 'uninstall-agents' } else { 'install-agents' }
$operation = if ($Uninstall) { 'Uninstall multi-agent data analysis custom agents' } else { 'Install multi-agent data analysis custom agents' }
$targetDescription = if ($Destination) { $Destination } elseif ($Scope -eq 'Project') { $ProjectPath } else { 'personal Codex agents directory' }
if (-not $isDryRun -and -not $PSCmdlet.ShouldProcess($targetDescription, $operation)) {
    return
}

$cliArgs = @(
    $installer,
    $command,
    '--scope', $Scope.ToLowerInvariant(),
    '--project-path', $ProjectPath
)
if ($Destination) { $cliArgs += @('--destination', $Destination) }
if ($Force) { $cliArgs += '--force' }
if ($isDryRun) { $cliArgs += '--dry-run' }
if ($Purge) { $cliArgs += '--purge' }
if ($StagingRoot) { $cliArgs += @('--staging-root', $StagingRoot) }
if ($Json) { $cliArgs += '--json' }

& $PythonExecutable @cliArgs
if ($LASTEXITCODE -ne 0) {
    throw "Custom-agent installer failed with exit code $LASTEXITCODE. Review the error above; use -Force only for intentional replacement."
}
if (-not $Json -and -not $isDryRun -and -not $Uninstall) {
    Write-Output 'Restart Codex before running the native-agent smoke test.'
}
