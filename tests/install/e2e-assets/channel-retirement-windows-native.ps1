# Native AppX observations and initial install/activation only. JSON in, JSON out.
param([Parameter(Mandatory=$true)][ValidateSet('preflight','artifact','available','installed','install','quit','process','listeners','registration','removed')][string]$Operation)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
if ($env:GITHUB_ACTIONS -cne 'true' -or $env:RUNNER_ENVIRONMENT -cne 'github-hosted' -or $env:OS -cne 'Windows_NT') { throw 'Disposable GitHub-hosted Windows required' }
$p = [Console]::In.ReadToEnd() | ConvertFrom-Json
. (Join-Path $PSScriptRoot 'windows-bundle-metadata.ps1')
Add-Type -AssemblyName System.IO.Compression.FileSystem
function Emit($Value) { ConvertTo-Json -Compress -Depth 32 -InputObject $Value }
function Exact-Package([bool]$Required) {
    $packages = @(Get-AppxPackage -Name $p.side.package.identity | Where-Object { $_.Name -ceq $p.side.package.identity })
    if (-not $Required -and $packages.Count -eq 0) { return $null }
    if ($packages.Count -ne 1) { throw 'Expected exactly one current-user registration' }
    $pkg = $packages[0]
    if ($pkg.Publisher -cne $p.side.package.publisher -or $pkg.Architecture.ToString() -ine $p.arch -or
        $pkg.Status.ToString() -cne 'Ok' -or $pkg.SignatureKind.ToString() -cne 'Developer' -or
        $pkg.IsDevelopmentMode -or $pkg.NonRemovable -or $pkg.IsPartiallyStaged) { throw 'Package is not the expected trusted sideload' }
    return $pkg
}
function Assert-Artifact {
    $file = $p.side.artifactPath
    if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -cne $p.side.package.artifact.sha256 -or
        (Get-Item -LiteralPath $file).Length -ne $p.side.package.artifact.size) { throw 'Artifact bytes differ' }
    $signature = Get-AuthenticodeSignature -LiteralPath $file
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -cne $p.side.package.publisher) { throw 'Artifact signature/publisher invalid' }
    $expected = [pscustomobject]@{ msixIdentity=$p.side.package.identity; publisher=$p.side.package.publisher; windowsVersion=$p.side.package.version; applicationId=$p.side.manifest.request.identity.appNamePascal }
    Read-BundleSmokeMetadata $file $p.arch $expected | Out-Null
}
function Installed {
    $pkg = Exact-Package $true
    if ($pkg.Version.ToString() -cne $p.side.package.version) { throw 'Registered native version mismatch' }
    $manifest = Get-AppxPackageManifest -Package $pkg.PackageFullName
    $expected = [pscustomobject]@{ msixIdentity=$p.side.package.identity; publisher=$p.side.package.publisher; windowsVersion=$p.side.package.version; applicationId=$p.side.manifest.request.identity.appNamePascal }
    $metadata = Read-SmokePackageManifest $manifest $p.arch $expected
    $exe = [IO.Path]::GetFullPath((Join-Path $pkg.InstallLocation $metadata.Executable))
    if (-not $exe.StartsWith($pkg.InstallLocation.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase) -or -not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw 'Invalid installed executable' }
    $resources = Join-Path (Split-Path -Parent $exe) 'resources'
    $stamp = Get-Content -LiteralPath (Join-Path $resources 'install-stamp.json') -Raw | ConvertFrom-Json
    return @{ identity=$pkg.Name; publisher=$pkg.Publisher; version=$pkg.Version.ToString(); arch=$pkg.Architecture.ToString();
        executable=$exe; appPath=$pkg.InstallLocation; resources=$resources; applicationId=$expected.applicationId;
        packageFamilyName=$pkg.PackageFamilyName; packageFullName=$pkg.PackageFullName; stamp=$stamp }
}
function Updater($Package) {
    $manager=[Windows.Management.Deployment.PackageManager,Windows.Management.Deployment,ContentType=WindowsRuntime]::new()
    $native=$manager.FindPackageForUser('', $Package.PackageFullName)
    $info=$native.GetAppInstallerInfo()
    if (-not $info) { throw 'Missing native App Installer registration' }
    return $info.Uri.AbsoluteUri
}
switch ($Operation) {
    'preflight' {
        $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        $profile = (Get-ItemProperty -LiteralPath ('HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\' + $sid)).ProfileImagePath
        $profile = [Environment]::ExpandEnvironmentVariables($profile)
        if ($profile -ine $env:USERPROFILE -or ($env:HOME -and $env:HOME -ine $profile)) { throw 'Account natural home differs from environment' }
        $local = [Environment]::GetFolderPath('LocalApplicationData')
        $roaming = [Environment]::GetFolderPath('ApplicationData')
        if ($env:LOCALAPPDATA -ine $local -or $env:APPDATA -ine $roaming) { throw 'Redirected AppData refused' }
        foreach ($scope in @('User','Machine')) {
            foreach ($key in @('HERMES_HOME','HERMES_DESKTOP_USER_DATA_DIR','HERMES_DATA_DIR_SUFFIX')) {
                if ([Environment]::GetEnvironmentVariable($key, $scope)) { throw 'Persistent workspace override refused' }
            }
        }
        $nativeArch = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString().ToLowerInvariant()
        if ($nativeArch -cne $p.arch) { throw 'Host architecture differs (emulation is not native acceptance)' }
        if (-not [Environment]::UserInteractive -or (Get-Process -Id $PID).SessionId -eq 0) { throw 'Interactive Windows desktop required, not session zero' }
        Get-Command Set-AppxPackageAutoUpdateSettings -ErrorAction Stop | Out-Null
        if (-not (Get-AppxPackage -Name Microsoft.DesktopAppInstaller)) { throw 'OS App Installer unavailable' }
        foreach ($identity in $p.identities) {
            if (@(Get-AppxPackage -Name $identity | Where-Object { $_.Name -ceq $identity }).Count) { throw 'Existing package identity refused' }
        }
        foreach ($file in @((Join-Path $profile '.hermes'), (Join-Path $local 'hermes'), (Join-Path $profile '.hermes-desktop-retirement'))) {
            if (Test-Path -LiteralPath $file) { throw "Existing private state refused: $file" }
        }
        Emit @{ home=$profile; localAppData=$local; appData=$roaming; sid=$sid; arch=$nativeArch }
    }
    'artifact' { Assert-Artifact; Emit @{ signature='Valid'; sha256=$p.side.package.artifact.sha256 } }
    'available' {
        $pkg = Exact-Package $false
        Emit ($null -ne $pkg -and $pkg.Version.ToString() -ceq $p.side.package.version)
    }
    'installed' { Emit (Installed) }
    'install' {
        if (Exact-Package $false) { throw 'Initial installation may not replace an existing package' }
        Assert-Artifact
        Add-AppxPackage -Path $p.side.artifactPath -ErrorAction Stop
        $installed = Installed
        # Pin a real source registration without background deployment racing the
        # OLD chat. The product, not the driver, installs and registers B/S/T.
        Set-AppxPackageAutoUpdateSettings -PackageFamilyName $installed.packageFamilyName -AppInstallerUri $p.feedUri -Version $p.side.package.version -UpdateUris @() -RepairUris @() -OptionalPackages @() -DependencyPackages @() -EnableAutomaticBackgroundTask:$false -ForceUpdateFromAnyVersion:$false -DisableAutoRepairs -CheckOnLaunch:$false -ShowPrompt:$false -UpdateBlocksActivation:$false -UseSystemPolicySource:$false -HoursBetweenUpdateChecks 12 -ErrorAction Stop | Out-Null
        if ((Updater (Exact-Package $true)) -cne $p.feedUri) { throw 'Initial registration readback differs' }
        Emit $installed
    }
    'registration' { Emit @{ uri=(Updater (Exact-Package $true)) } }
    'quit' {
        $proc = Get-Process -Id $p.pid -ErrorAction Stop
        if ($proc.Path -ine $p.executable -or $proc.StartTime.ToUniversalTime().ToString('o') -cne $p.birth) { throw 'Process identity changed' }
        if (-not $proc.CloseMainWindow()) { throw 'Normal close refused' }
        Emit @{ requested=$true }
    }
    'process' {
        $proc = Get-Process -Id $p.pid -ErrorAction Stop
        if ($proc.Path -ine $p.executable) { throw 'PID executable mismatch' }
        Emit @{ pid=$proc.Id; executable=$proc.Path; birth=$proc.StartTime.ToUniversalTime().ToString('o'); visible=($proc.MainWindowHandle -ne 0) }
    }
    'listeners' { Emit @(Get-NetTCPConnection -State Listen -ErrorAction Stop | Select-Object LocalAddress,LocalPort,OwningProcess) }
    'removed' {
        $package = Exact-Package $false
        $entries = @(Get-StartApps | Where-Object { $_.AppID.StartsWith($p.family + '!', [StringComparison]::OrdinalIgnoreCase) })
        Emit @{ registered=($null -ne $package); startApps=@($entries | Select-Object Name,AppID) }
    }
}
