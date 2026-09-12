import assert from 'node:assert/strict'
import { type ChildProcess, spawn } from 'node:child_process'
import { once } from 'node:events'

import { test } from 'vitest'

import { stopBackendChild, waitForBackendExit } from './backend-child'
import { createLocalBackendLifecycle } from './local-backend-lifecycle'
import { releaseLocalBackendSlotAfterExit } from './pool-spawn-coordinator'
import { createPoolStopper } from './pool-stop'

test.skipIf(process.platform === 'win32')(
  'eviction, update and quit share physical exit before releasing a slot',
  async (): Promise<void> => {
    let signals = 0
    let released = false

    const physical = {
      forceKillProcessTree: (): never => {
        throw new Error('POSIX test')
      }
    }

    const lifecycle = createLocalBackendLifecycle<ChildProcess>({
      cancelSetup: (): void => {},
      stopChild: (child: ChildProcess): void => {
        signals++
        stopBackendChild(child, physical)
      },
      waitForExit: (child: ChildProcess): Promise<void> => waitForBackendExit(child, physical)
    })

    const child = lifecycle.spawn((): ChildProcess =>
      spawn(
        process.execPath,
        [
          '-e',
          `
    process.on('SIGTERM', () => process.send('stopping'));
    process.on('message', () => process.exit(0));
    setInterval(() => {}, 1000);
    process.send('ready');
  `
        ],
        { detached: true, stdio: ['ignore', 'ignore', 'ignore', 'ipc'] }
      )
    )

    child.once('exit', (): boolean => lifecycle.release(child))

    const deps = {
      pool: new Map([['profile', { process: child }]]),
      stopChild: lifecycle.stop
    }

    const pool = createPoolStopper(deps)

    try {
      await once(child, 'message')
      const signalled = once(child, 'message')

      const eviction = releaseLocalBackendSlotAfterExit(
        (): void => {
          released = true
        },
        (): Promise<void> => pool.stop('profile')
      )

      const update = lifecycle.stop(child)
      const quit = lifecycle.shutdown()
      await signalled
      assert.equal(signals, 1)
      assert.equal(released, false, 'a pool slot must remain occupied while its process is alive')
      assert.ok(pool.inFlight('profile'))
      assert.throws((): ChildProcess => lifecycle.spawn((): ChildProcess => child))
      child.send('exit')
      await Promise.all([eviction, update, quit])
      assert.equal(child.exitCode, 0)
      assert.equal(released, true)
      assert.equal(pool.inFlight('profile'), undefined)
      assert.equal(lifecycle.hasPending(), false)
    } finally {
      if (child.exitCode === null && child.signalCode === null) {
        child.kill('SIGKILL')
        await once(child, 'exit')
      }
    }
  }
)
