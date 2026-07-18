"""Smoke tests for journal_rag.py: embed/index/search over a moto-backed table.

Bedrock embedding calls are stubbed (no real model dependency) — these tests
cover the deterministic plumbing (enabled-gating, packing, cosine ranking,
sync-from-write helpers) rather than embedding quality.
"""

from __future__ import annotations

import importlib
import json


def _reload_journal_rag(monkeypatch, *, embedding_model: str | None = "amazon.titan-embed-text-v2:0"):
    if embedding_model:
        monkeypatch.setenv("BEDROCK_EMBEDDING_MODEL_ID", embedding_model)
    else:
        monkeypatch.delenv("BEDROCK_EMBEDDING_MODEL_ID", raising=False)
    import journal_rag

    importlib.reload(journal_rag)
    return journal_rag


def _fake_embed_response(vec: list[float]):
    class _Body:
        def read(self):
            return json.dumps({"embedding": vec}).encode()

    return {"body": _Body()}


def test_disabled_without_embedding_model(dynamodb_env, monkeypatch):
    rag = _reload_journal_rag(monkeypatch, embedding_model=None)
    assert rag.enabled() is False
    assert rag.embed_text("some brew notes") is None

    out = rag.search("u1", "fruity naturals")
    assert out["ok"] is False
    assert "not configured" in out["error"]


def test_embed_text_calls_bedrock_and_parses_vector(dynamodb_env, monkeypatch):
    rag = _reload_journal_rag(monkeypatch)
    assert rag.enabled() is True

    captured = {}

    class FakeBedrock:
        def invoke_model(self, **kwargs):
            captured.update(kwargs)
            return _fake_embed_response([0.1, 0.2, 0.3])

    monkeypatch.setattr(rag, "_br", lambda: FakeBedrock())

    vec = rag.embed_text("Ethiopia Guji, washed, floral and stone fruit")
    assert vec == [0.1, 0.2, 0.3]
    assert captured["modelId"] == "amazon.titan-embed-text-v2:0"
    body = json.loads(captured["body"])
    assert "floral and stone fruit" in body["inputText"]


def test_embed_text_returns_none_on_bedrock_failure(dynamodb_env, monkeypatch):
    rag = _reload_journal_rag(monkeypatch)

    def boom():
        raise RuntimeError("bedrock unavailable")

    monkeypatch.setattr(rag, "_br", boom)
    assert rag.embed_text("anything") is None


def test_embed_text_empty_input_short_circuits(dynamodb_env, monkeypatch):
    rag = _reload_journal_rag(monkeypatch)
    monkeypatch.setattr(rag, "_br", lambda: (_ for _ in ()).throw(AssertionError("should not call Bedrock")))
    assert rag.embed_text("   ") is None


def test_chunk_sk_validates_kind(dynamodb_env, monkeypatch):
    rag = _reload_journal_rag(monkeypatch)
    assert rag.chunk_sk("brew", "b1") == "RAGCHUNK#BREW#b1"
    try:
        rag.chunk_sk("bogus", "x")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_upsert_and_search_ranks_by_cosine_similarity(dynamodb_env, monkeypatch):
    rag = _reload_journal_rag(monkeypatch)

    vectors = {
        "fruity ethiopia natural": [1.0, 0.0, 0.0],
        "washed colombia clean": [0.0, 1.0, 0.0],
        "query: fruity anaerobic": [0.9, 0.1, 0.0],
    }

    def fake_embed(text: str):
        for key, vec in vectors.items():
            if key in text:
                return vec
        return [0.0, 0.0, 1.0]

    monkeypatch.setattr(rag, "embed_text", fake_embed)

    rag.upsert_chunk(
        "u1",
        kind="coffee",
        entity_id="c1",
        text_for_embedding="fruity ethiopia natural",
        display_text="Ethiopia Guji — fruity natural",
        refs={"coffeeId": "c1"},
    )
    rag.upsert_chunk(
        "u1",
        kind="coffee",
        entity_id="c2",
        text_for_embedding="washed colombia clean",
        display_text="Colombia Huila — clean washed",
        refs={"coffeeId": "c2"},
    )

    out = rag.search("u1", "query: fruity anaerobic", top_k=2)
    assert out["ok"] is True
    assert out["chunksLoaded"] == 2
    assert len(out["hits"]) == 2
    # The fruity/natural chunk (closer cosine to the fruity query vector) ranks first.
    assert out["hits"][0]["text"] == "Ethiopia Guji — fruity natural"
    assert out["hits"][0]["score"] > out["hits"][1]["score"]


