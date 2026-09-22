import fs from 'node:fs';
import path from 'node:path';

/**
 * Resolve the installation-owned launcher used to finish a source update.
 *
 * Windows PM installs can contain two launchers during an update. A locked
 * executable can remain beside the current command file. Select the command
 * file explicitly because extensionless lookup would choose the executable.
 *
 * @param {string} root
 * @param {NodeJS.ProcessEnv | Record<string, string>} env
 * @param {NodeJS.Platform} platform
 * @returns {{launcher: string, command: string, args: string[], windowsVerbatimArguments: boolean}}
 */
export function sourceRuntimeSettleCommand(root, env, platform = process.platform) {
  const local = path.join(root, '.hermes', 'bin');
  const candidates = platform === 'win32'
    ? [path.join(local, 'hermes.cmd'), path.join(local, 'hermes.exe'),
      path.join(root, 'venv', 'Scripts', 'hermes.exe')]
    : [path.join(local, 'hermes'), path.join(root, 'venv', 'bin', 'hermes')];
  const launcher = candidates.find(candidate => fs.existsSync(candidate));
  if (!launcher) throw new Error(`No source launcher available to settle ${root}`);

  if (platform !== 'win32' || path.extname(launcher).toLowerCase() !== '.cmd') {
    return { launcher, command: launcher, args: ['status'], windowsVerbatimArguments: false };
  }
  if (launcher.includes('"')) throw new Error('Source launcher path contains an invalid quote');
  const command = env.ComSpec || env.COMSPEC
    || (env.SystemRoot ? path.join(env.SystemRoot, 'System32', 'cmd.exe') : 'cmd.exe');
  // /s applies cmd.exe's documented outer-quote stripping to this one command
  // string. The doubled outer quotes keep a launcher path containing spaces
  // intact while `status` remains a separate command-file argument.
  return { launcher, command, args: ['/d', '/s', '/c', `""${launcher}" status"`], windowsVerbatimArguments: true };
}
