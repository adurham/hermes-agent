"""Native metadata and artifact publication use the same verified bytes."""
import hashlib
import copy
import json
import zipfile
import xml.etree.ElementTree as ET

import pytest

from scripts.bundles.release_artifacts import materialize, record, stamp_matches
from tests.scripts.test_release_r2 import r2_server  # noqa: F401
from tests.scripts.test_stable_release import https_origin  # noqa: F401
from tests.scripts.test_release_darwin import _inputs
from scripts.bundles import release_artifacts as artifacts


def test_windows_metadata_is_read_from_package_and_stale_stamp_is_rejected(tmp_path):
    tag, commit = 'v1.2.3', 'a' * 40
    root = tmp_path / 'release'
    root.mkdir()
    package = root / 'Product-win-x64.msix'
    manifest = '<Package xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10"><Identity Name="Product" Publisher="CN=Test" Version="1.2.3.0" ProcessorArchitecture="x64"/><Applications><Application Id="App"/></Applications></Package>'

    def write_package(sha):
        with zipfile.ZipFile(package, 'w') as archive:
            archive.writestr('AppxManifest.xml', manifest)
            archive.writestr('app/resources/install-stamp.json', json.dumps({'tag': tag, 'commit': sha}))

    write_package(commit)
    out = root / 'metadata-windows-x64.json'
    original = package.read_bytes()
    record('windows', 'x64', root, tag, commit, out)
    metadata = json.loads(out.read_text(encoding='utf-8'))
    assert metadata['identity'] == 'Product'
    assert metadata['version'] == '1.2.3.0'
    assert metadata['publisher'] == 'CN=Test'
    assert metadata['applicationId'] == 'App'
    assert package.read_bytes() == original
    write_package('b' * 40)
    with pytest.raises(ValueError, match='provenance'):
        record('windows', 'x64', root, tag, commit, tmp_path / 'bad.json')
    with pytest.raises(ValueError, match='provenance'):
        stamp_matches({}, tag, commit)


@pytest.fixture
def staged_candidate(tmp_path, r2_server, https_origin):
    from scripts.bundles.release_artifacts import assemble
    from scripts.releases import handoff

    tag, commit, base = 'v1.2.3', 'a' * 40, https_origin.base
    https_origin.store = r2_server.store
    legs, mac_bytes = _inputs('1.2.3')
    built = tmp_path / 'built'
    built.mkdir()
    for platform, arches in [('windows', ('x64', 'arm64')), ('macos', ('x64', 'arm64')), ('termux', ('aarch64',))]:
        for arch in arches:
            row = {'platform': platform, 'arch': arch, 'tag': tag, 'commit': commit, 'identity': 'Product'}
            if platform == 'windows':
                row.update(version='1.2.3.0', publisher='CN=Test', applicationId='App')
                package = f'Product-win-{arch}.msix'
                handoff_name = f'win32-{arch}'
            elif platform == 'macos':
                package = f'HermesBundled-1.2.3-mac-{arch}.zip'
                row.update(version='1.2.3', teamId='ABCDEFGHIJ', filename=package)
                handoff_name = f'darwin-{arch}'
            else:
                package = 'deb/product.deb'
                row.update(version='1.2.3-1', filename=package)
                handoff_name = 'termux'
            file = built / package
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(mac_bytes.get(f'releases/tag/{tag}/{package}', b'package transport fixture'))
            metadata = built / f'metadata-{platform}-{arch}.json'
            metadata.write_text(json.dumps(row), encoding='utf-8')
            includes = [package, metadata.name]
            if platform == 'macos':
                feed = f'{arch}-stable-mac.yml'
                (built / feed).write_text(legs[feed], encoding='utf-8')
                includes.append(feed)
            if platform == 'termux':
                for name in ('pool/package.deb', 'dists/hermes-stable/InRelease', 'dists/hermes-stable/Release'):
                    path = built / 'apt' / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(f'index transport fixture: {name}'.encode())
                includes.append('apt/**/*')
            handoff.stage(tag, commit, handoff_name, built, includes)
    bundle = built / 'Product-win.msixbundle'
    with zipfile.ZipFile(bundle, 'w') as archive:
        archive.writestr('AppxMetadata/AppxBundleManifest.xml', '<Bundle><Identity Name="Product" Publisher="CN=Test" Version="1.2.3.0"/></Bundle>')
    (built / 'Store-Product-win.msixbundle').write_bytes(b'Store bundle transport fixture')
    handoff.stage(tag, commit, 'windows-universal', built, ['*.msixbundle'])
    fetched = tmp_path / 'fetched'
    names = ['win32-x64', 'win32-arm64', 'darwin-x64', 'darwin-arm64', 'termux', 'windows-universal']
    handoff.fetch(tag, commit, names, fetched, ['metadata-*.json', '*.msixbundle'])
    r2_server.requests.clear()
    manifest = assemble(fetched, tag, commit, base, tmp_path / 'release-candidates.json')
    assert {row['platform'] + '/' + row['arch'] for row in manifest['packages']} == {
        'windows/x64', 'windows/arm64', 'macos/x64', 'macos/arm64', 'termux/aarch64'}
    assert all(not file['path'].startswith(('handoff-', 'metadata-')) for file in manifest['files'])
    puts = [path for method, path, _ in r2_server.requests if method == 'PUT']
    assert puts == [f'/hermes-releases/releases/tag/{tag}/release-candidates.json']
    assert all(key.startswith(f'releases/tag/{tag}/') for key in r2_server.store)
    return manifest, fetched, base


