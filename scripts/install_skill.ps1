[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateSet('Personal', 'Project')]
    [string]$Scope = 'Personal',
    [string]$ProjectPath = (Get-Location).Path,
    [string]$Destination,
    [switch]$Force,
    [switch]$InstallDependencies,
    [string]$PythonExecutable = 'python'
)

$ErrorActionPreference = 'Stop'
$skillName = 'multi-agent-data-analysis-skill'
$sourceRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$sourcePrefix = $sourceRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar

function Get-SkillRelativePath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    if (-not $fullPath.StartsWith($sourcePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Skill package file is outside the source root: $fullPath"
    }
    return $fullPath.Substring($sourcePrefix.Length).Replace('\', '/')
}

if ($Destination) {
    $destinationPath = [System.IO.Path]::GetFullPath($Destination)
} elseif ($Scope -eq 'Project') {
    $destinationPath = [System.IO.Path]::GetFullPath((Join-Path $ProjectPath ".agents\skills\$skillName"))
} else {
    $destinationPath = [System.IO.Path]::GetFullPath((Join-Path $HOME ".agents\skills\$skillName"))
}

$includeFiles = @('SKILL.md', 'README.md', 'requirements.txt')
$includeDirectories = @('agents', 'assets', 'references', 'schemas', 'scripts', 'skill-test-harness', 'tests\evals')
$sourceFiles = @()
foreach ($relative in $includeFiles) {
    $path = Join-Path $sourceRoot $relative
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required skill file is missing: $path"
    }
    $sourceFiles += Get-Item -LiteralPath $path
}
foreach ($relative in $includeDirectories) {
    $path = Join-Path $sourceRoot $relative
    if (-not (Test-Path -LiteralPath $path -PathType Container)) {
        throw "Required skill directory is missing: $path"
    }
    $sourceFiles += Get-ChildItem -LiteralPath $path -Recurse -File | Where-Object {
        $_.FullName -notmatch '[\\/]__pycache__[\\/]' -and $_.Extension -ne '.pyc'
    }
}

$records = @($sourceFiles | ForEach-Object {
    $relative = Get-SkillRelativePath -Path $_.FullName
    [pscustomobject]@{
        path = $relative
        sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash
        source_path = $_.FullName
    }
} | Sort-Object path)

$hashInput = ($records | ForEach-Object { "$($_.path):$($_.sha256)" }) -join "`n"
$hashBytes = [System.Text.Encoding]::UTF8.GetBytes($hashInput)
$hasher = [System.Security.Cryptography.SHA256]::Create()
try {
    $packageHash = ([System.BitConverter]::ToString($hasher.ComputeHash($hashBytes))).Replace('-', '')
} finally {
    $hasher.Dispose()
}

$manifestName = 'multi-agent-data-analysis-skill.manifest.json'
$existingManifestPath = Join-Path $destinationPath $manifestName
$existingHash = $null
if (Test-Path -LiteralPath $existingManifestPath -PathType Leaf) {
    try {
        $existingHash = (Get-Content -Raw -Encoding UTF8 -LiteralPath $existingManifestPath | ConvertFrom-Json).package_sha256
    } catch {
        $existingHash = $null
    }
}
$installedMatches = $existingHash -eq $packageHash
if ($installedMatches) {
    foreach ($record in $records) {
        $installedFile = Join-Path $destinationPath $record.path
        if (-not (Test-Path -LiteralPath $installedFile -PathType Leaf) -or
            (Get-FileHash -Algorithm SHA256 -LiteralPath $installedFile).Hash -ne $record.sha256) {
            $installedMatches = $false
            break
        }
    }
}
if ((Test-Path -LiteralPath $destinationPath) -and -not $installedMatches -and -not $Force) {
    throw "Installed skill differs from source: $destinationPath. Re-run with -Force to update it."
}

if (-not $installedMatches -and $PSCmdlet.ShouldProcess($destinationPath, "Install Codex skill $skillName")) {
    $parent = Split-Path -Parent $destinationPath
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $stagingPath = Join-Path $parent (".$skillName." + [guid]::NewGuid().ToString('N') + '.tmp')
    New-Item -ItemType Directory -Path $stagingPath | Out-Null
    foreach ($record in $records) {
        $target = Join-Path $stagingPath $record.path
        New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
        Copy-Item -LiteralPath $record.source_path -Destination $target -Force
    }
    $manifest = [ordered]@{
        schema_version = '1.1'
        skill_name = $skillName
        installed_at = [DateTimeOffset]::UtcNow.ToString('o')
        scope = $Scope.ToLowerInvariant()
        source_root = $sourceRoot
        destination = $destinationPath
        package_sha256 = $packageHash
        files = @($records | ForEach-Object { [ordered]@{ path = $_.path; sha256 = $_.sha256 } })
    }
    $manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $stagingPath $manifestName) -Encoding UTF8

    $backupPath = $null
    if (Test-Path -LiteralPath $destinationPath) {
        $backupPath = Join-Path $parent (".$skillName." + [guid]::NewGuid().ToString('N') + '.backup')
        Move-Item -LiteralPath $destinationPath -Destination $backupPath
    }
    try {
        Move-Item -LiteralPath $stagingPath -Destination $destinationPath
        if ($backupPath) {
            $oldVenv = Join-Path $backupPath '.venv'
            $newVenv = Join-Path $destinationPath '.venv'
            if ((Test-Path -LiteralPath $oldVenv -PathType Container) -and -not (Test-Path -LiteralPath $newVenv)) {
                Move-Item -LiteralPath $oldVenv -Destination $newVenv
            }
        }
    } catch {
        if ($backupPath -and -not (Test-Path -LiteralPath $destinationPath) -and (Test-Path -LiteralPath $backupPath)) {
            Move-Item -LiteralPath $backupPath -Destination $destinationPath
        }
        throw
    }
    if ($backupPath -and (Test-Path -LiteralPath $backupPath)) {
        $resolvedBackup = [System.IO.Path]::GetFullPath($backupPath)
        if ((Split-Path -Parent $resolvedBackup) -ne [System.IO.Path]::GetFullPath($parent)) {
            throw "Refusing to remove backup outside the expected parent: $resolvedBackup"
        }
        Remove-Item -LiteralPath $resolvedBackup -Recurse -Force
    }
}

if ($InstallDependencies -and $PSCmdlet.ShouldProcess((Join-Path $destinationPath '.venv'), 'Create isolated Python environment and install runtime dependencies')) {
    $venvPath = Join-Path $destinationPath '.venv'
    if (-not (Test-Path -LiteralPath $venvPath -PathType Container)) {
        & $PythonExecutable -m venv $venvPath
        if ($LASTEXITCODE -ne 0) { throw "Unable to create virtual environment with $PythonExecutable" }
    }
    $venvPython = Join-Path $venvPath 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        $venvPython = Join-Path $venvPath 'bin/python'
    }
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        throw "Installed skill virtual environment has no Python executable: $venvPath"
    }
    & $venvPython -m pip install -r (Join-Path $destinationPath 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Unable to install skill runtime dependencies.' }
}

Write-Output "Skill package: $packageHash"
Write-Output "Installed at: $destinationPath"
Write-Output "Files: $($records.Count)"
if (Test-Path -LiteralPath (Join-Path $destinationPath '.venv')) {
    Write-Output "Runtime environment: $(Join-Path $destinationPath '.venv')"
} else {
    Write-Output 'Runtime environment: not installed (use -InstallDependencies)'
}
Write-Output 'Restart Codex after installing or updating the skill.'
