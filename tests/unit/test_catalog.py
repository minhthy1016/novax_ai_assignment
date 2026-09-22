from pathlib import Path

import pytest
from pydantic import ValidationError

from opsassist.gateway.catalog import Catalog, UnknownModelError, load_catalog

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def catalog() -> Catalog:
    return load_catalog(REPO / "config" / "models.toml")


def test_shipped_catalog_is_valid(catalog: Catalog) -> None:
    assert catalog.defaults.chat == "chat-default"
    assert catalog.routes["chat-default"] == ["nim/gpt-oss-20b", "ollama/llama3.2-3b"]


def test_route_and_model_id_resolution(catalog: Catalog) -> None:
    name, targets = catalog.resolve(None, "chat")
    assert name == "chat-default" and len(targets) == 2
    name, targets = catalog.resolve("ollama/llama3.2-3b", "chat")
    assert [t.id for t in targets] == ["ollama/llama3.2-3b"]  # explicit model: no fallback


def test_unknown_and_wrong_kind_are_rejected(catalog: Catalog) -> None:
    with pytest.raises(UnknownModelError):
        catalog.resolve("gpt-9", "chat")
    with pytest.raises(UnknownModelError, match="not a chat"):
        catalog.resolve("embed-default", "chat")


def _raw(catalog: Catalog) -> dict[str, object]:
    return catalog.model_dump()


def test_embedding_route_with_fallback_is_rejected(catalog: Catalog) -> None:
    raw = _raw(catalog)
    raw["routes"]["embed-default"] = ["ollama/nomic-embed-text", "mock/embed"]  # type: ignore[index]
    with pytest.raises(ValidationError, match="exactly one model"):
        Catalog.model_validate(raw)


def test_mixed_kind_route_is_rejected(catalog: Catalog) -> None:
    raw = _raw(catalog)
    raw["routes"]["broken"] = ["mock/echo", "mock/embed"]  # type: ignore[index]
    with pytest.raises(ValidationError, match="mixes chat and embedding"):
        Catalog.model_validate(raw)


def test_without_provider_prunes_models_and_routes(catalog: Catalog) -> None:
    pruned = catalog.without_provider("mock")
    assert all(m.provider != "mock" for m in pruned.models)
    assert "chat-mock" not in pruned.routes
    assert pruned.routes["chat-default"] == catalog.routes["chat-default"]
