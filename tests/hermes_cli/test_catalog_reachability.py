"""Catalog reachability: every entry's files must exist upstream with the
size the catalog claims (sizes feed the fit estimator and the download
progress bar, and a silent upstream re-upload that changes file size means
the fit math is stale).

Network-marked (skipped in hermetic CI unless explicitly enabled) — this is
the test that catches wrong repo names (the Nemotron 401), moved files, and
re-uploads that resize files. Run before any catalog commit:

    HERMES_TEST_NETWORK=1 scripts/run_tests.sh tests/hermes_cli/test_catalog_reachability.py
"""

from __future__ import annotations

import json
import os
import urllib.request

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("HERMES_TEST_NETWORK"),
    reason="network test; set HERMES_TEST_NETWORK=1 to run",
)


def test_every_catalog_file_resolves():
    from hermes_cli.local_runtime.catalog import CATALOG

    problems = []
    for entry in CATALOG:
        url = f"https://huggingface.co/api/models/{entry.repo}/tree/main?recursive=true"
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                files = {f["path"]: f.get("size") for f in json.load(r)}
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{entry.id}: repo {entry.repo} unreachable ({exc})")
            continue
        for variant in entry.variants:
            for asset in entry.download_files(variant):
                if asset.path not in files:
                    problems.append(
                        f"{entry.id}/{variant.quant}: {asset.path} not in {entry.repo}")
                    continue
                live_size = files[asset.path]
                if live_size and live_size != asset.size_bytes:
                    problems.append(
                        f"{entry.id}/{variant.quant}: size drift on {asset.path} — "
                        f"catalog {asset.size_bytes} vs live {live_size} "
                        f"(upstream re-uploaded; refresh the size deliberately)")
    assert not problems, "\n".join(problems)
