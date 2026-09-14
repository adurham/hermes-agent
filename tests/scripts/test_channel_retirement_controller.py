"""Loopback coordinator exercise, not native installation or release evidence."""
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import sys
from threading import Thread
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("coordinator", ROOT / "tests/install/channel-retirement-controller.py")
assert spec is not None and spec.loader is not None
coordinator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coordinator)
from hermes_cli.release_channels import ChannelError, canonical_json  # noqa: E402
from scripts.releases.channel_disposable import allocate_receivers  # noqa: E402
from scripts.releases.channels import ChannelPublisher, R2ChannelStore  # noqa: E402
from scripts.releases.r2_scope import R2Scope  # noqa: E402


@contextmanager
def archive():
    objects = {}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            body = objects.get(self.path)
            self.send_response(404 if body is None else 200)
            if body is not None:
                self.send_header("ETag", hashlib.sha256(body).hexdigest())
            self.end_headers()
            self.wfile.write(body or b"")

        def do_PUT(self):
            old = objects.get(self.path)
            expected = hashlib.sha256(old).hexdigest() if old is not None else None
            if ((self.headers.get("If-None-Match") and old is not None)
                    or (self.headers.get("If-Match") and self.headers["If-Match"] != expected)):
                self.send_response(412)
            else:
                objects[self.path] = self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
            self.end_headers()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", objects
    finally:
        server.shutdown(); server.server_close(); thread.join()


class CoordinatorTest(unittest.TestCase):
    def test_scoped_lifecycle_uses_real_retire_and_stable_descriptor(self):
        for platform in ("darwin", "win32"):
            for route in ("direct", "via-update"):
                with self.subTest(platform=platform, route=route), archive() as (url, objects):
                    scope = R2Scope("ci-disposable/1/2-1/")
                    base = scope.public_base(url + "/bucket")
                    store = R2ChannelStore({"access_key_id": "fixture", "secret_key": "fixture"}, url, "bucket", scope=scope)
                    pub = ChannelPublisher(store, "example/hermes", base, authorize=lambda *_: None)
                    pub.create("preview")
                    requests = {name: pub.allocate("preview", "a" * 40, "1.0.0") for name in ("A", "B")}
                    requests.update(allocate_receivers(pub, "a" * 40, "1.0.0", "f" * 40))
                    journey: dict = {"repository": pub.repository, "controllerCommit": "f" * 40, "route": route,
                               "platform": platform, "minimumVersion": "1.0.0"}
                    for name, request in requests.items():
                        prefix = f"releases/channel-builds/{request['buildId']}/"
                        key = prefix + ("stable-mac.yml" if platform == "darwin" else "stable.appinstaller")
                        pkg = {"platform": platform, "arch": "arm64", "variant": "bundled",
                               "identity": request["identity"]["appId" if platform == "darwin" else "msixAppIdWithOrg"],
                               "version": request["version" if platform == "darwin" else "windowsVersion"],
                               "teamId": "ABCDEFGHIJ", "publisher": "CN=Fixture",
                               "artifact": {"key": prefix + "artifact.zip", "sha256": "a" * 64, "size": 1},
                               "feed": {"key": key, "channel": "stable"}}
                        manifest = {"schema": 1, "request": request, "receiverProtocol": 1, "packages": [pkg]}
                        head = {"buildId": request["buildId"], "sequence": request["sequence"], "manifestKey": prefix + "build.json",
                                "sha256": hashlib.sha256(canonical_json(manifest)).hexdigest()}
                        pub._write(head["manifestKey"], manifest)
                        pub._write(key, {"version": request["version"], "files": [{"url": base + "/" + pkg["artifact"]["key"]}]})
                        journey[name] = {"head": head, "manifest": manifest}
                    for channel, slot in (("preview", "A"), ("stable", "S")):
                        current = pub._read(channel)
                        assert current is not None
                        record, etag = current
                        pub._write(f"releases/channels/{channel}.json", {**record, "head": journey[slot]["head"]}, etag)
                    if route == "direct":
                        del journey["B"]
                    with patch.dict(os.environ, {"GITHUB_SHA": "f" * 40}):
                        coordinator.prepare(pub, journey)
                    if route == "via-update":
                        advanced = coordinator.advance(pub, journey, "preview-update")
                        self.assertEqual(coordinator.advance(pub, journey, "preview-update"), advanced)
                        with patch.dict(os.environ, {"GITHUB_SHA": "f" * 40}):
                            coordinator.prepare(pub, journey)
                    retired = coordinator.advance(pub, journey, "retire")
                    self.assertEqual(retired["destinationHead"], journey["S"]["head"])
                    coordinator.advance(pub, journey, "stable-update")
                    self.assertEqual(pub.reader.resolve("stable").manifest, journey["T"]["manifest"])
                    self.assertEqual(pub.reader.resolve("preview").manifest, journey["S"]["manifest"])
                    feed_key = f"releases/{platform}/stable/" + ("stable-mac.yml" if platform == "darwin" else "stable.appinstaller")
                    feed = pub.reader.read_bytes(feed_key).decode()
                    self.assertIn(journey["T"]["manifest"]["request"]["version"], feed)
                    self.assertIn(base + "/", feed)
                    before = dict(objects)
                    with self.assertRaises(ChannelError):
                        coordinator.advance(pub, journey, "retire")
                    self.assertEqual(objects, before)
                    self.assertTrue(all(key.startswith("/bucket/" + scope.prefix) for key in objects))
                    store.scope = R2Scope()
                    with self.assertRaisesRegex(ValueError, "disposable"):
                        coordinator.advance(pub, journey, "stable-update")


if __name__ == "__main__":
    unittest.main()
