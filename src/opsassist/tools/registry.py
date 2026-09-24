"""Tool contracts: typed arguments, the permission each one needs, and who may approve.

Two rules hold for every tool:

* **The model never authorizes anything.** It may only propose a tool name and arguments;
  the arguments are validated against the schema here and the permission check runs in
  ``policy/executor.py`` against the caller's database permissions.
* **No tool can run shell commands, SQL, or arbitrary URLs.** Each one is a narrow function
  over fixed, validated fields - there is deliberately no deploy tool and no generic
  "execute" tool (eval case E11).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Severity = Literal["critical", "high", "medium", "low"]
MAX_VPN_DAYS = 30  # KB-IT-001: profiles are valid for at most 30 days


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields are rejected, never ignored


class SearchInternalDocsArgs(ToolArgs):
    query: str = Field(min_length=2, max_length=500)
    department: str | None = Field(default=None, pattern=r"^[a-z][a-z_]*$")


class GetServerStatusArgs(ToolArgs):
    server_id: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9.-]*$")


class CreateSupportTicketArgs(ToolArgs):
    title: str = Field(min_length=4, max_length=200)
    severity: Severity
    details: str = Field(min_length=4, max_length=4000)


TICKET_ID = r"INC-[0-9]{1,10}"
EMPLOYEE_ID = r"U[0-9]{3,}"


class GetSupportTicketArgs(ToolArgs):
    ticket_id: str = Field(pattern=rf"^{TICKET_ID}$")


class CreateVpnProfileArgs(ToolArgs):
    """Either the employee id or their name; the server resolves names, the model never
    invents an id."""

    employee_id: str | None = Field(default=None, pattern=rf"^{EMPLOYEE_ID}$")
    employee_name: str | None = Field(default=None, min_length=2, max_length=100)
    duration_days: int = Field(default=MAX_VPN_DAYS, ge=1, le=MAX_VPN_DAYS)

    @model_validator(mode="before")
    @classmethod
    def _an_id_is_an_id(cls, data: Any) -> Any:
        """An employee id given as a name ("U006") is an id: the format says so, whichever
        field the model put it in. Anything else is left for validation to judge."""
        if isinstance(data, dict):
            name = data.get("employee_name")
            candidate = name.strip().upper() if isinstance(name, str) else ""
            if re.fullmatch(EMPLOYEE_ID, candidate) and not data.get("employee_id"):
                data = {**data, "employee_id": candidate, "employee_name": None}
        return data

    @model_validator(mode="after")
    def _exactly_one_subject(self) -> CreateVpnProfileArgs:
        if bool(self.employee_id) == bool(self.employee_name):
            raise ValueError("provide exactly one of employee_id or employee_name")
        return self


@dataclass(frozen=True)
class ToolSpec:
    """A tool's contract and its skill card.

    ``purpose``, ``use_when`` and ``not_when`` are what the router reads to choose a tool.
    They describe *kinds* of request, never particular requests: no names, server ids,
    ticket ids or phrasings taken from the sample data or the evaluation cases
    (``test_prompt_hygiene.py`` enforces this). Adding a tool means writing its card here;
    the router prompt itself does not change (D-34).
    """

    name: str
    purpose: str
    args_model: type[ToolArgs]
    requires_permission: str | None  # None = any authenticated user
    use_when: tuple[str, ...] = ()
    not_when: tuple[str, ...] = ()
    # A read-only skill whose argument has an exact format may claim a message that contains
    # it, before any model is asked: (pattern, argument name). Never for a skill that writes
    # or needs approval - a trigger must not be a way around the router's refuse rule.
    trigger: tuple[str, str] | None = None
    sensitive: bool = False
    approve_permission: str | None = None  # a *different* holder must confirm

    @property
    def effect(self) -> str:
        if self.sensitive:
            return "sensitive: only proposed; runs after a different authorized person approves"
        return "reads data" if self.name.startswith(("get_", "search_")) else "creates a record"


TOOLS: dict[str, ToolSpec] = {
    "search_internal_docs": ToolSpec(
        name="search_internal_docs",
        purpose="List which approved documents mention a topic, as search results.",
        args_model=SearchInternalDocsArgs,
        requires_permission=None,
        use_when=("The user explicitly asks to search for, list or find documents.",),
        not_when=(
            "The user asks a question the documents answer: that is the knowledge route, "
            "which answers with citations.",
        ),
    ),
    "get_server_status": ToolSpec(
        name="get_server_status",
        purpose="Report the current, live status of one server identified by its id.",
        args_model=GetServerStatusArgs,
        requires_permission="server:read",
        use_when=(
            "The user asks whether a specific server is up, healthy or how it is doing now.",
        ),
        not_when=(
            "The user asks how servers should be operated, patched or scaled in general: "
            "procedures are documented, so that is knowledge.",
        ),
    ),
    "create_support_ticket": ToolSpec(
        name="create_support_ticket",
        purpose="Open a support ticket with a title, a severity and details.",
        args_model=CreateSupportTicketArgs,
        requires_permission="ticket:create",
        use_when=(
            "The user asks to open, raise, create or log a ticket, or to report a problem "
            "for follow-up. Severity is 'medium' unless the user states one.",
        ),
        not_when=("The user asks how the ticket process or severity levels work.",),
    ),
    "get_support_ticket": ToolSpec(
        name="get_support_ticket",
        purpose=(
            "Read one existing support ticket by its id: title, severity, status and the "
            "details it was raised with."
        ),
        args_model=GetSupportTicketArgs,
        requires_permission="ticket:create",
        use_when=(
            "The message contains a ticket id (INC- followed by digits), whatever it asks "
            "about that ticket - its status, its details or how to resolve it. Tickets are "
            "operational records and are never in the documents.",
        ),
        not_when=("No ticket id is given.",),
        trigger=(TICKET_ID, "ticket_id"),
    ),
    "create_vpn_profile": ToolSpec(
        name="create_vpn_profile",
        purpose="Create a VPN (OpenVPN) profile for one employee, given by id or by name.",
        args_model=CreateVpnProfileArgs,
        requires_permission="vpn:create",
        use_when=(
            "The user asks to create, set up, issue or request VPN access or a VPN profile "
            "for an employee - including when the request mentions that it needs, awaits "
            "or comes with approval, because approval always happens for this tool.",
        ),
        not_when=(
            "The user asks about VPN policy or how VPN access works: that is knowledge.",
            "The user asks to skip, avoid or not wait for the approval: that is refuse.",
        ),
        sensitive=True,
        approve_permission="vpn:approve",
    ),
}


def triggered_skill(message: str) -> tuple[str, dict[str, str]] | None:
    """The read-only skill a message names by an exact identifier (a ticket id), with that
    argument filled in - or None. Deterministic: the format lives in the skill's schema, so
    no example request is needed for the router to recognise it (D-34)."""
    found = []
    for spec in TOOLS.values():
        if spec.trigger is None or spec.sensitive or spec.effect != "reads data":
            continue
        pattern, argument = spec.trigger
        match = re.search(rf"\b{pattern}\b", message, re.IGNORECASE)
        if match:
            found.append((spec.name, {argument: match.group(0).upper()}))
    return found[0] if len(found) == 1 else None


def describe_for_model() -> list[dict[str, object]]:
    """The skill cards the router chooses from: purpose, when to use and not to use, the
    effect, and the argument schema - never credentials, connection details or internal ids."""
    return [
        {
            "name": spec.name,
            "purpose": spec.purpose,
            "use_when": list(spec.use_when),
            "not_when": list(spec.not_when),
            "effect": spec.effect,
            "parameters": spec.args_model.model_json_schema(),
        }
        for spec in TOOLS.values()
    ]
