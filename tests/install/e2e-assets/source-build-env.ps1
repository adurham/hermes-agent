# Child installers/builds stamp their checkout; restore the workflow identity even on failure.
function Invoke-SourceBuild([scriptblock]$Action) {
    $saved = @{}
    $names = @('GITHUB_SHA', 'GITHUB_REF', 'GITHUB_REF_NAME', 'GITHUB_HEAD_REF', 'GITHUB_BASE_REF',
        'HERMES_BUILD_COMMIT', 'HERMES_PAYLOAD_TAG', 'HERMES_PAYLOAD_VERSION', 'HERMES_DESKTOP_VARIANT')
    # The env: drive, not [Environment]::SetEnvironmentVariable: on .NET/Unix the latter only
    # updates the managed copy, so a child spawned afterwards still inherits the old value.
    try {
        foreach ($name in $names) {
            $saved[$name] = Get-Content -LiteralPath "env:$name" -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath "env:$name" -ErrorAction SilentlyContinue
        }
        & $Action
    } finally {
        foreach ($name in $names) {
            if ($null -ne $saved[$name]) { Set-Item -LiteralPath "env:$name" -Value $saved[$name] }
        }
    }
}
