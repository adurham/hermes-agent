import path from 'node:path'

import type { ChannelTarget } from './channel'
import { runRetirementPowerShell } from './retirement-discovery'
import { downloadRetirementArtifact } from './retirement-native'
import { newRetirementCorrelation, RetirementJournal, defaultRetirementRoot } from './retirement-state'

/** Verify downloaded bytes and native metadata before App Installer can stop the backend. */
export async function verifyPreparedChannelInstaller(file: string, target: ChannelTarget): Promise<void> {
  const pkg = target.package
  if (pkg.platform !== 'win32' || !pkg.publisher) { throw new Error('Expected a publisher-bound Windows package') }
  const journal: RetirementJournal = await RetirementJournal.open(defaultRetirementRoot(), newRetirementCorrelation().id)
  const artifact: string = await downloadRetirementArtifact(journal, {
    platform: 'win32', appPath: path.dirname(file), identity: pkg.identity, nativeVersion: pkg.version,
    signer: pkg.publisher, architecture: pkg.arch, applicationId: null, packageFamilyName: null,
    commit: target.manifest.request.commit,
    artifact: { url: target.artifactUrl, sha256: pkg.artifact.sha256, size: pkg.artifact.size,
      format: pkg.artifact.key.endsWith('.msixbundle') ? 'msixbundle' : 'msix' }
  })
  await runRetirementPowerShell(String.raw`
$ErrorActionPreference='Stop'
$p=[Console]::In.ReadToEnd() | ConvertFrom-Json
function Read-SafeXml($stream) {
  $settings=[Xml.XmlReaderSettings]::new(); $settings.DtdProcessing=[Xml.DtdProcessing]::Prohibit; $settings.XmlResolver=$null
  $reader=[Xml.XmlReader]::Create($stream,$settings)
  try { $xml=[Xml.XmlDocument]::new(); $xml.XmlResolver=$null; $xml.Load($reader); return ,$xml } finally { $reader.Dispose() }
}
$stream=[IO.File]::OpenRead($p.file)
try { $xml=Read-SafeXml $stream } finally { $stream.Dispose() }
$root=$xml.DocumentElement
if ($root.LocalName -cne 'AppInstaller' -or $root.GetAttribute('Uri') -cne $p.feedUrl -or $root.GetAttribute('Version') -cne $p.version) { throw 'Channel descriptor binding mismatch' }
$main=@($root.ChildNodes | Where-Object { $_.LocalName -in @('MainPackage','MainBundle') })
if ($main.Count -ne 1 -or $main[0].GetAttribute('Name') -cne $p.identity -or $main[0].GetAttribute('Publisher') -cne $p.publisher -or $main[0].GetAttribute('Version') -cne $p.version -or $main[0].GetAttribute('Uri') -cne $p.artifactUrl) { throw 'Descriptor does not reference the pinned native package' }
if (@($root.ChildNodes | Where-Object { $_.LocalName -notin @('MainPackage','MainBundle') }).Count) { throw 'Pinned channel descriptor must not add dependencies, optional packages or background update policies' }
$signature=Get-AuthenticodeSignature -LiteralPath $p.artifact
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -cne $p.publisher) { throw 'Native artifact signature/publisher mismatch' }
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip=[IO.Compression.ZipFile]::OpenRead($p.artifact)
try {
  $name=if ($p.artifact.EndsWith('.msixbundle')) { 'AppxMetadata/AppxBundleManifest.xml' } else { 'AppxManifest.xml' }
  $entry=$zip.GetEntry($name)
  if (-not $entry) { throw 'Native manifest missing' }
  $stream=$entry.Open()
  try { $native=Read-SafeXml $stream } finally { $stream.Dispose() }
  if ($name -ceq 'AppxManifest.xml') { $identity=$native.Package.Identity; if ($identity.ProcessorArchitecture -cne $p.arch) { throw 'Package architecture mismatch' } }
  else { $identity=$native.Bundle.Identity; $slices=@($native.Bundle.Packages.Package | Where-Object { $_.Type -ceq 'application' -and $_.Architecture -ceq $p.arch -and $_.Version -ceq $p.version }); if ($slices.Count -ne 1) { throw 'Bundle architecture/version mismatch' } }
  if ($identity.Name -cne $p.identity -or $identity.Publisher -cne $p.publisher -or $identity.Version -cne $p.version) { throw 'Native manifest binding mismatch' }
} finally { $zip.Dispose() }
`, JSON.stringify({ file, artifact, feedUrl: target.feedUrl, artifactUrl: target.artifactUrl,
    identity: pkg.identity, publisher: pkg.publisher, version: pkg.version, arch: pkg.arch }))
}
