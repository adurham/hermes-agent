"""Store submission control for the release pipeline.

The green run submits the verified ``.msixbundle`` with auto-publish off
(``targetPublishMode: "Manual"``); the publication pass releases it (or turns
auto-publish on while it is still in certification). Both entry points act on
the submission's current state, so both are safe to rerun.

Green run — ``python -m scripts.releases.store submit <package>`` (Windows
runner, ``msstore`` CLI configured beforehand by
``microsoft/microsoft-store-apppublisher`` + ``msstore reconfigure``):

1. ``msstore submission status <productId>`` — a submission is in flight when
   its status is not one of the terminal/failure states
   (``None, Published, PublishFailed, Canceled, CertificationFailed,
   PreProcessingFailed, CommitFailed``).
2. ``msstore submission delete <productId> --no-confirm`` — only when one is
   in flight. Deleting the last published submission would be wrong, so the
   status gates this.
3. ``msstore publish <package> --appId <productId> --noCommit`` — uploads the
   package and leaves the submission as a draft (``--noCommit`` / ``-nc``,
   "Disables committing the submission, keeping it in draft state").
4. ``msstore submission get <productId>`` — the complete submission JSON.
5. ``msstore submission update <productId> <json>`` — the same JSON with
   ``targetPublishMode: "Manual"``. For MSIX apps ``submission update`` sends
   the complete submission JSON, so it sets packages and publish mode at once.
6. ``msstore submission publish <productId>`` — commits; certification starts.

Publication pass — ``scripts.releases.store.release(...)`` (Ubuntu runner,
Partner Center submission REST API, stdlib ``urllib`` only):

1. ``POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token`` with
   ``grant_type=client_credentials`` and scope
   ``https://manage.devcenter.microsoft.com/.default`` (the same MS_STORE_*
   credentials the CLI uses).
2. ``POST https://manage.devcenter.microsoft.com/v1.0/my/applications/{id}/submissions``
   to find the in-flight submission: ``201`` means none existed (the fresh
   probe draft is deleted immediately with ``DELETE .../submissions/{id}``,
   nothing else is touched); ``409`` means one exists and its id is in the
   error body.
3. ``GET .../submissions/{id}/status``. Terminal or already-live states
   (``PendingPublication, Publishing, Published``) are a no-op ("already-live").
4. Otherwise the held submission is released by rewriting it with
   ``targetPublishMode: "Immediate"`` (``PUT .../submissions/{id}``) and
   committing it (``POST .../submissions/{id}/commit``). A certified
   submission (status ``Release``) goes live now ("released"); one still in
   certification goes live when certification passes ("auto-publish").

Sources for the commands and fields above:
- msstore CLI commands and options (``submission status/get/update/delete
  --no-confirm/publish``, ``publish --noCommit``):
  https://learn.microsoft.com/en-us/windows/apps/publish/msstore-dev-cli/commands
- CLI CI/CD setup (``msstore reconfigure --tenantId --sellerId --clientId
  --clientSecret`` on the runner):
  https://learn.microsoft.com/en-us/windows/apps/publish/msstore-dev-cli/github-actions
- Submission resource fields (``targetPublishMode``:
  ``Immediate``/``Manual``/``SpecificDate``) and the status enum
  (``PendingCommit, CommitStarted, PreProcessing, Certification,
  CertificationFailed, Release, PendingPublication, Publishing, Published, ...``):
  https://learn.microsoft.com/en-us/windows/uwp/monetize/manage-app-submissions
- REST methods (create/update/commit/delete/status):
  https://learn.microsoft.com/en-us/windows/uwp/monetize/manage-app-submissions#methods-for-managing-app-submissions
  (individual pages: create-an-app-submission, update-an-app-submission,
  commit-an-app-submission, delete-an-app-submission,
  get-status-for-an-app-submission)
- Azure AD client-credentials token:
  https://learn.microsoft.com/en-us/windows/uwp/monetize/create-and-manage-submissions-using-windows-store-services#obtain-an-azure-ad-access-token

Unverified on paper but guarded in code: whether the API accepts a ``PUT``
update on a submission that is committed and held (certified, waiting for
release). If it refuses, the release step fails and the publication run stays
red; rerunning after a fix from Partner Center is safe.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

API_ROOT = "https://manage.devcenter.microsoft.com/v1.0/my"
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
TOKEN_SCOPE = "https://manage.devcenter.microsoft.com/.default"

# Submission statuses that are terminal or already decided; none of them is an
# in-flight submission the green run would need to clear, and none of them is
# worth touching at release time except the already-live set below.
_NOT_IN_FLIGHT = {
    "None", "Published", "PublishFailed", "Canceled",
    "CertificationFailed", "PreProcessingFailed", "CommitFailed",
}
_ALREADY_LIVE = {"PendingPublication", "Publishing", "Published"}
# The status of a submission that passed certification and is held by
# targetPublishMode Manual, waiting for release.
_CERTIFIED_HELD = "Release"


class StoreError(RuntimeError):
    """A Store CLI or API call failed. The calling run stays red."""


def _cli_run(argv: list[str]) -> str:
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise StoreError(f"{' '.join(argv)} failed: {result.stderr.strip()}")
    return result.stdout


def _has_in_flight(status_output: str) -> bool:
    try:
        status = json.loads(status_output)
    except json.JSONDecodeError:
        raise StoreError(f"unreadable submission status: {status_output!r}")
    if not isinstance(status, dict) or "status" not in status:
        raise StoreError(f"unreadable submission status: {status_output!r}")
    return status["status"] not in _NOT_IN_FLIGHT


def submit(package: str, *, product_id: str, run=_cli_run) -> dict:
    """Submit ``package`` with auto-publish off, clearing any in-flight one."""
    if _has_in_flight(run(["msstore", "submission", "status", product_id])):
        run(["msstore", "submission", "delete", product_id, "--no-confirm"])
    run(["msstore", "publish", package, "--appId", product_id, "--noCommit"])
    submission = json.loads(run(["msstore", "submission", "get", product_id]))
    submission["targetPublishMode"] = "Manual"
    run(["msstore", "submission", "update", product_id,
         json.dumps(submission, separators=(",", ":"))])
    run(["msstore", "submission", "publish", product_id])
    return {"productId": product_id, "targetPublishMode": "Manual"}


def _http_run(request: dict) -> dict:
    """Default injected runner: one HTTP request, stdlib only."""
    url = request["url"]
    data = None
    headers = {"Content-Type": "application/json"}
    body = request.get("body")
    if body is not None:
        data = json.dumps(body).encode()
    if "form" in request:
        data = urllib.parse.urlencode(request["form"]).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=request["method"])
    try:
        with urllib.request.urlopen(req) as response:
            raw = response.read().decode()
            return {"status": response.status,
                    "body": json.loads(raw) if raw else ""}
    except urllib.error.HTTPError as error:
        raw = error.read().decode()
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"message": raw}
        return {"status": error.code, "body": parsed}


def _token(tenant_id: str, client_id: str, client_secret: str, run) -> str:
    response = run({"method": "POST", "url": TOKEN_URL.format(tenant=tenant_id),
                    "form": {
                        "grant_type": "client_credentials",
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "scope": TOKEN_SCOPE,
                    }})
    if response["status"] != 200 or "access_token" not in response["body"]:
        raise StoreError(f"token request failed: {response['status']}")
    return response["body"]["access_token"]


def _in_progress_id(conflict_body) -> str:
    """The 409 body names the in-progress submission; pull its id out."""
    text = json.dumps(conflict_body) if not isinstance(conflict_body, str) \
        else conflict_body
    digits = "".join(character if character.isdigit() or character == " "
                     else " " for character in text).split()
    long_ids = [token for token in digits if len(token) >= 10]
    if not long_ids:
        raise StoreError(f"cannot find the in-flight submission id in {text!r}")
    return long_ids[0]


def release(*, product_id: str, tenant_id: str, client_id: str,
            client_secret: str, run=_http_run) -> str:
    """Release the held submission, or let it go live when certified."""
    token = _token(tenant_id, client_id, client_secret, run)

    def call(method: str, path: str, body: dict | None = None) -> dict:
        request = {"method": method, "url": API_ROOT + path}
        if body is not None:
            request["body"] = body
        request["headers"] = {"Authorization": f"Bearer {token}"}
        return run(request)

    submissions_path = f"/applications/{product_id}/submissions"
    probe = call("POST", submissions_path)
    if probe["status"] == 201:
        # Nothing was in flight; do not leave the probe draft behind.
        probe_id = probe["body"].get("id")
        if probe_id:
            call("DELETE", f"{submissions_path}/{probe_id}")
        return "no-submission"
    if probe["status"] != 409:
        raise StoreError(f"unexpected submission probe reply: {probe['status']}")
    submission_id = _in_progress_id(probe["body"])

    status = call("GET", f"{submissions_path}/{submission_id}/status")
    if status["status"] != 200:
        raise StoreError(f"submission status failed: {status['status']}")
    state = status["body"]["status"]
    if state in _ALREADY_LIVE:
        return "already-live"

    submission = call("GET", f"{submissions_path}/{submission_id}")
    if submission["status"] != 200:
        raise StoreError(f"submission read failed: {submission['status']}")
    held = submission["body"]
    held["targetPublishMode"] = "Immediate"
    updated = call("PUT", f"{submissions_path}/{submission_id}", held)
    if updated["status"] != 200:
        raise StoreError(f"submission update failed: {updated['status']}")
    committed = call("POST", f"{submissions_path}/{submission_id}/commit")
    if committed["status"] != 200:
        raise StoreError(f"submission commit failed: {committed['status']}")
    return "released" if state == _CERTIFIED_HELD else "auto-publish"


def release_from_env(env: dict, run=_http_run) -> str:
    """Release the Store submission when the environment configures it."""
    product_id = env.get("MS_STORE_PRODUCT_ID")
    if not product_id:
        print("Store release skipped: MS_STORE_PRODUCT_ID is not configured.",
              file=sys.stderr)
        return "not-configured"
    return release(
        product_id=product_id,
        tenant_id=env["MS_STORE_TENANT_ID"],
        client_id=env["MS_STORE_CLIENT_ID"],
        client_secret=env["MS_STORE_CLIENT_SECRET"],
        run=run,
    )


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "submit":
        product_id = os.environ["MS_STORE_PRODUCT_ID"]
        package = argv[1]
        result = submit(package, product_id=product_id)
        print(json.dumps(result, sort_keys=True))
        return 0
    if argv[:1] == ["release"]:
        print(json.dumps({"result": release_from_env(dict(os.environ))},
                         sort_keys=True))
        return 0
    print("usage: python -m scripts.releases.store submit <package> | release",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
