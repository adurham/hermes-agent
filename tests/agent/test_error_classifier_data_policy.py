"""A Nous 403 ``data_policy_denied`` is deterministic for this context: no retry, no rotation, no fallback."""

from agent.error_classifier import FailoverReason, classify_api_error


class _ForbiddenError(Exception):
    def __init__(self, body):
        super().__init__("Forbidden")
        self.status_code = 403
        self.body = body


def test_403_data_policy_denied_never_retries_or_falls_back():
    body = {"error": {"code": "data_policy_denied", "type": "data_policy_denied", "message": "No endpoint qualifies.",
                      "requirement": ["no_training"], "sources": ["request"], "suggested_action": "switch_model"}}
    result = classify_api_error(_ForbiddenError(body), provider="nous")
    assert result.reason == FailoverReason.data_policy_denied
    assert (result.retryable, result.should_fallback, result.should_rotate_credential) == (False, False, False)
