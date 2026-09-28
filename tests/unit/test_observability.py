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
@pytest.mark.security
def test_unsafe_request_id_is_replaced(bad: str | None) -> None:
    rid = resolve_request_id(bad)
    assert rid != bad
    assert len(rid) == 32


@pytest.mark.security
def test_redaction_masks_secret_like_keys() -> None:
    event = {
        "event": "x",
        "api_key": "sk-1",
        "Authorization": "Bearer t",
        "access_token": "eyJ...",
        "jwt_secret": "s",
        "user": "U001",
        "prompt_tokens": 70,
        "completion_tokens": 30,
    }
    out = redact_sensitive(None, "info", event)
    for key in ("api_key", "Authorization", "access_token", "jwt_secret"):
        assert out[key] == REDACTED, key
    # Usage counters are telemetry, not secrets.
    assert (out["user"], out["prompt_tokens"], out["completion_tokens"]) == ("U001", 70, 30)


def test_logs_are_single_line_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "test-service")
    get_logger("t").info("hello", password="p@ss", n=1)
    line = capsys.readouterr().out.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["event"] == "hello"
    assert record["service"] == "test-service"
    assert record["password"] == REDACTED


def test_tool_calls_are_counted_with_bounded_labels() -> None:
    from opsassist.metrics import TOOL_CALLS
    from opsassist.tools.executor import ToolOutcome, _count

    def value(tool: str, stage: str, outcome: str) -> float:
        return TOOL_CALLS.labels(tool=tool, decision=stage, outcome=outcome)._value.get()

    before = value("get_server_status", "request", "denied")
    _count("get_server_status", "request", ToolOutcome("denied", "get_server_status", "no"))
    assert value("get_server_status", "request", "denied") == before + 1
    # A name the model made up never becomes a label of its own.
    unknown = value("unknown", "request", "denied")
    _count("rm -rf /", "request", ToolOutcome("denied", "rm -rf /", "no such tool"))
    assert value("unknown", "request", "denied") == unknown + 1
