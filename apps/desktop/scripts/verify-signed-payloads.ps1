param([Parameter(Mandatory = $true)][string]$Manifest)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$items = Get-Content -Raw -LiteralPath $Manifest | ConvertFrom-Json
$results = @(
    foreach ($item in $items) {
        $signature = Get-AuthenticodeSignature -LiteralPath $item.path
        $valid = $signature.Status -eq 'Valid' -and
            $signature.SignatureType -eq 'Authenticode' -and
            $null -ne $signature.TimeStamperCertificate -and
            $signature.SignerCertificate.Subject -eq $item.publisher
        [PSCustomObject]@{ path = $item.path; valid = [bool]$valid }
    }
)
ConvertTo-Json -InputObject $results -Compress
