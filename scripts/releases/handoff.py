"""Transfer immutable release files through R2 without publishing a feed."""
from __future__ import annotations

import argparse
import fnmatch
import json
import ntpath
import os
import re
import tempfile
from pathlib import Path, PurePosixPath

from scripts.releases import r2
from scripts.releases.semver import is_valid_version


def validate_identity(tag: str, commit: str, name: str) -> None:
    if (not isinstance(tag, str) or not tag.startswith("v") or not is_valid_version(tag[1:])
            or not re.fullmatch(r"[a-f0-9]{40}", commit or "")
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name or "")):
        raise ValueError("Invalid release handoff identity")


def receipt_name(name: str) -> str:
    return f"handoff-{name}.json"


def relative_path(value: str) -> str:
    if (not isinstance(value, str) or not value or value.startswith("/")
            or any(part in ("", ".", "..") for part in value.split("/"))
            or any(c in value for c in "\\:%?#") or any(ord(c) < 32 for c in value)
            or ntpath.isreserved(value)):
        raise ValueError("Invalid release artifact path")
    return value


def validate_receipt(receipt: dict, tag: str, commit: str, name: str) -> list[dict]:
    validate_identity(tag, commit, name)
    if (not isinstance(receipt, dict) or receipt.get("schema") != 1
            or receipt.get("tag") != tag or receipt.get("commit") != commit or receipt.get("name") != name):
        raise ValueError("Release handoff identity mismatch")
    files = receipt.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("No files in release handoff")
    seen = set()
    for row in files:
        if not isinstance(row, dict):
            raise ValueError("Invalid release file receipt")
        path = relative_path(row.get("path"))
        if path.casefold() in seen:
            raise ValueError("Duplicate release artifact path")
        seen.add(path.casefold())
        if (type(row.get("size")) is not int or row["size"] < 0
                or not re.fullmatch(r"[a-f0-9]{64}", row.get("sha256", ""))):
            raise ValueError("Invalid release file size or digest")
    return files


def stage(tag: str, commit: str, name: str, root: Path, includes: list[str]) -> dict:
    validate_identity(tag, commit, name)
    root = root.resolve()
    selected = {}
    for pattern in includes:
        matched = [p for p in root.glob(pattern) if p.is_file()]
        if not matched:
            raise ValueError(f"No files match release handoff pattern: {pattern}")
        for file in matched:
            if file.is_symlink() or not file.resolve().is_relative_to(root):
                raise ValueError("Release artifact path escapes its build root")
            selected[file.relative_to(root).as_posix()] = file
    files = [{"path": relative_path(path), "size": file.stat().st_size, "sha256": r2.file_sha256(file)}
             for path, file in sorted(selected.items())]
    receipt = {"schema": 1, "tag": tag, "commit": commit, "name": name, "files": files}
    validate_receipt(receipt, tag, commit, name)
    for row in files:
        r2.put(tag=tag, key=row["path"], file=selected[row["path"]], immutable=True)
    # The receipt is the completion marker. An interrupted upload cannot publish it.
    with tempfile.TemporaryDirectory() as directory:
        file = Path(directory) / receipt_name(name)
        file.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        r2.put(tag=tag, key=file.name, file=file, immutable=True)
    return receipt


def read_receipt(tag: str, commit: str, name: str) -> dict:
    validate_identity(tag, commit, name)
    creds, base, bucket = r2.credentials()
    key = r2.staging_key_for(tag, receipt_name(name))
    text = r2.get_object(creds, base, bucket, key, r2.amz_timestamp())
    if text is None:
        raise ValueError(f"Missing release handoff: {name}")
    receipt = json.loads(text)
    validate_receipt(receipt, tag, commit, name)
    return receipt


def fetch(tag: str, commit: str, names: list[str], root: Path,
          includes: list[str] | None = None) -> list[dict]:
    receipts = [read_receipt(tag, commit, name) for name in names]
    root = root.resolve()
    selected = {}
    for receipt in receipts:
        for row in receipt["files"]:
            if includes and not any(fnmatch.fnmatchcase(row["path"], pattern) for pattern in includes):
                continue
            prior = selected.get(row["path"].casefold())
            if prior is not None and prior != row:
                raise ValueError("Conflicting release file receipts")
            target = root / PurePosixPath(row["path"])
            if not target.resolve().is_relative_to(root):
                raise ValueError("Release artifact path escapes its download root")
            selected[row["path"].casefold()] = row
    if not selected:
        raise ValueError("No files selected from release handoff")
    root.mkdir(parents=True, exist_ok=True)
    creds, base, bucket = r2.credentials()
    for row in selected.values():
        r2.download_object(creds, base, bucket, r2.staging_key_for(tag, row["path"]),
                           root / row["path"], r2.amz_timestamp(),
                           expected_size=row["size"], expected_sha256=row["sha256"])
    for receipt in receipts:
        (root / receipt_name(receipt["name"])).write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return receipts


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["stage", "fetch"])
    parser.add_argument("--tag", default=os.environ.get("HERMES_PAYLOAD_TAG") or os.environ.get("RELEASE_TAG"), required=False)
    parser.add_argument("--commit", default=os.environ.get("GITHUB_SHA"))
    parser.add_argument("--name", action="append", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--include", action="append")
    args = parser.parse_args(argv)
    if args.command == "stage":
        if len(args.name) != 1 or not args.include:
            parser.error("stage needs one name and at least one include pattern")
        stage(args.tag, args.commit, args.name[0], args.root, args.include)
    else:
        fetch(args.tag, args.commit, args.name, args.root, args.include)


if __name__ == "__main__":
    main()
