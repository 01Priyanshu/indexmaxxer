# Run from C:\Users\Priyanshu\indexmaxxer after extracting the package.
# No activation needed: the existing virtual environment is used explicitly.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) {
    throw 'The project virtual environment was not found. Extract this package into your existing indexmaxxer folder.'
}

function Invoke-ProjectPython {
    param([string[]]$Arguments)
    & $projectPython @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Stopped because this command failed: python $($Arguments -join ' '). Keep the error output for troubleshooting."
    }
}

Write-Host 'Starting the existing PostgreSQL container...'
docker start indexmaxxer-db
if ($LASTEXITCODE -ne 0) {
    throw 'Open Docker Desktop, wait until its engine is running, and run this script again.'
}
$databaseReady = $false
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    docker exec indexmaxxer-db pg_isready -U indexmaxxer -d indexmaxxer | Out-Null
    if ($LASTEXITCODE -eq 0) { $databaseReady = $true; break }
    Start-Sleep -Seconds 1
}
if (-not $databaseReady) { throw 'PostgreSQL did not become ready. Inspect docker logs indexmaxxer-db.' }

Write-Host "`nInstalling the TPC-H data generator dependency..."
Invoke-ProjectPython -Arguments @('-m', 'pip', 'install', 'duckdb==2.0.0')
Write-Host "`n1/4: Frequency comparison (offline; existing reports are preserved)"
Invoke-ProjectPython -Arguments @('-m', 'experiments.compare_frequency')
Write-Host "`n2/4: Uniform versus skewed data"
Invoke-ProjectPython -Arguments @('-m', 'experiments.compare_skew')
Write-Host "`n3/4: Selected TPC-H-derived queries at scale 0.1"
Invoke-ProjectPython -Arguments @('-m', 'experiments.compare_tpch', '--scale', '0.1')
Write-Host "`n4/4: Exporting the three additional experiment groups"
Invoke-ProjectPython -Arguments @('-m', 'experiments.export_remaining')

$dependencyLines = & $projectPython -m pip freeze
if ($LASTEXITCODE -ne 0) { throw 'Could not record dependencies.' }
$dependencyLines | Set-Content -Encoding ascii requirements.txt

$readmePath = Join-Path $PSScriptRoot 'README.md'
$readmeLink = 'See [additional experiment methods and reproduction steps](docs/ADDITIONAL_EXPERIMENTS.md).'
if (Test-Path -LiteralPath $readmePath) {
    $readmeText = Get-Content -Raw -LiteralPath $readmePath
    if (-not $readmeText.Contains($readmeLink)) {
        [System.IO.File]::AppendAllText($readmePath, "`r`n## Additional experiments`r`n`r`n$readmeLink`r`n`r`nThe additional exporter writes three PNG figures, three SVG figures, six CSV tables, and a source manifest under results/additional_exports_<timestamp>/. These supplement the original five experiment figures. See the completed JSON reports for actual measured results.`r`n", [System.Text.UTF8Encoding]::new($false))
    }
}
Write-Host "`nFINISHED: all three additional experiments and their exports completed."
Write-Host 'The original dashboard still shows its existing results; these additional figures are in results/.'
Write-Host 'Review git status, then commit and push using the commands in docs/ADDITIONAL_EXPERIMENTS.md.'
