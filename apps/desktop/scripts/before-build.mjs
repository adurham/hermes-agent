/**
 * Desktop bundles ship precompiled renderer assets. Returning false here tells
 * electron-builder to skip the node_modules collector/install step, which
 * avoids workspace dependency graph explosions and keeps packaging
 * deterministic across environments.
 *
 * Payload assembly belongs to scripts/bundles. This hook never creates it.
 *
 * Also stages the MSIX build-time assets (the build/appx icon set and the
 * build/msix-extensions.xml fragment the config points customExtensionsPath
 * at). These MUST happen here, in the hook electron-builder calls at build
 * time — not in electron-builder.config.cjs at require time, or every
 * typecheck/test import of the config would write files.
 */
import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'
import { appIdentity, storeManifestTemplate } from '../../../scripts/msix-shared.mjs'


const require = createRequire(import.meta.url)
const {
  light,
  store,
  displayName,
  appNamePascal
} = require('../product-identity.cjs')

export default async function beforeBuild() {
  stageMsixAssets()
  writeMsixExtensions()
  if (store) stageStoreManifest(path.join(import.meta.dirname, '..'), process.env.HERMES_PAYLOAD_TAG)

  return false
}

function stageMsixAssets() {
  const desktop = path.join(import.meta.dirname, '..')
  const sourceDir = path.join(desktop, 'assets', 'appx')
  const stageDir = path.join(desktop, 'build', 'appx')
  const names = [
    'Square44x44Logo.png',
    'Square150x150Logo.png',
    'StoreLogo.png',
    'Wide310x150Logo.png'
  ]

  fs.mkdirSync(stageDir, { recursive: true })
  for (const name of names) {
    const source = path.join(sourceDir, name)
    if (!fs.existsSync(source)) {
      throw new Error(`missing MSIX asset ${source}`)
    }
    fs.copyFileSync(source, path.join(stageDir, name))
  }
}

export function stageStoreManifest(desktop, tag) {
  const template = fs.readFileSync(path.join(desktop, 'assets/msix-manifest.xml'), 'utf8')
  const { version } = appIdentity(desktop, tag)
  const output = path.join(desktop, 'build/store-msix-manifest.xml')
  fs.mkdirSync(path.dirname(output), { recursive: true })
  fs.writeFileSync(output, storeManifestTemplate(template, version), 'utf8')
  return output
}

function writeMsixExtensions() {
  const desktop = path.join(import.meta.dirname, '..')
  const output = path.join('build', 'msix-extensions.xml')
  const file = path.join(desktop, output)
  const manifest = path.join(desktop, 'build', 'agent-payload', 'manifest.json')
  const launchers = ['bundled', 'store'].includes(process.env.HERMES_DESKTOP_VARIANT || '')
    ? JSON.parse(fs.readFileSync(manifest, 'utf8')).launchers : []
  if (!Array.isArray(launchers)) throw new Error('Bundled payload has no declared launchers')
  const aliases = appExecutionAliasExtensions(launchers)
  // The uap3:AppExtension fragment that registers the app as a Windows
  // Copilot hardware key provider. The press activates hermes://copilot-key/start.
  //
  // Content rules (violations are an opaque makeappx 0x80080204):
  //   * xmlns:uap3 rides on the fragment root — the stock manifest template
  //     declares no uap3 prefix. A/B-verified fine.
  //   * children of uap3:Properties are UNPREFIXED (xs:any content, per
  //     Microsoft's copilot-key-state sample).
  const copilot = light
    ? ''
    : `<uap3:Extension
    xmlns:uap3="http://schemas.microsoft.com/appx/manifest/uap/windows10/3"
    Category="windows.appExtension">
  <uap3:AppExtension
      Name="com.microsoft.windows.copilotkeyprovider"
      Id="${appNamePascal}CopilotKeyProvider"
      DisplayName="${displayName}"
      Description="Launch ${displayName} with the Copilot key"
      PublicFolder="Public">
    <uap3:Properties>
      <SingleTap>hermes://copilot-key/start?state=Tap</SingleTap>
      <PressAndHoldStart>hermes://copilot-key/start?state=Down</PressAndHoldStart>
      <PressAndHoldStop>hermes://copilot-key/stop?state=Up</PressAndHoldStop>
    </uap3:Properties>
  </uap3:AppExtension>
</uap3:Extension>
${aliases}`

  fs.mkdirSync(path.dirname(file), { recursive: true })
  fs.writeFileSync(file, copilot)
}

/**
 * One uap5:Extension carries the aliases declared by the payload.
 * Exported pure for tests.
 * @param {string[]} launchers exe stems under bin/
 */
export function appExecutionAliasExtensions(launchers) {
  if (launchers.length === 0) return ''
  const bs = String.fromCharCode(92)
  const executable = (name) => ['app', 'resources', 'agent-payload', 'bin', `${name}.exe`].join(bs)
  // ONE windows.appExecutionAlias extension per package — makeappx rejects a
  // second one with the opaque 0x80080204 (A/B-verified against the 26100
  // kit). Every launcher alias rides in the same extension's AppExecutionAlias;
  // the Executable attribute names the exe that serves the aliases.
  return `<uap5:Extension
    xmlns:uap5="http://schemas.microsoft.com/appx/manifest/uap/windows10/5"
    Category="windows.appExecutionAlias"
    Executable="${executable(launchers[0])}"
    EntryPoint="Windows.FullTrustApplication">
  <uap5:AppExecutionAlias>
${launchers
  .map((name) => `    <uap5:ExecutionAlias Alias="${name}.exe" />`)
  .join('\n')}
  </uap5:AppExecutionAlias>
</uap5:Extension>`
}
