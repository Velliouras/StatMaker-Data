<#
StatMaker Betting V2: exactly the existing PROD App-Ready betting ZIP, READ ONLY.
No API requests, no Actions, no Android edits, no PROD/App-Ready modifications.
Run inside a separate clone/worktree checked out to this UAT branch.
#>
param(
    [Parameter(Mandatory=$true)] [string] $DataRoot,
    [string] $OutputDirectory = (Join-Path $env:TEMP 'StatMaker-V2-Real-Diagnostics')
)
$ErrorActionPreference='Stop'
$original = Get-Location
try {
    $DataRoot = (Resolve-Path -LiteralPath $DataRoot).Path
    $manifestPath = Join-Path $DataRoot 'data/statmaker/app_ready/update_manifest.json'
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $bundle = @($manifest.artifacts | Where-Object { $_.id -eq 'app_ready_betting_bundle' })
    if ($bundle.Count -ne 1) { throw 'Canonical App-Ready betting bundle missing or ambiguous' }
    $bundle = $bundle[0]
    $dbZip = Join-Path $DataRoot ($bundle.path -replace '/', [IO.Path]::DirectorySeparatorChar)
    if (-not (Test-Path -LiteralPath $dbZip -PathType Leaf)) { throw "Missing PROD source ZIP: $dbZip" }
    $sha = (Get-FileHash -LiteralPath $dbZip -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($sha -ne [string]$bundle.sha256) { throw "PROD bundle hash mismatch: $sha" }
    $generation = [string]$manifest.metadata.preparedPatternGenerationId
    if ([string]::IsNullOrWhiteSpace($generation)) { throw 'Missing canonical generation ID' }
    $runner = Join-Path $DataRoot 'scripts/betting_v2/run_real_prod_diagnostic.py'
    $tests = Join-Path $DataRoot 'scripts/betting_v2/test_real_prod_diagnostic.py'
    if (-not (Test-Path $runner) -or -not (Test-Path $tests)) { throw 'Wrong Git UAT branch: diagnostic files missing' }
    $env:PYTHONDONTWRITEBYTECODE='1'
    $python = (Get-Command python -ErrorAction Stop).Source
    Set-Location -LiteralPath $DataRoot
    & $python -m unittest scripts.betting_v2.test_real_prod_diagnostic -v
    if ($LASTEXITCODE -ne 0) { throw "Diagnostic regressions failed: $LASTEXITCODE" }
    New-Item -Force -ItemType Directory -Path $OutputDirectory | Out-Null
    $outputPath = Join-Path $OutputDirectory ("real-prod-" + (Get-Date -Format 'yyyyMMdd-HHmmssfff') + '.json')
    & $python -m scripts.betting_v2.run_real_prod_diagnostic --repository-root $DataRoot --app-ready $dbZip --generation-id $generation --output $outputPath
    if ($LASTEXITCODE -ne 0) { throw "Real PROD diagnostic failed: $LASTEXITCODE" }
    $result = Get-Content -LiteralPath $outputPath -Raw -Encoding UTF8 | ConvertFrom-Json
    Write-Host "FULL PROD: $($result.totalFixtureRows) fixtures / $($result.rawQuotedRows) quoted rows / $($result.forecastableFixtures) forecastable fixtures / $($result.candidateCount) preliminary selections"
    $result.candidates | Select-Object -First 20 league,homeTeam,awayTeam,market,selection,odd,winProbability,indicativeEV | Format-Table -AutoSize
    Write-Host "Complete audit: $outputPath"
} finally {
    Set-Location $original
}