[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateSet('Personal', 'Project')]
    [string]$Scope = 'Personal',
    [string]$ProjectPath = (Get-Location).Path,
    [string]$Destination,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$contractVersion = '1.1'
$requiredNames = @(
    'growth-business',
    'growth-metrics',
    'growth-sql',
    'growth-insight',
    'growth-visualization',
    'growth-review',
    'growth-report'
)

$source = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\assets\custom-agents'))
if (-not (Test-Path -LiteralPath $source -PathType Container)) {
    throw "Custom agent source directory not found: $source"
}

if ($Destination) {
    $destinationPath = [System.IO.Path]::GetFullPath($Destination)
} elseif ($Scope -eq 'Project') {
    $destinationPath = [System.IO.Path]::GetFullPath((Join-Path $ProjectPath '.codex\agents'))
} else {
    $codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
    $destinationPath = [System.IO.Path]::GetFullPath((Join-Path $codexHome 'agents'))
}

$sourceFiles = Get-ChildItem -LiteralPath $source -Filter '*.toml' -File | Sort-Object Name
$records = @()
foreach ($file in $sourceFiles) {
    $content = Get-Content -Raw -Encoding UTF8 -LiteralPath $file.FullName
    $nameMatch = [regex]::Match($content, '(?m)^\s*name\s*=\s*"([^"]+)"\s*$')
    if (-not $nameMatch.Success) {
        throw "Missing valid name field: $($file.FullName)"
    }
    $name = $nameMatch.Groups[1].Value
    if ($requiredNames -notcontains $name) {
        throw "Unexpected custom agent name '$name' in $($file.Name)"
    }
    foreach ($requiredField in @('description', 'developer_instructions', 'model_reasoning_effort', 'sandbox_mode')) {
        if ($content -notmatch "(?m)^\s*$requiredField\s*=") {
            throw "Missing $requiredField in $($file.Name)"
        }
    }
    if ($content -match '(?i)deepseek|model_provider|DEEPSEEK_API_KEY') {
        throw "External model provider configuration is not allowed in $($file.Name)"
    }
    if ($content -notmatch 'agent_contract_version.*1\.1') {
        throw "Agent contract version 1.1 is missing from $($file.Name)"
    }
    $effortMatch = [regex]::Match($content, '(?m)^\s*model_reasoning_effort\s*=\s*"([^"]+)"\s*$')
    $sandboxMatch = [regex]::Match($content, '(?m)^\s*sandbox_mode\s*=\s*"([^"]+)"\s*$')
    $modelMatch = [regex]::Match($content, '(?m)^\s*model\s*=\s*"([^"]+)"\s*$')
    $target = Join-Path $destinationPath $file.Name
    $sourceHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $file.FullName).Hash
    $targetHash = if (Test-Path -LiteralPath $target -PathType Leaf) {
        (Get-FileHash -Algorithm SHA256 -LiteralPath $target).Hash
    } else {
        $null
    }
    if ($targetHash -and $targetHash -ne $sourceHash -and -not $Force) {
        throw "Agent already exists with different content: $target. Re-run with -Force to replace it."
    }
    $records += [pscustomobject]@{
        name = $name
        file = $file.Name
        source_path = $file.FullName
        target_path = $target
        source_sha256 = $sourceHash
        previous_target_sha256 = $targetHash
        model = if ($modelMatch.Success) { $modelMatch.Groups[1].Value } else { $null }
        model_reasoning_effort = $effortMatch.Groups[1].Value
        sandbox_mode = $sandboxMatch.Groups[1].Value
        status = if ($targetHash -eq $sourceHash) { 'unchanged' } elseif ($targetHash) { 'replace' } else { 'install' }
    }
}

$foundNames = @($records | ForEach-Object { $_.name })
$missing = @($requiredNames | Where-Object { $foundNames -notcontains $_ })
if ($missing.Count -gt 0 -or $records.Count -ne $requiredNames.Count) {
    throw "Expected exactly seven agents. Missing: $($missing -join ', ')"
}

if ($PSCmdlet.ShouldProcess($destinationPath, "Install $($records.Count) Codex custom agents")) {
    New-Item -ItemType Directory -Path $destinationPath -Force | Out-Null
    foreach ($record in $records) {
        if ($record.status -ne 'unchanged') {
            Copy-Item -LiteralPath $record.source_path -Destination $record.target_path -Force
        }
        $record | Add-Member -NotePropertyName installed_sha256 -NotePropertyValue ((Get-FileHash -Algorithm SHA256 -LiteralPath $record.target_path).Hash)
    }
    $manifest = [ordered]@{
        schema_version = '1.1'
        agent_contract_version = $contractVersion
        installed_at = [DateTimeOffset]::UtcNow.ToString('o')
        scope = $Scope.ToLowerInvariant()
        source_root = $source
        destination = $destinationPath
        agents = $records
    }
    $manifestPath = Join-Path $destinationPath 'multi-agent-data-analysis-agents.manifest.json'
    $tempManifest = "$manifestPath.tmp"
    $manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $tempManifest -Encoding UTF8
    Move-Item -LiteralPath $tempManifest -Destination $manifestPath -Force

    Write-Output "Installed $($records.Count) Codex native agents to $destinationPath"
    $records | ForEach-Object { Write-Output "- $($_.name): $($_.installed_sha256)" }
    Write-Output "Manifest: $manifestPath"
    Write-Output 'Restart Codex before running the v1.1 native-agent smoke test.'
}
