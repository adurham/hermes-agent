// #77311: the renderer heap ceiling knob (`desktop.renderer_max_old_space_mb`)
// and `desktop.electron_flags` must reach app.commandLine on packaged launches
// that never go through the `hermes desktop` launcher.
import { describe, expect, it, vi } from 'vitest'

import { planLaunchSwitches, readDesktopLaunchConfig } from './renderer-heap-flags'

describe('renderer heap flags', () => {
  it('reads both launch keys from config.yaml and plans a well-formed js-flags switch', () => {
    const cfg = readDesktopLaunchConfig(
      [
        'model:',
        '  default: x',
        'desktop:',
        '  font_family: ""',
        '  electron_flags:',
        '    - --ozone-platform=x11',
        '    - "--js-flags=--expose-gc"',
        '  renderer_max_old_space_mb: 2048  # ceiling',
        'terminal:',
        '  electron_flags: [--not-desktop]'
      ].join('\n')
    )

    expect(cfg).toEqual({
      electronFlags: ['--ozone-platform=x11', '--js-flags=--expose-gc'],
      rendererMaxOldSpaceMb: 2048
    })
    expect(planLaunchSwitches(cfg)).toEqual([
      { name: 'ozone-platform', value: 'x11' },
      { name: 'js-flags', value: '--expose-gc --max-old-space-size=2048' }
    ])
    // Default 0 = Chromium's own heap limit, nothing applied.
    expect(planLaunchSwitches(readDesktopLaunchConfig('desktop:\n  renderer_max_old_space_mb: 0\n'))).toEqual([])
  })

  it('merges with a js-flags switch already on argv instead of overwriting it', () => {
    const cfg = readDesktopLaunchConfig(
      'desktop:\n  electron_flags: [--disable-gpu]\n  renderer_max_old_space_mb: 1536\n'
    )

    const planned = planLaunchSwitches(cfg, ['/app/hermes', '--js-flags=--expose-gc', '--disable-gpu'])

    // --disable-gpu is already on the launcher's argv: not re-applied.
    expect(planned).toEqual([{ name: 'js-flags', value: '--expose-gc --max-old-space-size=1536' }])

    // Space-separated is the same switch: the argv value must reach the merge
    // (and win the ceiling) rather than being scanned as a switch of its own.
    expect(planLaunchSwitches(cfg, ['/app/hermes', '--js-flags', '--max-old-space-size=4096'])).toEqual([
      { name: 'disable-gpu' },
      { name: 'js-flags', value: '--max-old-space-size=4096' }
    ])
  })

  it('reads the renderer_accessibility opt-out for packaged launches (#118271)', () => {
    expect(readDesktopLaunchConfig('desktop:\n  renderer_accessibility: false\n').rendererAccessibility).toBe(false)
    expect(readDesktopLaunchConfig('desktop:\n  renderer_accessibility: "off"  # perf\n').rendererAccessibility).toBe(
      false
    )
    expect(readDesktopLaunchConfig('desktop:\n  renderer_accessibility: true\n').rendererAccessibility).toBe(true)
    // Bare `0` is the string '0' to this reader (no YAML parser), but the
    // unquoted form a user actually writes must opt out like the quoted one.
    expect(readDesktopLaunchConfig('desktop:\n  renderer_accessibility: 0\n').rendererAccessibility).toBe(false)
    // `disabled`/`enabled` mirror the launcher's word list so both launch
    // paths accept the same vocabulary (#118271 review).
    expect(readDesktopLaunchConfig('desktop:\n  renderer_accessibility: disabled\n').rendererAccessibility).toBe(false)
    expect(readDesktopLaunchConfig('desktop:\n  renderer_accessibility: "Disabled"\n').rendererAccessibility).toBe(
      false
    )
    expect(readDesktopLaunchConfig('desktop:\n  renderer_accessibility: enabled\n').rendererAccessibility).toBe(true)
    // Absent or unreadable: unset, so the app keeps its own default.
    expect(readDesktopLaunchConfig('desktop:\n  font_family: ""\n').rendererAccessibility).toBeUndefined()
    expect(readDesktopLaunchConfig('desktop:\n  renderer_accessibility: maybe\n').rendererAccessibility).toBeUndefined()
  })

  it('warns once when a desktop block names the keys in an indentation it cannot read', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)

    // Valid YAML, unsupported subset (four-space keys / two-space list items).
    expect(readDesktopLaunchConfig('desktop:\n    renderer_max_old_space_mb: 2048\n')).toEqual({
      electronFlags: [],
      rendererMaxOldSpaceMb: 0
    })
    expect(warn).toHaveBeenCalledTimes(1)

    // A desktop block that never mentions them is not a misconfiguration.
    readDesktopLaunchConfig('desktop:\n  font_family: ""\n')
    expect(warn).toHaveBeenCalledTimes(1)

    warn.mockRestore()
  })

  it('stays quiet when the keys are explicitly set to their defaults', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)

    // What `hermes config` writes when the two knobs are untouched: an explicit
    // empty list and 0 are the defaults, not a misconfiguration.
    expect(
      readDesktopLaunchConfig(
        [
          'desktop:',
          '  font_family: ""',
          '  electron_flags: []',
          '  renderer_max_old_space_mb: 0',
          'terminal:',
          '  electron_flags: [--not-desktop]'
        ].join('\n')
      )
    ).toEqual({ electronFlags: [], rendererMaxOldSpaceMb: 0 })
    expect(warn).not.toHaveBeenCalled()

    // A bare `electron_flags:` followed by another key is an explicit empty too.
    readDesktopLaunchConfig('desktop:\n  electron_flags:\n  font_family: ""\n')
    expect(warn).not.toHaveBeenCalled()

    // Comments at column 0 and between items do not hide the list.
    expect(
      readDesktopLaunchConfig(
        [
          'desktop:',
          '  electron_flags:',
          '#    - --commented-out',
          '    - --enable-logging',
          '',
          '    # between items',
          '    - --disable-gpu',
          '  font_family: ""'
        ].join('\n')
      ).electronFlags
    ).toEqual(['--enable-logging', '--disable-gpu'])
    expect(warn).not.toHaveBeenCalled()

    // YAML allows an indentless (column 0) or two-space list; both are read as-is.
    expect(readDesktopLaunchConfig('desktop:\n  electron_flags:\n- --a\n- --b\n').electronFlags).toEqual(['--a', '--b'])
    expect(readDesktopLaunchConfig('desktop:\n  electron_flags:\n  - --a\n  - --b\n').electronFlags).toEqual([
      '--a',
      '--b'
    ])
    expect(warn).not.toHaveBeenCalled()

    // `~` and an explicit 0 (with a trailing comment) are defaults too.
    readDesktopLaunchConfig('desktop:\n  renderer_max_old_space_mb: ~\n  electron_flags: []\n')
    readDesktopLaunchConfig('desktop:\n  renderer_max_old_space_mb: 0  # default\n')
    expect(warn).not.toHaveBeenCalled()

    warn.mockRestore()
  })

  it('warns for values it cannot read, without mistaking a later block for this one', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)

    // A ceiling that is not a positive integer.
    readDesktopLaunchConfig('desktop:\n  renderer_max_old_space_mb: lots\n')
    expect(warn).toHaveBeenCalledTimes(1)

    // Four-space keys are valid YAML but outside the supported subset.
    readDesktopLaunchConfig('desktop:\n  electron_flags: []\n    renderer_max_old_space_mb: 2048\n')
    expect(warn).toHaveBeenCalledTimes(2)

    // A list whose indent changes mid-way.
    readDesktopLaunchConfig('desktop:\n  electron_flags:\n    - --a\n      - --b\n')
    expect(warn).toHaveBeenCalledTimes(3)

    // Indented content that is not a list item under the flags key.
    readDesktopLaunchConfig('desktop:\n  electron_flags:\n    nested: x\n')
    expect(warn).toHaveBeenCalledTimes(4)

    // A list in the NEXT top-level block is not this block's misconfiguration.
    readDesktopLaunchConfig('desktop:\n  electron_flags: []\nterminal:\n  something:\n    - x\n')
    expect(warn).toHaveBeenCalledTimes(4)

    warn.mockRestore()
  })

  it('#103288: reads desktop.ssh_path as plain, single-quoted, and YAML double-quoted scalars', () => {
    const read = (value: string) =>
      readDesktopLaunchConfig(['desktop:', `  ssh_path: ${value}`, 'terminal:', '  ssh_path: nope'].join('\n')).sshPath

    expect(read('C:\\Program Files\\Git\\usr\\bin\\ssh.exe')).toBe('C:\\Program Files\\Git\\usr\\bin\\ssh.exe')
    expect(read("'C:\\Git\\usr\\bin\\ssh.exe'  # Git's client")).toBe('C:\\Git\\usr\\bin\\ssh.exe')
    expect(read('"C:\\\\Git\\\\usr\\\\bin\\\\ssh.exe"')).toBe('C:\\Git\\usr\\bin\\ssh.exe')
    expect(read('""')).toBeUndefined()
    expect(readDesktopLaunchConfig('terminal:\n  ssh_path: C:\\x.exe').sshPath).toBeUndefined()
  })
})
