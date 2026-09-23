"""The console is a client: served only in dev/test, and holding no policy of its own."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opsassist.config import Settings
from opsassist.main import create_app

CONSOLE = Path(__file__).resolve().parents[2] / "web" / "index.html"


def app_for(env: str) -> TestClient:
    return TestClient(
        create_app(
            Settings(
                env=env,
                jwt_secret="a-non-default-secret-for-this-test",
                app_db_password="a-non-default-password",
                database_url="postgresql+psycopg://nobody:nothing@127.0.0.1:1/none",
                redis_url="redis://127.0.0.1:1/0",
            )
        )
    )


def test_console_is_served_in_development() -> None:
    with app_for("test") as client:
        resp = client.get("/ui")
    assert resp.status_code == 200
    assert "OpsAssist console" in resp.text
    assert resp.headers["content-security-policy"].startswith("default-src 'none'")


@pytest.mark.security
def test_console_and_its_token_issuer_are_absent_outside_development() -> None:
    """Both disappear together: a console without the dev issuer would be a login page for
    an issuer that does not exist, and an issuer without the console is worse."""
    with app_for("staging") as client:
        assert client.get("/ui").status_code == 404
        assert client.post("/api/auth/dev-token", json={"user_id": "U001"}).status_code == 404


@pytest.mark.security
def test_console_loads_nothing_from_the_internet() -> None:
    """A page that pulls a script from a CDN would put a third party inside the session that
    holds a bearer token. Everything is inline and same-origin."""
    page = CONSOLE.read_text(encoding="utf-8")
    assert not re.search(r"""(src|href)\s*=\s*["']https?://""", page)
    assert "fetch(" in page  # it does call the API - on its own origin


@pytest.mark.security
def test_console_carries_no_permissions_of_its_own() -> None:
    """Every decision belongs to the server. The page must not contain a permission table it
    could use to decide what to show or allow."""
    page = CONSOLE.read_text(encoding="utf-8")
    for permission in ("docs:hr", "hr:confidential", "server:read", "vpn:approve"):
        # 'vpn:approve' may only appear as a label the server sent, never as a comparison.
        assert f'"{permission}"' not in page
        assert f"'{permission}'" not in page
