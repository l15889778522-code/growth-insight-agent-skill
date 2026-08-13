[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateSet('Personal', 'Project')]
    [string]$Scope = 'Personal',
    [string]$ProjectPath = (Get-Location).Path,
    [string]$Destination,
    [switch]$Force,
    [switch]$InstallDependencies,
    [switch]$DryRun,
    [switch]$Uninstall,
    [switch]$Purge,
    [switch]$KeepEnvironment,
    [switch]$Json,
    [string]$StagingRoot,
    [string]$PythonExecutable = 'python'
)

$ErrorActionPreference = 'Stop'
$installer = Join-Path $PSScriptRoot 'install_skill.py'
if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
    throw "Cross-platform installer not found: $installer"
}
if ($InstallDependencies -and $Uninstall) {
    throw '-InstallDependencies cannot be combined with -Uninstall.'
}
if ($Purge -and -not $Uninstall) {
    throw '-Purge is valid only with -Uninstall.'
}
if ($KeepEnvironment -and -not $Uninstall) {
    throw '-KeepEnvironment is valid only with -Uninstall.'
}

$isDryRun = $DryRun -or [bool]$WhatIfPreference
$command = if ($Uninstall) { 'uninstall' } else { 'install' }
$operation = if ($Uninstall) { 'Uninstall multi-agent data analysis Skill' } else { 'Install multi-agent data analysis Skill' }
$targetDescription = if ($Destination) { $Destination } elseif ($Scope -eq 'Project') { $ProjectPath } else { 'personal Skill directory' }
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
if ($InstallDependencies) {
    $cliArgs += @('--install-dependencies', '--python-executable', $PythonExecutable)
}
if ($Purge) { $cliArgs += '--purge' }
if ($KeepEnvironment) { $cliArgs += '--keep-environment' }
if ($StagingRoot) { $cliArgs += @('--staging-root', $StagingRoot) }
if ($Json) { $cliArgs += '--json' }

& $PythonExecutable @cliArgs
if ($LASTEXITCODE -ne 0) {
    throw "Skill installer failed with exit code $LASTEXITCODE. Review the error above; use -Force only for intentional replacement."
}
if (-not $Json -and -not $isDryRun -and -not $Uninstall) {
    Write-Output 'Restart Codex after installing or updating the Skill.'
}
