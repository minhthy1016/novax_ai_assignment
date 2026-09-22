"""Document access policy: which knowledge a principal may retrieve.

Rules (evaluated in trusted code, then enforced again by Postgres row-level security):

==============  ===================================================================
classification  who may read
==============  ===================================================================
public          every authenticated employee
internal        holders of ``docs:<department>`` for the document's department
confidential    holders of ``docs:<department>`` AND ``<department>:confidential``;
                stored in a separate table, never in the shared index
==============  ===================================================================

The scope is computed from database permissions (never from the token or the prompt)
and is the *only* input the retrieval SQL and the RLS session settings are built from.
"""

from __future__ import annotations

from dataclasses import dataclass

from opsassist.auth import Principal

# Permission suffix -> department slug. Explicit, so a new permission string cannot
# silently widen access.
DOC_PERMISSION_DEPARTMENTS: dict[str, str] = {
    "docs:engineering": "engineering",
    "docs:hr": "hr",
    "docs:it": "it_ops",
    "docs:finance": "finance",
    "docs:ai_platform": "ai_platform",
}


@dataclass(frozen=True, slots=True)
class AccessScope:
    user_id: str
    internal_departments: frozenset[str]
    confidential_departments: frozenset[str]

    def can_read(self, department: str, classification: str) -> bool:
        if classification == "public":
            return True
        if classification == "internal":
            return department in self.internal_departments
        if classification == "confidential":
            return department in self.confidential_departments
        return False  # unknown classification: deny


def scope_for(principal: Principal) -> AccessScope:
    internal = frozenset(
        dept for perm, dept in DOC_PERMISSION_DEPARTMENTS.items() if principal.has(perm)
    )
    confidential = frozenset(d for d in internal if principal.has(f"{d}:confidential"))
    return AccessScope(principal.user_id, internal, confidential)
