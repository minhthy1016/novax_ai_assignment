import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from opsassist.seed import load_fixtures


def test_sample_fixtures_match_the_brief(sample_data_dir: Path) -> None:
    fx = load_fixtures(sample_data_dir)
    users = {u.id: u for u in fx.users}
    assert set(users) == {"U001", "U002", "U003", "U004", "U005", "U006"}
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
