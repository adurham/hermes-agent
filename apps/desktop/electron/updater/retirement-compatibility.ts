import type { PayloadInfo } from '../payload-backend'

import { runRetirementCommand } from './retirement-native'
import type { RetirementSnapshotTools } from './retirement-preservation'
import type { RetirementRecord } from './retirement-state'

const compatibilityProbe: string = `import os, pathlib, sys
repo, site, root, profile = sys.argv[1:]
sys.path[:0] = [repo, site]
home = pathlib.Path(root)
if profile != 'default': home = home / 'profiles' / profile
os.environ['HERMES_HOME'] = str(home)
os.environ['HERMES_SHARED_AUTH_DIR'] = str(pathlib.Path(root) / 'shared')
import hermes_yaml
from hermes_cli.config import validate_config_structure
config_file = home / 'config.yaml'
config = hermes_yaml.safe_load(config_file.read_text(encoding='utf-8')) if config_file.exists() else {}
if not isinstance(config, dict): raise RuntimeError('Configuration must be a mapping')
issues = validate_config_structure(config)
if any(issue.severity == 'error' for issue in issues): raise RuntimeError('Snapshot configuration is incompatible; run hermes doctor in the preview')
from hermes_state import SessionDB
from hermes_state_schema import schema_read_probe_statements
file = home / 'state.db'
if file.exists():
    # Only the disposable snapshot receives the destination migrations. Never
    # initialize the live database as a compatibility probe.
    db = SessionDB(db_path=file)
    try:
        for sql in schema_read_probe_statements(): db._conn.execute(sql)
        for row in db._conn.execute('SELECT id FROM sessions').fetchall():
            db.get_session(row[0])
            db.get_messages(row[0])
    finally: db.close()
`

/** Exercise destination readers before any live migrations; not a universal compatibility proof. */
export async function probeRetirementSnapshot(record: RetirementRecord, payload: PayloadInfo): Promise<void> {
  await runRetirementCommand(payload.storePython, ['-c', compatibilityProbe,
    payload.repoDir, payload.sitePackages, record.state.snapshotHome, record.request.source.profile], 120_000)
}

export function retirementSnapshotTools(payload: PayloadInfo): RetirementSnapshotTools {
  return { python: payload.storePython, repo: payload.repoDir, sitePackages: payload.sitePackages }
}
