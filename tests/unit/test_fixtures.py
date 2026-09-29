import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from opsassist.seed import load_fixtures


def test_sample_fixtures_match_the_brief(sample_data_dir: Path) -> None:
    fx = load_fixtures(sample_data_dir)
    users = {u.id: u for u in fx.users}
    # The brief's six users, unchanged, plus two candidate-added readers (D-35).
    assert set(users) == {"U001", "U002", "U003", "U004", "U005", "U006", "U007", "U008"}
    assert all(not users[f"U00{n}"].team_leads for n in range(1, 7))
    for reader in ("U007", "U008"):
        assert users[reader].department == "ai_platform"
        assert users[reader].team_leads == ["U002", "U005"]
        assert "docs:ai_platform" in users[reader].permissions
    assert "hr:confidential" in users["U004"].permissions
    assert "server:read" not in users["U004"].permissions
    assert "vpn:approve" in users["U002"].permissions
    assert "vpn:create" in users["U005"].permissions
    servers = {s.id: s for s in fx.servers}
    assert servers["ai-gpu-01"].cpu_pct is None
    assert servers["api-prod-02"].status == "degraded"


def test_unknown_department_reference_is_rejected(sample_data_dir: Path, tmp_path: Path) -> None:
    for name in ("departments.json", "servers.json"):
        (tmp_path / name).write_text((sample_data_dir / name).read_text())
    users = json.loads((sample_data_dir / "users.json").read_text())
    users[0]["department"] = "marketing"
    (tmp_path / "users.json").write_text(json.dumps(users))
    with pytest.raises(ValidationError, match="unknown departments"):
        load_fixtures(tmp_path)


def test_malformed_permission_is_rejected(sample_data_dir: Path, tmp_path: Path) -> None:
    for name in ("departments.json", "servers.json"):
        (tmp_path / name).write_text((sample_data_dir / name).read_text())
    users = json.loads((sample_data_dir / "users.json").read_text())
    users[0]["permissions"] = ["docs:*"]
    (tmp_path / "users.json").write_text(json.dumps(users))
    with pytest.raises(ValidationError, match="malformed permissions"):
        load_fixtures(tmp_path)


@pytest.mark.parametrize("leads", [["U999"], ["U001", "U998"]])
def test_an_unknown_team_lead_is_rejected(
    sample_data_dir: Path, tmp_path: Path, leads: list[str]
) -> None:
    for name in ("departments.json", "servers.json"):
        (tmp_path / name).write_text((sample_data_dir / name).read_text())
    users = json.loads((sample_data_dir / "users.json").read_text())
    users[1]["team_leads"] = leads
    (tmp_path / "users.json").write_text(json.dumps(users))
    with pytest.raises(ValidationError, match="unknown team leads"):
        load_fixtures(tmp_path)


def test_a_user_cannot_be_their_own_team_lead(sample_data_dir: Path, tmp_path: Path) -> None:
    for name in ("departments.json", "servers.json"):
        (tmp_path / name).write_text((sample_data_dir / name).read_text())
    users = json.loads((sample_data_dir / "users.json").read_text())
    users[0]["team_leads"] = [users[0]["id"]]
    (tmp_path / "users.json").write_text(json.dumps(users))
    with pytest.raises(ValidationError, match="own team lead"):
        load_fixtures(tmp_path)
