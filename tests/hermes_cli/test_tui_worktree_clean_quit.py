"""N8: quitting ``hermes --tui -w`` removes a clean worktree (and its branch), as ``main`` did
before #106742; a dirty one, or one the gateway still has queued/started work in, is kept."""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

GIT = ['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid']


def _repo(tmp_path):
    repo = tmp_path / 'repo'
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(repo)], check=True)
    (repo / '.gitignore').write_text('.worktrees/\n')
    subprocess.run([*GIT, '-C', str(repo), 'add', '.gitignore'], check=True)
    subprocess.run([*GIT, '-C', str(repo), 'commit', '-qm', 'fixture'], check=True)
    return repo


def _branches(repo):
    return subprocess.run(['git', '-C', str(repo), 'branch', '--format=%(refname:short)'],
                          capture_output=True, text=True, check=True).stdout.split()


@pytest.mark.parametrize('case', ['clean', 'clean_with_history', 'dirty', 'pending_turn'])
def test_tui_worktree_quit_removes_only_a_clean_idle_tree(tmp_path, monkeypatch, case):
    from hermes_cli import main_tui_launch
    from hermes_cli.worktree_ops import _setup_worktree
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB

    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv('HERMES_PYTHON', sys.executable)
    wt_info = _setup_worktree(str(repo), sync_base=False, retain_db=get_hermes_home() / 'state.db')
    monkeypatch.setattr(main_tui_launch, '_setup_tui_worktree', lambda: wt_info)
    monkeypatch.setattr(main_tui_launch, '_make_tui_argv', lambda *_: (['node'], Path('.')))
    monkeypatch.setattr(main_tui_launch, '_print_tui_exit_summary', lambda *_: None)

    def ink(_argv, cwd=None, env=None):
        db_path = get_hermes_home() / 'state.db'
        db = SessionDB(db_path=db_path)
        db.create_session('tui-wt', 'tui', cwd=env['HERMES_TUI_CWD'])
        if case != 'clean':
            db.append_message('tui-wt', 'user', 'hello')
        db.close()
        if case == 'pending_turn':  # an accepted turn the gateway has not finished yet
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO session_admissions(admission_id,request_id,principal_id,target_session_id,"
                "lineage_json,payload_json,payload_digest,intent,status,outcome,owner_epoch,generation) "
                "VALUES('adm-1','req-1','p','tui-wt','[]','{}','d','queue','queued',NULL,1,1)")
            conn.commit()
            conn.close()
        if case == 'dirty':
            (Path(env['HERMES_TUI_CWD']) / 'work.txt').write_text('uncommitted')
        Path(env['HERMES_TUI_ACTIVE_SESSION_FILE']).write_text(json.dumps({'session_id': 'tui-wt'}))
        return 0

    monkeypatch.setattr(main_tui_launch.subprocess, 'call', ink)
    with pytest.raises(SystemExit) as exited:
        main_tui_launch._launch_tui(worktree=True)
    assert exited.value.code == 0
    tree = Path(wt_info['path'])
    if case in ('clean', 'clean_with_history'):
        assert not tree.exists()
        assert wt_info['branch'] not in _branches(repo)
    else:
        assert tree.is_dir()
        assert wt_info['branch'] in _branches(repo)
        if case == 'dirty':
            assert (tree / 'work.txt').read_text() == 'uncommitted'
