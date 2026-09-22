import json

import pytest

from opsassist.logging_setup import REDACTED, configure_logging, get_logger, redact_sensitive
from opsassist.middleware import resolve_request_id


def test_valid_incoming_request_id_is_kept() -> None:
    assert resolve_request_id("req-abc12345") == "req-abc12345"


@pytest.mark.parametrize(
    "bad",
    [None, "", "short", "has space in it", "inject\nfake=log", "x" * 65, 'evil"quote123'],
)
def test_unsafe_request_id_is_replaced(bad: str | None) -> None:
    rid = resolve_request_id(bad)
    assert rid != bad
    assert len(rid) == 32


def test_redaction_masks_secret_like_keys() -> None:
    event = {"event": "x", "api_key": "sk-1", "Authorization": "Bearer t", "user": "U001"}
    out = redact_sensitive(None, "info", event)
    assert out["api_key"] == REDACTED
    assert out["Authorization"] == REDACTED
    assert out["user"] == "U001"


def test_logs_are_single_line_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "test-service")
    get_logger("t").info("hello", password="p@ss", n=1)
    line = capsys.readouterr().out.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["event"] == "hello"
    assert record["service"] == "test-service"
    assert record["password"] == REDACTED
