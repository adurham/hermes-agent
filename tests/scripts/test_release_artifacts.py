"""Native metadata and artifact publication use the same verified bytes."""
import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.bundles.release_artifacts import materialize, record, stamp_matches


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
    out = tmp_path / 'record.tar'
    record('windows', 'x64', root, tag, commit, out)
    with tarfile.open(out) as archive:
        metadata = json.load(archive.extractfile('metadata-windows-x64.json'))
        assert metadata['identity'] == 'Product'
        assert metadata['version'] == '1.2.3.0'
        assert metadata['publisher'] == 'CN=Test'
        assert metadata['applicationId'] == 'App'
        assert archive.extractfile(package.name).read() == package.read_bytes()
    write_package('b' * 40)
    with pytest.raises(ValueError, match='provenance'):
        record('windows', 'x64', root, tag, commit, tmp_path / 'bad.tar')
    with pytest.raises(ValueError, match='provenance'):
        stamp_matches({}, tag, commit)


def test_materialize_validates_the_published_file_receipt_before_using_bytes(tmp_path, monkeypatch):
    base, tag, commit = 'https://releases.example', 'v1.2.3', 'a' * 40
    data = b'package transport fixture, not native signing proof'
    digest = hashlib.sha256(data).hexdigest()
    files, packages = [], []
    for platform in ('windows', 'macos'):
        for arch in ('x64', 'arm64'):
            filename = f'{platform}-{arch}.' + ('msixbundle' if platform == 'windows' else 'zip')
            url = f'{base}/releases/tag/{tag}/{filename}'
            files.append({'path': filename, 'url': url, 'sha256': digest})
            packages.append({'platform': platform, 'arch': arch, 'identity': 'Product', 'tag': tag, 'commit': commit,
                             'version': '1.2.3.0' if platform == 'windows' else '1.2.3',
                             **({'publisher': 'CN=Test', 'applicationId': 'App'} if platform == 'windows' else {'teamId': 'ABCDEFGHIJ'}),
                             'artifact': {'url': url, 'sha256': digest}})
    manifest = {'schema': 1, 'tag': tag, 'commit': commit, 'packages': packages, 'files': files}
    class Response(io.BytesIO):
        def geturl(self):
            return base + "/package"

    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **kw: Response(data))
    materialize(manifest, tmp_path / 'good', public_base=base)
    assert all((tmp_path / 'good' / f['path']).read_bytes() == data for f in files)
    with pytest.raises(ValueError, match='one Store candidate'):
        materialize(manifest, tmp_path / 'store-missing', public_base=base, store_only=True)
    store = {'path': 'Store-App.msixbundle', 'url': f'{base}/releases/tag/{tag}/Store-App.msixbundle', 'sha256': digest}
    files.append(store)
    materialize(manifest, tmp_path / 'store', public_base=base, store_only=True)
    assert [p.name for p in (tmp_path / 'store').iterdir()] == [store['path']]
    files[0]['sha256'] = 'b' * 64
    with pytest.raises(ValueError, match='receipts differ'):
        materialize(manifest, tmp_path / 'bad', public_base=base)
    files[0]['sha256'] = digest
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **kw: Response(b'changed bytes'))
    with pytest.raises(ValueError, match='digest mismatch'):
        materialize(manifest, tmp_path / 'corrupt', public_base=base)
