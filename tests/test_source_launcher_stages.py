"""Bootstrap stages publish launchers through the shared writer after PM setup."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess

import pytest

from hermes_cli.runtime_paths import install_state_dir, site_packages
from pm.lock import Lockfile
from pm.store import current_target
from tests.hermes_cli.test_source_launcher_publication import fixture_tree

ROOT = Path(__file__).resolve().parents[1]


def bootstrap_inputs(repo):
    # Both pre-Python readers need the real lock shape, including artifact
    # objects (an empty inline {} does not exercise their brace tracking).
    (repo / 'pm').mkdir()
    for name in ('lock.json', 'artifact-mirror.json'):
        shutil.copy2(ROOT / 'pm' / name, repo / 'pm' / name)
    lock = Lockfile(repo / 'pm/lock.json')
    uv_version, py_version = lock.version('uv'), lock.version('python')
    assert uv_version and py_version
    return uv_version, '.'.join(py_version.split('+')[0].split('.')[:2])


def shell_bootstrap(repo, home, interpreter):
    uv_version, py_version = bootstrap_inputs(repo)
    entry = home / 'tools' / f'uv-{uv_version}-{current_target()}'
    entry.mkdir()
    calls = home / 'uv-calls'
    uv = entry / ('uv.exe' if os.name == 'nt' else 'uv')
    # Only acquisition is replaced: the real stages still parse the pin, run
    # bootstrap Python, and invoke the unmodified shared launcher writer.
    uv.write_text(
        '#!/usr/bin/env bash\nset -eu\n'
        f'printf \'%s\\n\' "$*" >> {shlex.quote(calls.as_posix())}\n'
        'case "$*" in\n'
        f'  --version) printf \'%s\\n\' "uv {uv_version}" ;;\n'
        f'  "python install --no-bin {py_version}") test -x {shlex.quote(interpreter.as_posix())} ;;\n'
        f'  "python find --managed-python {py_version}") printf \'%s\\n\' {shlex.quote(interpreter.as_posix())} ;;\n'
        '  *) printf \'unexpected bootstrap uv call: %s\\n\' "$*" >&2; exit 91 ;;\n'
        'esac\n', encoding='utf-8',
    )
    uv.chmod(0o755)
    # Fail closed if a fixture regression tries the installer's download path.
    curl = entry / 'curl'
    curl.write_text('#!/usr/bin/env bash\necho "unexpected bootstrap download" >&2\nexit 91\n',
                    encoding='utf-8')
    curl.chmod(0o755)
    return entry, calls, [f'python install --no-bin {py_version}',
                          f'python find --managed-python {py_version}']


def selected_environment(repo, value=11):
    selected = install_state_dir(repo) / 'environments/ready/venv'
    site = site_packages(selected)
    site.mkdir(parents=True)
    (selected / 'pyvenv.cfg').write_text('home = fixture\n', encoding='utf-8')
    (site / 'selected_probe.py').write_text(f'VALUE = {value}\n', encoding='utf-8')
    (install_state_dir(repo) / 'facts.json').write_text(
        json.dumps({'packages': {'venv': {'environment': str(selected)}}}), encoding='utf-8')


@pytest.mark.platforms("windows", "posix")
def test_developer_setup_publishes_without_a_checkout_venv(tmp_path, monkeypatch):
    repo, home, interpreter = fixture_tree(tmp_path, monkeypatch)
    shutil.copy2(ROOT / 'setup-hermes.sh', repo / 'setup-hermes.sh')
    store = home / 'tools'
    entry, calls, expected_calls = shell_bootstrap(repo, home, interpreter)
    (repo / 'pm/__init__.py').write_text('', encoding='utf-8')
    # Stop at PM's install boundary; the launcher writer and boot selection stay real.
    receipt = repo / 'pm-install.json'
    (repo / 'pm/cli.py').write_text(
        'import json, pathlib, sys\n'
        "assert sys.argv[1:] == ['install'], sys.argv\n"
        f'pathlib.Path({str(receipt)!r}).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")\n',
        encoding='utf-8',
    )
    selected_environment(repo)
    env = dict(os.environ, HOME=str(tmp_path / 'shell-home'), HERMES_HOME=str(home),
               HERMES_RUNTIME_DIR=str(store), UV_OFFLINE='1', UV_PYTHON_DOWNLOADS='never',
               PATH=os.pathsep.join([str(entry), os.environ['PATH']]))
    env.pop('UV_PYTHON_INSTALL_DIR', None)
    env.pop('PYTHONHOME', None)
    env.pop('PYTHONPATH', None)
    Path(env['HOME']).mkdir()
    result = subprocess.run(['bash', str(repo / 'setup-hermes.sh')], cwd=tmp_path, env=env,
                            capture_output=True, timeout=90)
    if result.returncode:
        print(result.stdout.decode('utf-8', errors='replace'))
        print(result.stderr.decode('utf-8', errors='backslashreplace'))
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls.read_text(encoding='utf-8').splitlines() == ['--version', *expected_calls]
    assert json.loads(receipt.read_text(encoding='utf-8')) == ['install']
    out = home / 'bin' if os.name == 'nt' else Path(env['HOME']) / '.local/bin'
    launchers = list(out.glob('hermes*')) if out.exists() else []
    assert launchers, result.stdout + result.stderr
    command = out / ('hermes.exe' if (out / 'hermes.exe').is_file() else 'hermes.cmd' if os.name == 'nt' else 'hermes')
    child_env = dict(env)
    child_env.pop('HERMES_HOME')
    child_env.pop('HERMES_RUNTIME_DIR')
    child = subprocess.run([str(command), 'literal input'], cwd=tmp_path, env=child_env,
                           capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert child.returncode == 7, child.stdout + child.stderr
    witness = json.loads(child.stdout)
    assert witness['value'] == 11 and witness['argv'] == ['literal input']
    assert Path(witness['home']) == home and Path(witness['exe']).samefile(interpreter)
    assert not (repo / 'venv').exists()


@pytest.mark.platforms("windows", "posix")
def test_shell_installer_path_stage_uses_shared_publication(tmp_path, monkeypatch):
    repo, home, interpreter = fixture_tree(tmp_path, monkeypatch)
    entry, calls, expected_calls = shell_bootstrap(repo, home, interpreter)
    selected_environment(repo)
    shell_home = tmp_path / 'shell-home'
    shell_home.mkdir()
    env = dict(os.environ, PROBE_SCRIPT=(ROOT / 'scripts/install.sh').as_posix(),
               PROBE_REPO=repo.as_posix(), PROBE_HOME=home.as_posix(),
               PROBE_SHELL_HOME=shell_home.as_posix(), UV_OFFLINE='1', UV_PYTHON_DOWNLOADS='never',
               PATH=os.pathsep.join([str(entry), os.environ['PATH']]))
    env.pop('UV_PYTHON_INSTALL_DIR', None)
    script = '''source "$PROBE_SCRIPT" --manifest --dir "$PROBE_REPO" --hermes-home "$PROBE_HOME"
HOME="$PROBE_SHELL_HOME"
JSON=true
run_stage path
'''
    # Source the stage to test its transport on Windows without pretending it is POSIX.
    result = subprocess.run(['bash', '-c', script], cwd=tmp_path, env=env,
                            capture_output=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls.read_text(encoding='utf-8').splitlines() == expected_calls
    frames = [json.loads(line) for line in result.stdout.splitlines() if line.startswith(b'{')]
    assert frames == [{'ok': True, 'stage': 'path', 'skipped': False}]
    out = shell_home / '.local/bin'
    for name in ('hermes', 'hermes-acp'):
        command = out / (name + '.exe' if (out / (name + '.exe')).is_file()
                         else name + '.cmd' if os.name == 'nt' else name)
        child_env = dict(env)
        child_env.pop('HERMES_HOME', None)
        child = subprocess.run([str(command), 'from-stage'], cwd=tmp_path, env=child_env,
                               capture_output=True, text=True, encoding='utf-8', timeout=30)
        assert child.returncode == 7, child.stdout + child.stderr
        witness = json.loads(child.stdout)
        assert witness['value'] == 11 and witness['argv'] == ['from-stage']
        assert Path(witness['home']) == home and Path(witness['exe']).samefile(interpreter)
    assert not (repo / 'venv').exists()


@pytest.mark.platforms("windows")
def test_powershell_stage_publishes_without_a_checkout_venv(tmp_path, monkeypatch):
    repo, home, interpreter = fixture_tree(tmp_path, monkeypatch)
    _, py_version = bootstrap_inputs(repo)
    selected_environment(repo)
    calls = home / 'uv-calls'
    wrapper = tmp_path / 'stage.ps1'
    wrapper.write_text('''$ErrorActionPreference = 'Stop'
. $env:PROBE_INSTALLER -InstallDir $env:PROBE_REPO -HermesHome $env:PROBE_HOME
Initialize-ResolvedPaths
# Replace acquisition only; Get-BootstrapPython and Stage-Path stay real.
function Get-Uv { return 'Invoke-FixtureUv' }
function Invoke-FixtureUv {
    $call = $args -join ' '
    Add-Content -LiteralPath $env:PROBE_UV_CALLS -Encoding UTF8 -Value $call
    switch -Exact ($call) {
        "python install --no-bin $env:PROBE_PY_VERSION" {
            if (-not (Test-Path -LiteralPath $env:PROBE_PYTHON -PathType Leaf)) { throw 'missing fixture Python' }
        }
        "python find --managed-python --no-project $env:PROBE_PY_VERSION" {
            Write-Output $env:PROBE_PYTHON
        }
        default { throw "unexpected bootstrap uv call: $call" }
    }
    $global:LASTEXITCODE = 0
}
function Invoke-WebRequest { throw 'unexpected bootstrap download' }
# Replace only the registry publication edge, never mutate the actual user PATH.
function Set-LauncherUserPath([string]$binDir) {
    if ($binDir -ne (Join-Path $env:PROBE_HOME 'bin')) { throw 'wrong user PATH target' }
    $script:publishedPath = $binDir
    Write-Output 'REACHED_PATH_PUBLICATION'
}
Stage-Path
if (-not $script:publishedPath) { throw 'registry-publication seam was bypassed' }
exit 0
''', encoding='utf-8-sig')
    powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    env = dict(os.environ, PROBE_INSTALLER=str(ROOT / 'scripts/install.ps1'),
               PROBE_REPO=str(repo), PROBE_HOME=str(home), HERMES_HOME=str(tmp_path / 'other-home'),
               PROBE_PY_VERSION=py_version, PROBE_PYTHON=str(interpreter), PROBE_UV_CALLS=str(calls),
               UV_OFFLINE='1', UV_PYTHON_DOWNLOADS='never')
    env['PATH'] = os.pathsep.join([str(powershell.parent), str(Path(os.environ['SystemRoot']) / 'System32')])
    env['PATHEXT'] = '.COM;.EXE;.BAT;.CMD'
    env.pop('UV_PYTHON_INSTALL_DIR', None)
    result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(wrapper)],
                            cwd=tmp_path, env=env, capture_output=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert b'REACHED_PATH_PUBLICATION' in result.stdout
    assert calls.read_text(encoding='utf-8-sig').splitlines() == [
        f'python install --no-bin {py_version}',
        f'python find --managed-python --no-project {py_version}',
    ]
    for name in ('hermes', 'hermes-acp'):
        command = home / 'bin' / (name + ('.exe' if (home / 'bin' / (name + '.exe')).is_file() else '.cmd'))
        child_env = dict(env)
        child_env.pop('HERMES_HOME', None)
        child = subprocess.run([str(command), 'from-powershell'], cwd=tmp_path, env=child_env,
                               capture_output=True, text=True, encoding='utf-8', timeout=30)
        assert child.returncode == 7, child.stdout + child.stderr
        witness = json.loads(child.stdout)
        assert witness['value'] == 11 and witness['argv'] == ['from-powershell']
        assert Path(witness['home']) == home and Path(witness['exe']).samefile(interpreter)
    assert not (repo / 'venv').exists()