def test_assemble_rejects_missing_and_changed_receipts(tmp_path, staged_candidate):
    from scripts.bundles.release_artifacts import assemble

    manifest, fetched, base = staged_candidate
    tag, commit = manifest['tag'], manifest['commit']
    receipt = fetched / 'handoff-darwin-arm64.json'
    original = receipt.read_bytes()
    receipt.unlink()
    with pytest.raises(ValueError, match='handoff'):
        assemble(fetched, tag, commit, base, tmp_path / 'missing.json')
    receipt.write_bytes(original)
    (fetched / 'metadata-windows-x64.json').write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='digest'):
        assemble(fetched, tag, commit, base, tmp_path / 'changed.json')


def test_candidate_publication_and_store_selection(tmp_path, monkeypatch, r2_server, staged_candidate):
    manifest, _, base = staged_candidate
    tag, commit = manifest['tag'], manifest['commit']
    raw = r2_server.store[f'releases/tag/{tag}/release-candidates.json'][0]
    args = ['--tag', tag, '--commit', commit, '--public-base', base]
    monkeypatch.setenv('CANDIDATE_MANIFEST_SHA256', hashlib.sha256(raw).hexdigest())
    artifacts.main(['materialize', *args, '--root', str(tmp_path / 'store'), '--store-only'])
    assert [p.name for p in (tmp_path / 'store').iterdir()] == ['Store-Product-win.msixbundle']
    assert (tmp_path / 'store/Store-Product-win.msixbundle').read_bytes() == b'Store bundle transport fixture'
    monkeypatch.setenv('CANDIDATE_MANIFEST_SHA256', 'f' * 64)
    with pytest.raises(ValueError, match='digest mismatch'):
        artifacts.main(['materialize', *args, '--root', str(tmp_path / 'wrong')])
    assert not (tmp_path / 'wrong').exists()
    missing = copy.deepcopy(manifest)
    missing['files'] = [f for f in missing['files'] if not f['path'].startswith('Store-')]
    with pytest.raises(ValueError, match='one Store candidate'):
        materialize(missing, tmp_path / 'missing-store', public_base=base, store_only=True)
    changed = copy.deepcopy(manifest)
    changed['files'][0]['sha256'] = 'b' * 64
    with pytest.raises(ValueError, match='receipts differ'):
        materialize(changed, tmp_path / 'bad', public_base=base)

    r2_server.requests.clear()
    artifacts.publish(manifest, tmp_path / 'publish', base)
    assert [path for method, path, _ in r2_server.requests if method == 'PUT'] == [
        '/hermes-releases/releases/termux/stable/pool/package.deb']
    r2_server.requests.clear()
    artifacts.promote(manifest, tmp_path / 'promote', base)
    puts = [path for method, path, _ in r2_server.requests if method == 'PUT']
    assert puts == ['/hermes-releases/' + name for name in (
        'releases/darwin/stable/stable-mac.yml', 'releases/win32/stable/stable.appinstaller',
        'releases/termux/stable/dists/hermes-stable/Release', 'releases/termux/stable/dists/hermes-stable/InRelease')]
    for item in manifest['files']:
        assert (tmp_path / 'promote' / item['path']).read_bytes() == r2_server.store[f'releases/tag/{tag}/{item["path"]}'][0]
    descriptor = ET.fromstring(r2_server.store['releases/win32/stable/stable.appinstaller'][0])
    assert descriptor.attrib == {'Uri': base + '/releases/win32/stable/stable.appinstaller', 'Version': '1.2.3.0'}
    assert descriptor.find('{*}MainBundle').attrib == {
        'Name': 'Product', 'Publisher': 'CN=Test', 'Version': '1.2.3.0',
        'Uri': base + '/releases/tag/v1.2.3/Product-win.msixbundle'}
    pointer = 'releases/win32/stable/stable.appinstaller'
    original = r2_server.store[pointer]
    r2_server.corrupt_put = pointer
    r2_server.requests.clear()
    with pytest.raises(ValueError, match='Channel read-back differs'):
        artifacts.promote(manifest, tmp_path / 'bad-readback', base)
    assert [path for method, path, _ in r2_server.requests if method == 'PUT'] == ['/hermes-releases/' + pointer]
    r2_server.corrupt_put = None
    r2_server.store[pointer] = original
    before = dict(r2_server.store)
    r2_server.store[f'releases/tag/{tag}/Product-win.msixbundle'] = (b'corrupt', '"e"')
    r2_server.requests.clear()
    with pytest.raises(ValueError, match='digest mismatch'):
        artifacts.promote(manifest, tmp_path / 'broken', base)
    assert not any(method == 'PUT' for method, _, _ in r2_server.requests)
    assert all(value == r2_server.store[key] for key, value in before.items() if not key.startswith('releases/tag/'))