def test_search_with_no_indexed_chunks(dynamodb_env, monkeypatch):
    rag = _reload_journal_rag(monkeypatch)
    monkeypatch.setattr(rag, "embed_text", lambda text: [1.0, 0.0])

    out = rag.search("u-empty", "anything")
    assert out["ok"] is True
    assert out["chunksLoaded"] == 0
    assert out["hits"] == []


def test_search_requires_nonempty_query(dynamodb_env, monkeypatch):
    rag = _reload_journal_rag(monkeypatch)
    out = rag.search("u1", "   ")
    assert out["ok"] is False
    assert "query is required" in out["error"]


def test_upsert_chunk_with_empty_text_deletes(dynamodb_env, monkeypatch):
    rag = _reload_journal_rag(monkeypatch)
    monkeypatch.setattr(rag, "embed_text", lambda text: [1.0, 0.0])

    rag.upsert_chunk(
        "u1", kind="visit", entity_id="v1", text_for_embedding="great cortado",
        display_text="great cortado", refs={"visitId": "v1"},
    )
    out = rag.search("u1", "cortado")
    assert out["chunksLoaded"] == 1

    rag.upsert_chunk(
        "u1", kind="visit", entity_id="v1", text_for_embedding="",
        display_text="", refs={"visitId": "v1"},
    )
    out = rag.search("u1", "cortado")
    assert out["chunksLoaded"] == 0


def test_sync_brew_indexes_taste_and_notes(dynamodb_env, monkeypatch):
    rag = _reload_journal_rag(monkeypatch)
    captured_text = {}

    def fake_embed(text: str):
        captured_text["text"] = text
        return [1.0, 0.0]

    monkeypatch.setattr(rag, "embed_text", fake_embed)

    brew = {
        "brewId": "b1",
        "coffeeId": "c1",
        "method": "V60",
        "doseG": 15,
        "yieldG": 250,
        "ratio": 16.7,
        "grind": "Ode 4",
        "rating": 9,
        "taste": "bright, floral",
        "notes": "best cup yet",
    }
    coffee = {"name": "Ethiopia Guji", "roaster": "Sey"}
    rag.sync_brew("u1", brew, coffee)

    assert "bright, floral" in captured_text["text"]
    assert "best cup yet" in captured_text["text"]
    assert "Ethiopia Guji" in captured_text["text"]

    out = rag.search("u1", "bright floral")
    assert out["chunksLoaded"] == 1
    assert out["hits"][0]["refs"]["brewId"] == "b1"


def test_sync_coffee_archived_deletes_chunk(dynamodb_env, monkeypatch):
    rag = _reload_journal_rag(monkeypatch)
    monkeypatch.setattr(rag, "embed_text", lambda text: [1.0, 0.0])

    rag.sync_coffee("u1", {"coffeeId": "c1", "name": "Kenya Nyeri", "notes": "citrus"})
    assert rag.search("u1", "citrus")["chunksLoaded"] == 1

    rag.sync_coffee("u1", {"coffeeId": "c1", "name": "Kenya Nyeri", "archived": True})
    assert rag.search("u1", "citrus")["chunksLoaded"] == 0


def test_try_sync_helpers_swallow_errors(dynamodb_env, monkeypatch):
    rag = _reload_journal_rag(monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("ddb down")

    monkeypatch.setattr(rag.ddb, "get_coffee", boom)
    # Must not raise — failures are logged, not propagated (writes must never fail on index issues).
    rag.try_sync_brew("u1", {"brewId": "b1", "coffeeId": "c1"})
