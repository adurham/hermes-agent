import path from 'node:path'

import { assertNativeRetirementRemoval, type RetirementNativeAdapter } from './retirement'
import {
  assertRunningRetirementStamp,
  downloadRetirementArtifact,
  type NativeCommandResult,
  runRetirementCommand
} from './retirement-native'
import { retirementArgument } from './retirement-receiver'
import { canonicalRetirementPath, type RetirementJournal, type RetirementRequest } from './retirement-state'

interface WindowsInstalled {
  executable: string
  packageFullName: string
  appPath: string
}
type WindowsOperation = 'install' | 'verify' | 'activate' | 'remove' | 'removed'

// AppX cmdlets and manifest/signature checks follow windows-bundle-smoke.ps1.
// The activation COM ABI is IApplicationActivationManager from shobjidl_core.h.
const windowsNativeScript: string = String.raw`
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.IO.Compression.FileSystem
function Read-ZipXml($zip, [string]$name) {
  $entries = @($zip.Entries | Where-Object { $_.FullName -ceq $name })
  if ($entries.Count -ne 1) { throw 'Ambiguous package manifest' }
  $settings = [Xml.XmlReaderSettings]::new()
  $settings.DtdProcessing = [Xml.DtdProcessing]::Prohibit
  $settings.XmlResolver = $null
  $stream = $entries[0].Open()
  $reader = [Xml.XmlReader]::Create($stream, $settings)
  try {
    $xml = [Xml.XmlDocument]::new(); $xml.XmlResolver = $null; $xml.Load($reader)
    return ,$xml
  } finally { $reader.Dispose(); $stream.Dispose() }
}
function Assert-Artifact {
  if ((Get-FileHash -Algorithm SHA256 -LiteralPath $p.artifact).Hash.ToLowerInvariant() -cne $p.request.destination.artifact.sha256) { throw 'Artifact digest mismatch' }
  $signature = Get-AuthenticodeSignature -LiteralPath $p.artifact
  if ($signature.Status -ne 'Valid' -or -not $signature.SignerCertificate -or $signature.SignerCertificate.Subject -cne $d.signer) { throw 'Artifact signature/publisher mismatch' }
  $zip = [IO.Compression.ZipFile]::OpenRead($p.artifact)
  try {
    if ($d.artifact.format -ceq 'msixbundle') {
      $xml = Read-ZipXml $zip 'AppxMetadata/AppxBundleManifest.xml'
      $identity = $xml.Bundle.Identity
      $slices = @($xml.Bundle.Packages.Package | Where-Object { $_.Type -ceq 'application' -and $_.Architecture -ceq $d.architecture })
      if ($slices.Count -ne 1 -or $slices[0].Version -cne $d.nativeVersion) { throw 'Native slice mismatch' }
    } elseif ($d.artifact.format -ceq 'msix') {
      $xml = Read-ZipXml $zip 'AppxManifest.xml'
      $identity = $xml.Package.Identity
      if ($identity.ProcessorArchitecture -cne $d.architecture) { throw 'Native architecture mismatch' }
    } else { throw 'Expected MSIX or MSIXBUNDLE' }
    if ($identity.Name -cne $d.identity -or $identity.Publisher -cne $d.signer -or $identity.Version -cne $d.nativeVersion) { throw 'Artifact native identity mismatch' }
  } finally { $zip.Dispose() }
}
function Get-ExactPackage($identity, [bool]$required) {
  $packages = @(Get-AppxPackage -Name $identity.identity | Where-Object { $_.Name -ceq $identity.identity })
  if (-not $packages.Count -and -not $required) { return $null }
  if ($packages.Count -ne 1) { throw 'Expected exactly one current-user package' }
  $pkg = $packages[0]
  if ($pkg.Publisher -cne $identity.signer -or $pkg.Architecture.ToString() -ine $identity.architecture -or $pkg.Status.ToString() -cne 'Ok' -or $pkg.IsDevelopmentMode) { throw 'Native identity/status mismatch' }
  return $pkg
}
function Assert-Sideload($pkg) {
  if ($pkg.SignatureKind.ToString() -cne 'Developer' -or $pkg.NonRemovable -or $pkg.IsPartiallyStaged) { throw 'Package is Store/system/managed; its steward must update or remove it' }
}
function Assert-NoProcesses($pkg) {
  $live = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($pkg.InstallLocation.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase) })
  if ($live.Count) { throw 'Package has live processes; lifecycle owner must stop them' }
}
function Assert-Destination {
  $pkg = Get-ExactPackage $d $true
  if ($pkg.Version.ToString() -cne $d.nativeVersion -or $pkg.PackageFamilyName -cne $d.packageFamilyName -or $pkg.InstallLocation -ine $d.appPath) { throw 'Installed destination binding mismatch' }
  $manifest = Get-AppxPackageManifest -Package $pkg.PackageFullName
  $apps = @($manifest.Package.Applications.Application | Where-Object { $_.Id -ceq $d.applicationId })
  if ($apps.Count -ne 1) { throw 'Registered destination application missing' }
  $exe = [IO.Path]::GetFullPath((Join-Path $pkg.InstallLocation $apps[0].Executable))
  if (-not $exe.StartsWith($pkg.InstallLocation.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase) -or -not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw 'Invalid registered executable' }
  $stamp = Get-Content -Raw -LiteralPath (Join-Path (Split-Path -Parent $exe) 'resources\install-stamp.json') | ConvertFrom-Json
  if ($stamp.commit -cne $d.commit -or $stamp.payload -cne 'bundled' -or $stamp.distribution -cne 'desktop-app' -or $stamp.dirty -ne $false -or $stamp.updateMechanism -cne 'app-installer') { throw 'Destination stamp mismatch' }
  return @{ executable=$exe; packageFullName=$pkg.PackageFullName; appPath=$pkg.InstallLocation }
}
$d = $p.request.destination
switch ($p.operation) {
  'install' {
    $existing = Get-ExactPackage $d $false
    if ($existing -and $existing.Version.ToString() -ceq $d.nativeVersion) {
      Assert-Destination | ConvertTo-Json -Compress
      break
    }
    if ($existing) {
      Assert-Sideload $existing
      if (-not $p.request.consent.replaceExistingStable -or [version]$existing.Version -ge [version]$d.nativeVersion) { throw 'Existing stable requires steward update or explicit upgrade consent' }
      Assert-NoProcesses $existing
    }
    Assert-Artifact
    Add-AppxPackage -Path $p.artifact -ErrorAction Stop
    Assert-Destination | ConvertTo-Json -Compress
  }
  'verify' { Assert-Destination | ConvertTo-Json -Compress }
  'activate' {
    $installed = Assert-Destination
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
[ComImport, Guid("2e941141-7f97-4756-ba1d-9decde894a3d"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IRetirementActivation {
  [PreserveSig] int ActivateApplication([MarshalAs(UnmanagedType.LPWStr)] string id, [MarshalAs(UnmanagedType.LPWStr)] string args, uint options, out uint pid);
  [PreserveSig] int ActivateForFile([MarshalAs(UnmanagedType.LPWStr)] string id, IntPtr items, [MarshalAs(UnmanagedType.LPWStr)] string verb, out uint pid);
  [PreserveSig] int ActivateForProtocol([MarshalAs(UnmanagedType.LPWStr)] string id, IntPtr items, out uint pid);
}
public static class RetirementActivation {
  public static uint Launch(string id, string args) {
    object manager = Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("45BA127D-10A8-46EA-8AB7-56EA9078943C")));
    try { uint pid; Marshal.ThrowExceptionForHR(((IRetirementActivation)manager).ActivateApplication(id, args, 0, out pid)); return pid; }
    finally { Marshal.FinalReleaseComObject(manager); }
  }
}
'@
    $pidValue = [RetirementActivation]::Launch(($d.packageFamilyName + '!' + $d.applicationId), $p.argument)
    @{ pid=$pidValue } | ConvertTo-Json -Compress
  }
  'remove' {
    $installed = Assert-Destination
    if ($installed.executable -ine $p.receiverExecutable) { throw 'Cleanup must run from the destination executable' }
    $source = Get-ExactPackage $p.request.source $false
    if (-not $source) { break }
    if ($source.PackageFullName -cne $p.request.source.packageFullName -or $source.InstallLocation -ine $p.request.source.appPath -or $source.Version.ToString() -cne $p.request.source.nativeVersion) { throw 'Preview package changed before removal' }
    Assert-Sideload $source
    Assert-NoProcesses $source
    $packageData = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) ('Packages\' + $source.PackageFamilyName)
    if (Test-Path -LiteralPath $packageData) {
      # Remove-AppxPackage -PreserveApplicationData only supports development
      # registrations. Archive signed sideload data outside the package instead;
      # encrypted envelopes are retained for recovery, never imported into stable.
      $archive=Join-Path $p.journalDirectory 'preview-package-data'
      $entries=@(Get-ChildItem -LiteralPath $packageData -Recurse -Force -ErrorAction Stop)
      if (@($entries | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }).Count) { throw 'Package data contains a reparse point; preserve its target manually before removal' }
      New-Item -ItemType Directory -Path $archive -Force | Out-Null
      & "$env:SystemRoot\System32\robocopy.exe" $packageData $archive /E /COPY:DAT /DCOPY:DAT /XJ /R:0 /W:0 /NFL /NDL /NJH /NJS | Out-Null
      if ($LASTEXITCODE -gt 7) { throw 'Package-private data archive failed; preview preserved' }
      foreach ($entry in @($entries | Where-Object { -not $_.PSIsContainer })) {
        $relative=$entry.FullName.Substring($packageData.Length).TrimStart('\')
        $copy=Join-Path $archive $relative
        if (-not (Test-Path -LiteralPath $copy -PathType Leaf) -or (Get-FileHash -LiteralPath $copy -Algorithm SHA256).Hash -cne (Get-FileHash -LiteralPath $entry.FullName -Algorithm SHA256).Hash) { throw 'Package-private data archive verification failed; preview preserved' }
      }
    }
    $sourceStamp = Get-Content -Raw -LiteralPath (Join-Path (Split-Path -Parent $p.request.source.executable) 'resources\install-stamp.json') | ConvertFrom-Json
    if ($sourceStamp.commit -cne $p.request.qualification.sourceCommit -or $sourceStamp.distribution -cne 'desktop-app' -or $sourceStamp.payload -cne 'bundled' -or $sourceStamp.updateMechanism -cne 'app-installer') { throw 'Preview source stamp mismatch' }
    Assert-NoProcesses $source
    Remove-AppxPackage -Package $source.PackageFullName -ErrorAction Stop
    if (Get-ExactPackage $p.request.source $false) { throw 'Preview package remains registered' }
  }
  'removed' {
    $source = Get-ExactPackage $p.request.source $false
    @{ removed=($null -eq $source) } | ConvertTo-Json -Compress
  }
}
`

