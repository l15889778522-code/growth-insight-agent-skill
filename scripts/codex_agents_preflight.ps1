[CmdletBinding()]
param(
    [ValidateSet('Personal', 'Project')]
    [string]$Scope = 'Personal',
    [string]$ProjectPath = (Get-Location).Path,
    [string]$Destination,
    [switch]$Json
)

$ErrorActionPreference = 'Stop'

function Get-Sha256Hex {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LiteralPath
    )

    $stream = [System.IO.File]::OpenRead($LiteralPath)
    try {
        $sha256 = [System.Security.Cryptography.SHA256]::Create()
        try {
            return ([System.BitConverter]::ToString($sha256.ComputeHash($stream))).Replace('-', '')
        }
        finally {
            $sha256.Dispose()
        }
    }
    finally {
        $stream.Dispose()
    }
}
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
if ($Destination) {
    $destinationPath = [System.IO.Path]::GetFullPath($Destination)
} elseif ($Scope -eq 'Project') {
    $destinationPath = [System.IO.Path]::GetFullPath((Join-Path $ProjectPath '.codex\agents'))
} else {
    $codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
    $destinationPath = [System.IO.Path]::GetFullPath((Join-Path $codexHome 'agents'))
}

$checks = @()
foreach ($name in $requiredNames) {
    $fileName = "$name.toml"
    $sourcePath = Join-Path $source $fileName
    $targetPath = Join-Path $destinationPath $fileName
    $messages = @()
    $ok = $true
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        $ok = $false
        $messages += 'source_missing'
    }
    if (-not (Test-Path -LiteralPath $targetPath -PathType Leaf)) {
        $ok = $false
        $messages += 'target_missing'
    }
    $sourceHash = if (Test-Path -LiteralPath $sourcePath -PathType Leaf) { Get-Sha256Hex -LiteralPath $sourcePath } else { $null }
    $targetHash = if (Test-Path -LiteralPath $targetPath -PathType Leaf) { Get-Sha256Hex -LiteralPath $targetPath } else { $null }
    if ($sourceHash -and $targetHash -and $sourceHash -ne $targetHash) {
        $ok = $false
        $messages += 'hash_mismatch'
    }
    $model = $null
    $effort = $null
    $sandbox = $null
    if ($sourceHash) {
        $content = Get-Content -Raw -Encoding UTF8 -LiteralPath $sourcePath
        foreach ($requiredField in @('name', 'description', 'developer_instructions', 'model_reasoning_effort', 'sandbox_mode')) {
            if ($content -notmatch "(?m)^\s*$requiredField\s*=") {
                $ok = $false
                $messages += "missing_$requiredField"
            }
        }
        if ($content -match '(?i)deepseek|model_provider|DEEPSEEK_API_KEY') {
            $ok = $false
            $messages += 'external_provider_found'
        }
        if ($content -notmatch 'sandbox_mode\s*=\s*"read-only"') {
            $ok = $false
            $messages += 'sandbox_not_read_only'
        }
        if ($content -notmatch 'agent_contract_version.*1\.2') {
            $ok = $false
            $messages += 'contract_version_missing'
        }
        $modelMatch = [regex]::Match($content, '(?m)^\s*model\s*=\s*"([^"]+)"\s*$')
        $effortMatch = [regex]::Match($content, '(?m)^\s*model_reasoning_effort\s*=\s*"([^"]+)"\s*$')
        $sandboxMatch = [regex]::Match($content, '(?m)^\s*sandbox_mode\s*=\s*"([^"]+)"\s*$')
        $model = if ($modelMatch.Success) { $modelMatch.Groups[1].Value } else { $null }
        $effort = if ($effortMatch.Success) { $effortMatch.Groups[1].Value } else { $null }
        $sandbox = if ($sandboxMatch.Success) { $sandboxMatch.Groups[1].Value } else { $null }
    }
    $checks += [pscustomobject]@{
        name = $name
        ok = $ok
        source_sha256 = $sourceHash
        installed_sha256 = $targetHash
        model = $model
        model_resolution = if ($model) { 'explicit' } else { 'inherited_native_portable' }
        model_reasoning_effort = $effort
        sandbox_mode = $sandbox
        tool_policy = 'inherits_host_tool_availability; TOML enforces read-only sandbox only'
        messages = $messages
    }
}

$manifestPath = Join-Path $destinationPath 'multi-agent-data-analysis-agents.manifest.json'
$result = [ordered]@{
    schema_version = '1.2'
    checked_at = [DateTimeOffset]::UtcNow.ToString('o')
    scope = $Scope.ToLowerInvariant()
    destination = $destinationPath
    manifest_present = (Test-Path -LiteralPath $manifestPath -PathType Leaf)
    static_checks_passed = (@($checks | Where-Object { -not $_.ok }).Count -eq 0)
    runtime_spawn_required = $true
    restart_required_after_install = $true
    agents = $checks
}

if ($Json) {
    $result | ConvertTo-Json -Depth 8
} else {
    Write-Output "Codex native agent preflight: $($result.static_checks_passed)"
    Write-Output "Destination: $destinationPath"
    foreach ($check in $checks) {
        $status = if ($check.ok) { 'PASS' } else { 'FAIL' }
        Write-Output "- $($check.name): $status $($check.messages -join ',')"
    }
    Write-Output 'A visible spawn is still required after Codex reload to prove runtime availability.'
}

if (-not $result.static_checks_passed) {
    exit 2
}
