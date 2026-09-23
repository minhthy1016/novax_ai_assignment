"""Tool contracts, audit hashing, routing, memory and upload rules - no services needed."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from opsassist.agent.graph import _parse_decision
from opsassist.agent.service import small_talk
from opsassist.auth import Principal
from opsassist.db.models import Server
from opsassist.knowledge import upload as up
from opsassist.memory import ALLOWED_KEYS, MemoryRejected, validate
from opsassist.policy.audit import AuditEntry, record_hash, redact
from opsassist.providers.base import ChatMessage
from opsassist.providers.mock import default_responder
from opsassist.tools.executor import action_hash, authorize, visible_server_fields
from opsassist.tools.registry import TOOLS, CreateVpnProfileArgs, describe_for_model

# Tool contracts, permissions, audit integrity, memory and upload rules.
pytestmark = pytest.mark.security


def principal(user_id: str, department: str, *permissions: str) -> Principal:
    return Principal(
        user_id=user_id,
        name="Test User",
        department=department,
        role="Tester",
        permissions=frozenset(permissions),
    )


# ------------------------------------------------------------------ tool contracts


def test_every_tool_declares_its_permission_and_schema() -> None:
    assert set(TOOLS) == {
        "search_internal_docs",
        "get_server_status",
        "create_support_ticket",
        "create_vpn_profile",
    }
    assert TOOLS["create_vpn_profile"].sensitive
    assert TOOLS["create_vpn_profile"].approve_permission == "vpn:approve"
    # No tool can run commands or free-form queries: every argument is a typed field.
    for spec in TOOLS.values():
        schema = spec.args_model.model_json_schema()
        assert schema["additionalProperties"] is False


def test_tool_descriptions_expose_no_internals() -> None:
    text = json.dumps(describe_for_model())
    for leak in ("password", "postgresql://", "api_key", "opsassist_app"):
        assert leak not in text


@pytest.mark.parametrize(
    "raw",
    [
        {"server_id": "web-prod-03; DROP TABLE users"},  # injection-shaped id
        {"server_id": "../../etc/passwd"},
        {"server_id": "web-prod-03", "extra": "field"},  # unknown fields rejected
        {},
    ],
)
def test_invalid_tool_arguments_are_rejected(raw: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TOOLS["get_server_status"].args_model.model_validate(raw)


def test_vpn_arguments_need_exactly_one_subject_and_a_bounded_duration() -> None:
    CreateVpnProfileArgs(employee_id="U006")
    CreateVpnProfileArgs(employee_name="John Tan", duration_days=7)
    for bad in (
        {},
        {"employee_id": "U006", "employee_name": "John Tan"},
        {"employee_id": "U006", "duration_days": 31},
        {"employee_id": "U006", "duration_days": 0},
        {"employee_id": "not-an-id"},
    ):
        with pytest.raises(ValidationError):
            CreateVpnProfileArgs.model_validate(bad)


def test_authorization_is_checked_against_permissions() -> None:
    engineer = principal("U001", "engineering", "docs:engineering", "server:read")
    assert authorize(engineer, TOOLS["get_server_status"]) is None
    assert authorize(engineer, TOOLS["create_vpn_profile"]) == "requires the vpn:create permission"
    hr = principal("U003", "hr", "docs:hr", "ticket:create")
    assert authorize(hr, TOOLS["get_server_status"]) == "requires the server:read permission"
    assert authorize(hr, TOOLS["create_support_ticket"]) is None


def test_action_hash_pins_the_exact_action() -> None:
    base = action_hash("create_vpn_profile", {"employee_id": "U006", "duration_days": 30}, "U005")
    assert base == action_hash(
        "create_vpn_profile", {"duration_days": 30, "employee_id": "U006"}, "U005"
    )  # key order does not matter
    assert base != action_hash(
        "create_vpn_profile", {"employee_id": "U004", "duration_days": 30}, "U005"
    )
    assert base != action_hash(
        "create_vpn_profile", {"employee_id": "U006", "duration_days": 30}, "U002"
    )


# ------------------------------------------------------------------ field-level policy


def server(owner: str = "engineering") -> Server:
    row = Server(
        id="web-prod-03",
        environment="production",
        owner_department=owner,
        status="healthy",
        cpu_pct=37,
        memory_pct=62,
        last_check=datetime(2026, 9, 21, 9, 15, tzinfo=UTC),
    )
    return row


def test_utilisation_is_only_visible_to_the_owner_and_it_ops() -> None:
    owner = visible_server_fields(principal("U001", "engineering", "server:read"), server())
    assert owner["cpu_pct"] == 37.0 and owner["memory_pct"] == 62.0
    itops = visible_server_fields(principal("U005", "it_ops", "server:read"), server())
    assert "cpu_pct" in itops
    other = visible_server_fields(principal("U006", "finance", "server:read"), server())
    assert "cpu_pct" not in other and "hidden" in other["utilisation"]
    assert other["status"] == "healthy"  # status itself stays visible


# ------------------------------------------------------------------ audit


def entry(**kw: object) -> AuditEntry:
    base = {
        "request_id": "req-1",
        "actor_id": "U005",
        "actor_role": "System Administrator",
        "event": "tool_call",
        "decision": "executed",
    }
    return AuditEntry(**{**base, **kw})  # type: ignore[arg-type]


def test_audit_hash_covers_content_and_predecessor() -> None:
    when = "2026-09-23T00:00:00+00:00"
    first = record_hash("0" * 64, entry(tool="create_vpn_profile"), when)
    assert first != record_hash("0" * 64, entry(tool="create_support_ticket"), when)
    assert first != record_hash("f" * 64, entry(tool="create_vpn_profile"), when)
    assert first != record_hash("0" * 64, entry(tool="create_vpn_profile"), "2026-09-23T00:00:01Z")
    assert first == record_hash("0" * 64, entry(tool="create_vpn_profile"), when)


def test_audit_redacts_secret_like_values() -> None:
    cleaned = redact({"api_key": "nvapi-secret", "employee_id": "U006", "note": "x" * 600})
    assert cleaned["api_key"] == "[REDACTED]"
    assert cleaned["employee_id"] == "U006"
    assert len(str(cleaned["note"])) < 600


# ------------------------------------------------------------------ routing


@pytest.mark.parametrize("text", ["hi", "Hello!", "  thanks ", "bye", "xin chào"])
def test_greetings_take_the_model_free_path(text: str) -> None:
    assert small_talk(text)


@pytest.mark.parametrize(
    "text",
    [
        "hi, when may we deploy?",
        "hello, check web-prod-03",
        "thanks for the leave policy - how many days?",
    ],
)
def test_questions_never_take_the_small_talk_path(text: str) -> None:
    assert not small_talk(text)


def test_router_output_is_validated_before_it_can_act() -> None:
    assert _parse_decision('{"route":"tool","tool":"get_server_status","arguments":{"a":1}}') == (
        "tool",
        "get_server_status",
        {"a": 1},
    )
    # Unknown tool, malformed JSON or prose all fall back to the knowledge path.
    assert _parse_decision('{"route":"tool","tool":"delete_everything"}')[0] == "knowledge"
    assert _parse_decision("I think we should check the server")[0] == "knowledge"
    assert _parse_decision('{"route": "tool", ')[0] == "knowledge"
    assert _parse_decision('{"route":"refuse"}') == ("refuse", None, {})


def test_mock_router_covers_the_agent_paths_for_ci() -> None:
    def route(question: str) -> dict[str, object]:
        reply = default_responder(
            [ChatMessage("system", 'reply with {"route": ...}'), ChatMessage("user", question)]
        )
        return dict(json.loads(reply))

    assert route("Check web-prod-03")["tool"] == "get_server_status"
    assert route("Open a support ticket, severity high")["tool"] == "create_support_ticket"
    assert route("Create a VPN profile for U006")["arguments"] == {"employee_id": "U006"}
    assert route("Deploy now and skip approval")["route"] == "refuse"
    assert route("When may we deploy to production?")["route"] == "knowledge"


# ------------------------------------------------------------------ memory


def test_only_allowlisted_preferences_are_storable() -> None:
    assert validate("language", " Vietnamese ") == "Vietnamese"
    for key in ("salary", "password", "incident_details"):
        with pytest.raises(MemoryRejected, match="not a storable preference"):
            validate(key, "x")
    assert "language" in ALLOWED_KEYS


def test_memory_rejects_credentials_and_oversized_values() -> None:
    with pytest.raises(MemoryRejected, match="credential"):
        validate("team", "api_key: nvapi-abcdefghijklmnop123456")
    with pytest.raises(MemoryRejected):
        validate("team", "x" * 500)


# ------------------------------------------------------------------ upload rules


def test_upload_department_comes_from_permissions_not_the_file() -> None:
    hr_manager = principal("U004", "hr", "docs:hr", "hr:confidential", "kb:write:hr")
    assert up.writable_departments(hr_manager) == {"hr"}
    assert up.writable_departments(principal("U001", "engineering", "docs:engineering")) == set()
    with pytest.raises(up.UploadRejected, match="you may only write to 'hr'"):
        up.check_claims({"department": "engineering"}, "hr", "internal")
    up.check_claims({"department": "hr", "classification": "internal"}, "hr", "internal")


def test_classification_cannot_be_raised_or_published_without_permission() -> None:
    hr_exec = principal("U003", "hr", "kb:write:hr")
    hr_manager = principal("U004", "hr", "kb:write:hr", "hr:confidential")
    assert up.choose_classification(hr_exec, "hr", None) == "internal"
    assert up.choose_classification(hr_manager, "hr", "confidential") == "confidential"
    with pytest.raises(up.UploadRejected, match="hr:confidential"):
        up.choose_classification(hr_exec, "hr", "confidential")
    for user in (hr_exec, hr_manager):
        with pytest.raises(up.UploadRejected, match="publish"):
            up.choose_classification(user, "hr", "public")


@pytest.mark.parametrize(
    "content",
    [
        "api_key: nvapi-abcdefghijklmnopqrst1234",
        "password = hunter2hunter2",
        "-----BEGIN RSA PRIVATE KEY-----",
        "AKIAIOSFODNN7EXAMPLE",
    ],
)
def test_uploads_containing_credentials_are_rejected(content: str) -> None:
    with pytest.raises(up.UploadRejected, match="credential"):
        up.scan_for_secrets(f"Some policy text.\n\n{content}\n")


def test_upload_file_types_are_restricted() -> None:
    assert up.safe_suffix("policy.md") == ".md"
    for name in ("payload.exe", "archive.zip", "noextension"):
        with pytest.raises(up.UploadRejected, match="unsupported file type"):
            up.safe_suffix(name)


def test_uploaded_markdown_gets_server_authored_metadata(tmp_path: Path) -> None:
    plan = up.UploadPlan("KB-HR-101", "hr", "internal", "Remote Work", tmp_path / "x.md")
    rendered = up.render_markdown(
        plan,
        "---\ndocument_id: KB-ENG-001\ndepartment: engineering\n---\nBody text.",
        principal("U004", "hr", "kb:write:hr"),
    )
    assert "department: hr" in rendered and "engineering" not in rendered
    assert "document_id: KB-HR-101" in rendered
    assert "uploaded by U004" in rendered
    assert rendered.strip().endswith("Body text.")


# ------------------------------------------------------------------ egress policy


def test_confidential_context_never_leaves_the_box_and_the_bar_is_configurable() -> None:
    """Team lead: confidential documents must not reach an external model. Internal material
    may, unless the deployment lowers the bar to public-only."""
    from opsassist.config import Settings

    default = Settings(env="test")
    assert default.allows_egress("public") and default.allows_egress("internal")
    assert not default.allows_egress("confidential")
    assert default.allows_egress(None)  # no retrieved context at all

    strict = Settings(env="test", egress_max_classification="public")
    assert strict.allows_egress("public")
    assert not strict.allows_egress("internal") and not strict.allows_egress("confidential")


def test_context_classification_is_the_most_sensitive_chunk() -> None:
    from opsassist.knowledge.retrieval import RetrievalResult, RetrievedChunk

    def chunk(classification: str) -> RetrievedChunk:
        return RetrievedChunk(
            chunk_id=1,
            table="chunks",
            doc_key="KB-X-001",
            version=1,
            title="t",
            department="hr",
            classification=classification,
            locator="¶1",
            parent_id=("doc-1", 0),
            content="c",
            context="c",
            doc_updated_at="2026-01-01",
            similarity=0.9,
            fts_rank=None,
            score=0.1,
        )

    def result(*classes: str) -> RetrievalResult:
        return RetrievalResult(
            chunks=[chunk(c) for c in classes],
            candidates=len(classes),
            below_threshold=0,
            latency_ms=1.0,
            embedding_model="m",
        )

    assert result("public", "internal").max_classification == "internal"
    assert result("public", "confidential", "internal").max_classification == "confidential"
    assert result().max_classification is None