export class WindowsRetirementAdapter implements RetirementNativeAdapter {
  private async run(
    operation: WindowsOperation,
    request: RetirementRequest,
    artifact: string | null = null,
    journalDirectory: string | null = null
  ): Promise<string> {
    if (process.platform !== 'win32' || request.destination.platform !== 'win32') {
      throw new Error('Native Windows retirement requires Windows')
    }

    if (
      !request.destination.packageFamilyName ||
      !request.destination.applicationId ||
      !request.source.packageFullName
    ) {
      throw new Error('Exact AppX family/application/full-name bindings are required')
    }

    const payload: string = JSON.stringify({
      operation,
      request,
      artifact,
      journalDirectory,
      argument: retirementArgument(request.id, request.token),
      receiverExecutable: process.execPath
    })

    const script: string = `$p = [Console]::In.ReadToEnd() | ConvertFrom-Json\n${windowsNativeScript}`

    const result: NativeCommandResult = await runRetirementCommand(
      path.join(process.env.SystemRoot || 'C:\\Windows', 'System32/WindowsPowerShell/v1.0/powershell.exe'),
      ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(script, 'utf16le').toString('base64')],
      600_000,
      payload
    )

    return result.stdout.trim()
  }

  async install(request: RetirementRequest, journal: RetirementJournal): Promise<void> {
    const artifact: string = await downloadRetirementArtifact(journal, request.destination)
    await this.run('install', request, artifact)
  }

  async verifyDestination(request: RetirementRequest): Promise<void> {
    await this.run('verify', request)
  }

  async verifyRunningDestination(request: RetirementRequest): Promise<string> {
    assertRunningRetirementStamp(request.destination)
    const installed: WindowsInstalled = JSON.parse(await this.run('verify', request))
    const running: string = await canonicalRetirementPath(process.execPath)

    if (running.toLowerCase() !== (await canonicalRetirementPath(installed.executable)).toLowerCase()) {
      throw new Error('Receiver is not the exact registered stable executable')
    }

    return installed.executable
  }

  async activate(request: RetirementRequest, _journal: RetirementJournal): Promise<void> {
    await this.run('activate', request)
  }

  async removePreview(request: RetirementRequest, journal: RetirementJournal): Promise<void> {
    await assertNativeRetirementRemoval(journal, request)
    await this.verifyRunningDestination(request)
    await this.run('remove', request, null, journal.directory)
  }

  async isPreviewRemoved(request: RetirementRequest): Promise<boolean> {
    const result: { removed: boolean } = JSON.parse(await this.run('removed', request))

    return result.removed === true
  }
}
