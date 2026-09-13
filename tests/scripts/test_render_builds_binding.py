"""Commit rows bind a literal producer's receipt, not merely object names."""
import pytest

from tests.scripts.test_render_builds_publication import rbt

SHA = 'a' * 40
PACKAGE = 'HermesBundled-1.2.3-win-x64.msix'
OTHER = 'HermesBundled-1.2.4-win-x64.msix'
PREFIX = f'releases/commit/{SHA}/'


@pytest.mark.parametrize('listed,objects,state,diagnostic', [
    (None, [PACKAGE], 'receipt-missing', 'leg incomplete or upload interrupted'),
    (['metadata-windows-x64.json'], [PACKAGE], 'receipt-omits', 'artifact absent from receipt'),
    ([PACKAGE], [], 'object-missing', 'receipt present but object missing'),
    ([PACKAGE, OTHER], [PACKAGE, OTHER], 'ambiguous', 'ambiguous: multiple objects match'),
    ([PACKAGE], [PACKAGE, OTHER], 'built', '✅ Built'),
])
def test_commit_row_binding_faults(listed, objects, state, diagnostic):
    receipt = None if listed is None else {
        'schema': 2, 'commit': SHA, 'name': 'win32-x64',
        'files': [{'path': path, 'size': 1, 'sha256': '0' * 64} for path in listed],
    }
    receipts = {'win32-x64': receipt}
    keys = [PREFIX + path for path in objects]
    row = next(row for row in rbt.commit_expected_rows(keys, receipts)
               if row['label'] == 'Windows x64 (MSIX)')
    assert row == {'label': 'Windows x64 (MSIX)', 'leg': 'win32-x64',
                   'state': state, 'key': PREFIX + PACKAGE if state == 'built' else None}
    summary = rbt.render_commit_summary(keys, 'https://cdn.example', SHA, receipts)
    page = rbt.render_commit_page(SHA, keys, 'https://cdn.example', receipts)
    for output in (summary, page):
        assert diagnostic in output
        assert ('https://cdn.example/' + PREFIX + PACKAGE in output) == (state == 'built')
        assert 'Store' not in output
    assert rbt.recorded_build(page) == SHA
    assert 'Bundle environment' not in page


@pytest.mark.parametrize('needs,expected', [
    (None, []), ('', []), ('not json', []),
    ('{"build-win32":{"result":"success"},"build-darwin":{"result":"failure"},'
     '"termux-deb":{"result":"skipped"},"build-linux":{"result":"failure"}}',
     ['build-darwin', 'build-linux']),
])
def test_failed_legs_from_release_needs(needs, expected):
    assert rbt.failed_legs_from_release_needs(needs) == expected
