"""The image ships the Webapp renderer where the gateway serves it from.

The install root is read-only, so ``hermes webapp`` cannot build the bundle at
runtime: a gateway with ``webapp.enabled`` serves only what the build baked.
"""
from __future__ import annotations

import shlex
import subprocess


def test_webapp_bundle_is_baked_where_the_gateway_serves_it(built_image: str) -> None:
    py = (
        "from hermes_cli.config import get_project_root; "
        "from hermes_cli.webapp import webapp_bundle_stale, webapp_dist_dir; "
        "dist = webapp_dist_dir(get_project_root()); "
        "assert (dist / 'index.html').is_file(), dist; "
        "assert not webapp_bundle_stale(dist), dist"
    )
    inner = f"source /opt/hermes/.venv/bin/activate && cd /opt/hermes && python3 -c {shlex.quote(py)}"
    r = subprocess.run(
        ["docker", "run", "--rm", "--network=none", "--entrypoint", "su", built_image,
         "hermes", "-s", "/bin/bash", "-c", inner],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert r.returncode == 0, f"Webapp bundle missing from the image:\n{r.stderr[-2000:]}"
