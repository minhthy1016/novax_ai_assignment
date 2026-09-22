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
    assert catalog.routes["chat-default"] == [
        "nim/gpt-oss-20b",
        "claude/sonnet-4.5",
        "ollama/llama3.2-3b",
    ]
    # Every model in the catalog is exercised through the live API by
    # tests/integration/test_models_via_api.py (200 or a controlled error, never a crash).


def test_route_and_model_id_resolution(catalog: Catalog) -> None:
    name, targets = catalog.resolve(None, "chat")
    assert name == "chat-default" and len(targets) == 3
    # Non-picker model id: exactly that model, no fallback.
    _, targets = catalog.resolve("mock/echo", "chat")
    assert [t.id for t in targets] == ["mock/echo"]


def test_picker_has_three_models_and_choice_goes_first(catalog: Catalog) -> None:
    assert catalog.selectable == ["nim/gpt-oss-20b", "claude/sonnet-4.5", "ollama/llama3.2-3b"]
    _, targets = catalog.resolve("claude/sonnet-4.5", "chat")
    assert [t.id for t in targets] == [
        "claude/sonnet-4.5",  # the user's choice first...
        "nim/gpt-oss-20b",  # ...then the other picker models as fallbacks
        "ollama/llama3.2-3b",
    ]


def test_only_picker_models_are_choosable_outside_dev(catalog: Catalog) -> None:
    catalog.check_choice(None, allow_any=False)
    catalog.check_choice("ollama/llama3.2-3b", allow_any=False)
    for not_allowed in ("mock/echo", "demo-failover", "nim/nemotron-3-embed-1b"):
        with pytest.raises(UnknownModelError, match="not selectable"):
            catalog.check_choice(not_allowed, allow_any=False)
    catalog.check_choice("demo-failover", allow_any=True)  # dev/test demos
    with pytest.raises(UnknownModelError):
        catalog.check_choice("gpt-9", allow_any=True)


def test_selectable_must_reference_chat_models(catalog: Catalog) -> None:
    raw = catalog.model_dump()
    raw["selectable"] = ["nim/nemotron-3-embed-1b"]
    with pytest.raises(ValidationError, match="not a chat model"):
        Catalog.model_validate(raw)


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
