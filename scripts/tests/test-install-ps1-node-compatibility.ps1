# PM owns Node provisioning. This verifies the installer's delegation boundary.
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
$installScript = Join-Path $repoRoot 'scripts\install.ps1'
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ("hermes-pm-delegation-" + [Guid]::NewGuid().ToString('N'))
$testHome = Join-Path $testRoot 'home'
$checkout = Join-Path $testRoot 'checkout'
$script:Failures = 0
function Assert-True($Condition, [string]$Label) {
    if ($Condition) { Write-Host "PASS: $Label" }
    else { Write-Host "FAIL: $Label"; $script:Failures++ }
}
# Tripwires exist before dot-sourcing, so a broken guard cannot run an install.
function Invoke-WebRequest { throw 'unexpected download' }
function Invoke-RestMethod { throw 'unexpected download' }
function git { throw 'unexpected git command' }
function uv { throw 'unexpected uv command' }
function node { throw 'unexpected node command' }
function npm { throw 'unexpected npm command' }

try {
    . $installScript -HermesHome $testHome -InstallDir $checkout
    Assert-True (-not (Test-Path $testRoot)) 'dot-source loads definitions without filesystem writes'

    $fakeUv = Join-Path $testRoot 'uv.cmd'
    $argsFile = Join-Path $testRoot 'args.txt'
    New-Item -ItemType Directory -Force -Path (Join-Path $checkout 'pm') | Out-Null
    @'
@echo off
echo %* > "%HERMES_TEST_UV_ARGS%"
exit /b 0
'@ | Set-Content -LiteralPath $fakeUv -Encoding Ascii
    $priorArgs = $env:HERMES_TEST_UV_ARGS
    $env:HERMES_TEST_UV_ARGS = $argsFile
    function Get-Uv { return $fakeUv }

    $failed = $false
    try { Invoke-BootstrapPm } catch { $failed = $true }
    Assert-True $failed 'missing lockfile refuses delegation'
    Assert-True (-not (Test-Path $argsFile)) 'missing lockfile never invokes uv'

    '{"packages":{"python":{"version":"3.13.2+test"}}}' |
        Set-Content -LiteralPath (Join-Path $checkout 'pm\lock.json') -Encoding UTF8
    Invoke-BootstrapPm
    $recorded = Get-Content -LiteralPath $argsFile -Raw
    Assert-True ($recorded -match '--no-project') 'uv runs without ambient project discovery'
    Assert-True ($recorded -match '--python 3\.13(\s|$)') 'Python minor comes from the lockfile'
    Assert-True ($recorded -match 'python -m pm\.cli install') 'PM owns the install'

    function Get-Uv { throw 'node stage attempted provisioning' }
    Stage-NodeDeps
    Write-Host 'PASS: node stage performs no separate install'
} finally {
    if (Get-Variable priorArgs -ErrorAction SilentlyContinue) {
        if ($null -eq $priorArgs) { Remove-Item Env:HERMES_TEST_UV_ARGS -ErrorAction SilentlyContinue }
        else { $env:HERMES_TEST_UV_ARGS = $priorArgs }
    }
    if (Test-Path $testRoot) { Remove-Item -LiteralPath $testRoot -Recurse -Force }
}
if ($script:Failures) { exit 1 }
Write-Host 'all assertions passed'
