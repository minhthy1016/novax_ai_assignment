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

from dataclasses import dataclass
from typing import Literal

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


class GetSupportTicketArgs(ToolArgs):
    ticket_id: str = Field(pattern=r"^INC-[0-9]{1,10}$")


class CreateVpnProfileArgs(ToolArgs):
    """Either the employee id or their name; the server resolves names, the model never
    invents an id."""

    employee_id: str | None = Field(default=None, pattern=r"^U[0-9]{3,}$")
    employee_name: str | None = Field(default=None, min_length=2, max_length=100)
    duration_days: int = Field(default=MAX_VPN_DAYS, ge=1, le=MAX_VPN_DAYS)

    @model_validator(mode="after")
    def _exactly_one_subject(self) -> CreateVpnProfileArgs:
        if bool(self.employee_id) == bool(self.employee_name):
            raise ValueError("provide exactly one of employee_id or employee_name")
        return self


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[ToolArgs]
    requires_permission: str | None  # None = any authenticated user
    sensitive: bool = False
    approve_permission: str | None = None  # a *different* holder must confirm


TOOLS: dict[str, ToolSpec] = {
    "search_internal_docs": ToolSpec(
        name="search_internal_docs",
        description=(
            "Search approved company documents the caller may read. Use for policies, "
            "procedures, runbooks and incident reports."
        ),
        args_model=SearchInternalDocsArgs,
        requires_permission=None,
    ),
    "get_server_status": ToolSpec(
        name="get_server_status",
        description=(
            "Current status of one server by id (for example web-prod-03). Returns only the "
            "fields the caller is allowed to see."
        ),
        args_model=GetServerStatusArgs,
        requires_permission="server:read",
    ),
    "create_support_ticket": ToolSpec(
        name="create_support_ticket",
        description="Open a support ticket with a title, severity and details.",
        args_model=CreateSupportTicketArgs,
        requires_permission="ticket:create",
    ),
    "get_support_ticket": ToolSpec(
        name="get_support_ticket",
        description=(
            "Read one support ticket by its id (for example INC-1042): title, severity, "
            "status and the details it was raised with. Use this whenever the user names a "
            "ticket id - a ticket is operational data and is never in the documents."
        ),
        args_model=GetSupportTicketArgs,
        requires_permission="ticket:create",
    ),
    "create_vpn_profile": ToolSpec(
        name="create_vpn_profile",
        description=(
            "Create an OpenVPN profile for an employee. Sensitive: it is only proposed here "
            "and executed after a different authorized approver confirms it."
        ),
        args_model=CreateVpnProfileArgs,
        requires_permission="vpn:create",
        sensitive=True,
        approve_permission="vpn:approve",
    ),
}


def describe_for_model() -> list[dict[str, object]]:
    """Tool descriptions given to the model: names, purpose and JSON schema only - never
    credentials, connection details or internal ids."""
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "sensitive": spec.sensitive,
            "parameters": spec.args_model.model_json_schema(),
        }
        for spec in TOOLS.values()
    ]
