[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Context,
    [Parameter(Mandatory)][string]$Namespace,
    [Parameter(Mandatory)][string]$ConfigPath,
    [Parameter(Mandatory)][string]$PayloadPath,
    [Parameter(Mandatory)][string]$Image,
    [Parameter(Mandatory)][string]$OutputDirectory,
    [switch]$Apply,
    [switch]$Cleanup
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$pythonPath = Join-Path $repoRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { $pythonPath = (Get-Command python).Source }
$configFile = (Resolve-Path -LiteralPath $ConfigPath).Path
$payloadFile = (Resolve-Path -LiteralPath $PayloadPath).Path
$config = Get-Content -Raw -LiteralPath $configFile | ConvertFrom-Json
if ($config.namespace -ne $Namespace) { throw 'Namespace differs from runner config' }
$outputPath = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $outputPath | Out-Null
$manifestPath = Join-Path $outputPath 'job.json'
$created = [Collections.Generic.List[string]]::new()
$exitCode = 2
Push-Location $repoRoot
try {
    & $pythonPath -m scripts.e2e_v2.render --config $configFile --payload $payloadFile --image $Image --output $manifestPath
    if ($LASTEXITCODE -ne 0) { throw 'Render failed' }
    if (-not $Apply) { Write-Output "Rendered only: $manifestPath"; return }
    & kubectl --context $Context get namespace $Namespace -o name
    if ($LASTEXITCODE -ne 0) { throw 'Context/namespace verification failed' }
    $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
    $job = $manifest.items | Where-Object kind -eq 'Job'
    $name = $job.metadata.name
    $identity = $job.metadata.annotations.'ml-template.io/e2e-identity'
    foreach ($item in $manifest.items) {
        $existing = & kubectl --context $Context -n $Namespace get $item.kind $name --ignore-not-found -o json
        if ($LASTEXITCODE -ne 0) { throw 'Resource lookup failed' }
        if ($existing) {
            $object = $existing | ConvertFrom-Json
            if ($object.metadata.annotations.'ml-template.io/e2e-identity' -ne $identity) { throw 'Existing run identity conflicts; choose a new run ID' }
        } else {
            $itemPath = Join-Path $outputPath ($item.kind + '.json')
            $item | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $itemPath -Encoding utf8
            & kubectl --context $Context -n $Namespace create -f $itemPath
            if ($LASTEXITCODE -ne 0) { throw 'Create failed; existing resources retained for inspection' }
            $created.Add($item.kind)
        }
    }
    $deadline = [DateTime]::UtcNow.AddSeconds([int]$config.duration + 60)
    while ([DateTime]::UtcNow -lt $deadline) {
        $raw = & kubectl --context $Context -n $Namespace get job $name -o json
        if ($LASTEXITCODE -ne 0) { throw 'Job observation failed' }
        $observed = $raw | ConvertFrom-Json
        $conditions = @()
        if ($observed.status.PSObject.Properties['conditions']) { $conditions = @($observed.status.conditions) }
        $terminal = @($conditions | Where-Object { $_.status -eq 'True' -and $_.type -in @('Complete','Failed') })
        if ($terminal.Count -gt 0) { break }
        Start-Sleep -Seconds 2
    }
    $logs = & kubectl --context $Context -n $Namespace logs ('job/' + $name) --all-containers=true
    if ($LASTEXITCODE -ne 0) { throw 'Evidence retrieval failed' }
    $logs | Set-Content -LiteralPath (Join-Path $outputPath 'events.jsonl') -Encoding utf8
    $results = @($logs | ForEach-Object { try { $_ | ConvertFrom-Json } catch {} } | Where-Object { $_.PSObject.Properties.Name -contains 'outcome' })
    if ($results.Count -eq 0) { throw 'No terminal evidence; run is BLOCKED, never PASS' }
    $result = $results[-1]
    $result | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath (Join-Path $outputPath 'summary.json') -Encoding utf8
    $exitCode = @{PASS=0;FAIL=1;BLOCKED=2}[$result.outcome]
    if ($null -eq $exitCode) { $exitCode = 2 }
} finally {
    # Explicit cleanup affects only resources created by this invocation.
    if ($Cleanup -and $created.Count -gt 0) {
        foreach ($kind in (@($created.ToArray()) | Sort-Object { if ($_ -eq 'Job') { 0 } else { 1 } })) {
            & kubectl --context $Context -n $Namespace delete $kind $name --ignore-not-found --wait=false
        }
    }
    Pop-Location
}
exit $exitCode
