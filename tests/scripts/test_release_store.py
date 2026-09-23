"""The Store submission is held until publish and is safe to rerun."""
import json

import pytest


class _Cli:
    """Stands in for the msstore CLI. Records every argv it is handed."""

    def __init__(self, pending):
        self.events = []
        self.pending = pending

    def __call__(self, argv):
        name = " ".join(argv)
        if argv[:3] == ["msstore", "submission", "status"]:
            self.events.append(("status", name))
            return json.dumps(
                {"status": "Certification"} if self.pending
                else {"status": "Published"})
        if argv[:3] == ["msstore", "submission", "get"]:
            self.events.append(("get", name))
            return json.dumps({
                "targetPublishMode": "Immediate", "friendlyName": "Submission 2",
                "listings": {}})
        if argv[:3] == ["msstore", "submission", "delete"]:
            self.events.append(("delete-pending", name))
            return ""
        if argv[:3] == ["msstore", "submission", "update"]:
            self.events.append(("update", name))
            self.updated = json.loads(argv[4])
            return ""
        if argv[:3] == ["msstore", "submission", "publish"]:
            self.events.append(("commit", name))
            return ""
        if argv[:2] == ["msstore", "publish"]:
            self.events.append(("submit", name))
            return ""
        raise AssertionError(argv)

    def names(self):
        return [name for _kind, name in self.events]


def _index(names, prefix):
    return next(index for index, name in enumerate(names)
                if name.startswith(prefix))


def test_submit_deletes_the_in_flight_submission_first():
    from scripts.releases.store import submit

    cli = _Cli(pending=True)
    submit("verified/Store-1.2.3.msixbundle", product_id="9NTEST", run=cli)
    names = cli.names()
    assert _index(names, "msstore submission delete") \
        < _index(names, "msstore publish ")
    assert _index(names, "msstore submission delete") \
        < _index(names, "msstore submission publish")


def test_submit_skips_the_delete_when_nothing_is_in_flight():
    from scripts.releases.store import submit

    cli = _Cli(pending=False)
    submit("verified/Store-1.2.3.msixbundle", product_id="9NTEST", run=cli)
    assert not any(name.startswith("msstore submission delete")
                   for name in cli.names())


def test_submit_turns_auto_publish_off():
    from scripts.releases.store import submit

    cli = _Cli(pending=False)
    submit("verified/Store-1.2.3.msixbundle", product_id="9NTEST", run=cli)
    assert cli.updated["targetPublishMode"] == "Manual"


def test_submit_commits_the_held_submission():
    from scripts.releases.store import submit

    cli = _Cli(pending=False)
    submit("verified/Store-1.2.3.msixbundle", product_id="9NTEST", run=cli)
    names = cli.names()
    assert _index(names, "msstore submission update") \
        < _index(names, "msstore submission publish")
    assert names[-1].startswith("msstore submission publish")


def test_submit_keeps_the_submission_in_draft_until_the_mode_is_set():
    from scripts.releases.store import submit

    cli = _Cli(pending=False)
    submit("verified/Store-1.2.3.msixbundle", product_id="9NTEST", run=cli)
    submit_call = next(name for name in cli.names() if name.startswith("msstore publish"))
    assert "--noCommit" in submit_call
    assert _index(cli.names(), "msstore publish ") \
        < _index(cli.names(), "msstore submission update")


class _Api:
    """Stands in for the Partner Center submission REST API."""

    def __init__(self, status, in_flight=True, fail_update=False):
        self.requests = []
        self.status = status
        self.in_flight = in_flight
        self.fail_update = fail_update
        self.updated = None
        self.committed = False

    def __call__(self, request):
        self.requests.append(request)
        url, method = request["url"], request["method"]
        if url.endswith("/token"):
            return {"status": 200, "body": {"access_token": "tok"}}
        if url.endswith("/submissions") and method == "POST":
            if self.in_flight:
                return {
                    "status": 409,
                    "body": {"code": "InvalidOperation", "message":
                             "The app already has an in-progress submission: "
                             "1152921504621243540"},
                }
            return {"status": 201, "body": {"id": "probe", "status": "PendingCommit"}}
        if url.endswith("/status") and method == "GET":
            return {"status": 200, "body": {"status": self.status}}
        if "/submissions/" in url and method == "GET":
            return {"status": 200, "body": {
                "id": "1152921504621243540", "targetPublishMode": "Manual",
                "friendlyName": "Submission 2"}}
        if "/submissions/" in url and method == "DELETE":
            return {"status": 204, "body": ""}
        if "/submissions/" in url and method == "PUT":
            if self.fail_update:
                return {"status": 500, "body": {"code": "ServiceError"}}
            self.updated = request["body"]
            return {"status": 200, "body": {"id": "1152921504621243540"}}
        if url.endswith("/commit") and method == "POST":
            self.committed = True
            return {"status": 200, "body": {"status": "CommitStarted"}}
        raise AssertionError(request)


def _release(status, **kwargs):
    from scripts.releases.store import release

    api = _Api(status, **kwargs)
    result = release(product_id="9NTEST", tenant_id="T", client_id="C",
                     client_secret="S", run=api)
    return api, result


def test_release_publishes_a_certified_submission():
    api, result = _release("Release")
    assert result == "released"
    assert api.updated["targetPublishMode"] == "Immediate"
    assert api.committed is True


def test_release_turns_auto_publish_on_when_not_certified():
    api, result = _release("Certification")
    assert result == "auto-publish"
    assert api.updated["targetPublishMode"] == "Immediate"
    assert api.committed is True


def test_release_is_a_noop_when_the_submission_already_goes_live():
    api, result = _release("PendingPublication")
    assert result == "already-live"
    assert api.updated is None and api.committed is False


def test_release_without_an_in_flight_submission_deletes_its_probe_draft():
    api, result = _release("Certification", in_flight=False)
    assert result == "no-submission"
    deleted = [r for r in api.requests
               if r["method"] == "DELETE" and "/submissions/" in r["url"]]
    assert len(deleted) == 1
    assert api.committed is False


def test_a_failed_store_call_leaves_the_run_red():
    from scripts.releases.store import StoreError

    with pytest.raises(StoreError):
        _release("Certification", fail_update=True)
def test_release_from_env_skips_when_the_store_is_not_configured(capsys):
    from scripts.releases.store import release_from_env

    assert release_from_env({}) == "not-configured"


def test_release_from_env_passes_the_configured_credentials():
    from scripts.releases.store import release_from_env

    api = _Api("Release")
    env = {
        "MS_STORE_PRODUCT_ID": "9NTEST",
        "MS_STORE_TENANT_ID": "TENANT",
        "MS_STORE_CLIENT_ID": "CLIENT",
        "MS_STORE_CLIENT_SECRET": "SECRET",
    }
    assert release_from_env(env, run=api) == "released"
    token = next(r for r in api.requests if r["url"].endswith("/token"))
    assert token["form"]["client_id"] == "CLIENT"
    assert token["form"]["client_secret"] == "SECRET"
