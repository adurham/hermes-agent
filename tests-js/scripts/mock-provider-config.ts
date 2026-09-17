import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

import yaml from 'js-yaml'
import { z } from 'zod'

const section = z.object({}).passthrough()
const configSchema = z.object({
  model: section.optional(),
  providers: section.optional(),
  custom_providers: z.array(section).optional(),
  auxiliary: z.object({ title_generation: section.optional() }).passthrough().optional(),
  approvals: section.optional(),
  display: section.optional(),
}).passthrough()

//: Entry name this writer owns in ``custom_providers``; reconfiguring replaces
//: it rather than stacking duplicates.
const MOCK_PROVIDER_NAME = 'Mock'

export function validateMockUrl(value: string): string {
  const url = new URL(value)
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port
      || url.username || url.password || url.search || url.hash || url.pathname !== '/') {
    throw new Error('Mock URL must be a credential-free http://127.0.0.1:PORT origin')
  }
  return url.origin
}

/** Merge only test-owned settings. Do not replace the update feed or plugins. */
export function writeMockProviderConfig(
  hermesHome: string,
  mockUrl: string,
  extraDisplayConfig?: string,
  extraConfig?: string,
  modelContextLength?: number,
): void {
  const url = validateMockUrl(mockUrl)
  fs.mkdirSync(hermesHome, { recursive: true })
  const configPath = path.join(hermesHome, 'config.yaml')
  const config = configSchema.parse(fs.existsSync(configPath) ? yaml.load(fs.readFileSync(configPath, 'utf8')) ?? {} : {})
  const extra = configSchema.parse(extraConfig ? yaml.load(extraConfig) ?? {} : {})
  const display = section.parse(extraDisplayConfig ? yaml.load(extraDisplayConfig) ?? {} : {})
  const merged = {
    ...config,
    // The endpoint must live in config.yaml: runtime_provider's bare-`custom`
    // trust path reads the CONFIG base_url, and v2026.8.31 says so outright --
    // "OPENAI_BASE_URL env var is no longer consulted -- config.yaml is the
    // single source of truth for endpoint URLs". Without it `custom` resolves to
    // no endpoint and the app boots on its onboarding overlay ("No usable
    // credentials found for custom"), which is what every +desktop smoke leg
    // hit. The `custom_providers` entry names the same endpoint for the
    // named/`custom:<name>` form; the .env OPENAI_BASE_URL/OPENAI_API_KEY pair
    // (writeEnvFile) stays for the trees that resolve the endpoint from the
    // environment. `providers:` stays untouched (a provider named 'mock' only
    // resolved on trees that had registered such a profile, so older refs died
    // with "Unknown provider 'mock'").
    model: { ...config.model, default: 'mock-model', provider: 'custom', base_url: `${url}/v1`, context_length: modelContextLength ?? 64000 },
    providers: { ...config.providers },
    custom_providers: [
      ...(config.custom_providers ?? []).filter(entry => entry?.name !== MOCK_PROVIDER_NAME),
      { name: MOCK_PROVIDER_NAME, base_url: `${url}/v1`, key_env: 'OPENAI_API_KEY' },
    ],
    auxiliary: { ...config.auxiliary, title_generation: { ...config.auxiliary?.title_generation, enabled: false } },
    approvals: { ...config.approvals, mode: 'off' },
    ...extra,
  }
  if (extraDisplayConfig) {
    merged.display = { ...config.display, ...extra.display, ...display }
  }
  fs.writeFileSync(configPath, yaml.dump(merged), 'utf8')
}

/** Keep journey-owned entries, replacing only the inert test keys. */
export function writeEnvFile(hermesHome: string, apiKey = 'e2e-mock-key', mockUrl?: string): void {
  if (!/^[\w-]+$/.test(apiKey)) {
    throw new Error('Mock key must be an inert single-line test value')
  }
  const envPath = path.join(hermesHome, '.env')
  const prior = fs.existsSync(envPath) ? fs.readFileSync(envPath, 'utf8') : ''
  const lines = prior.split(/\r?\n/).filter((line: string): boolean => !/^\s*(?:export\s+)?(?:MOCK_API_KEY|OPENAI_BASE_URL|OPENAI_API_KEY)\s*=/.test(line))
  // OPENAI_BASE_URL + OPENAI_API_KEY is how EVERY vintage reaches an external
  // OpenAI-compatible endpoint ("custom"); a bare MOCK_API_KEY only means
  // something to a tree that knows a provider named `mock`, which older refs
  // do not (their resolve_provider accepts only openrouter/custom/registry).
  const portable = mockUrl ? `\nOPENAI_BASE_URL=${mockUrl}/v1\nOPENAI_API_KEY=${apiKey}` : ''
  fs.writeFileSync(envPath, `${lines.join('\n').trimEnd()}\nMOCK_API_KEY=${apiKey}${portable}\n`, { mode: 0o600 })
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const [home, url] = process.argv.slice(2)
  if (!home || !url || !path.isAbsolute(home)) {
    throw new Error('usage: node mock-provider-config.ts ABSOLUTE_HERMES_HOME MOCK_URL')
  }
  writeMockProviderConfig(home, url)
  writeEnvFile(home, 'e2e-mock-key', url)
}