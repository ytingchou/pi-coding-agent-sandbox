"""Document validation and the MongoDB/API serialization boundary."""

import pytest
from pydantic import ValidationError

from orchestrator.documents import (
    AgentDocument,
    BindingDocument,
    BindingUpdate,
)


def binding(**updates):
    return {
        "_id": "session-a",
        "agent_id": "agent-a",
        "sandbox_id": "worker-0",
        "status": "ready",
        "retention": "managed",
        "idle_ttl_seconds": 0,
        "created_at": 10.0,
        "last_activity": 20.0,
        "expires_at": None,
        **updates,
    }


def test_document_round_trip_preserves_existing_bson_shape():
    row = binding()
    document = BindingDocument.model_validate(row)
    assert document.to_mongo() == row
    assert "_id" not in document.public() and document.public()["id"] == "session-a"
    assert BindingDocument.model_validate(document.public()) == document
    assert AgentDocument(id="agent-a").to_mongo() == {"_id": "agent-a"}
    with pytest.raises(ValidationError):
        document.status = "deleting"


@pytest.mark.parametrize(
    "updates",
    [
        {"status": "unknown"},
        {"retention": "temporary"},
        {"_id": ""},
        {"agent_id": "a b"},
        {"sandbox_id": 10},
        {"created_at": -1},
        {"last_activity": float("nan")},
        {"last_activity": float("inf")},
        {"idle_ttl_seconds": True},
        {"idle_ttl_seconds": "10"},
        {"retention": "managed", "idle_ttl_seconds": 1},
        {"retention": "managed", "expires_at": 20.0},
        {"retention": "ephemeral", "idle_ttl_seconds": 0, "expires_at": 20.0},
        {"retention": "ephemeral", "idle_ttl_seconds": 10, "expires_at": None},
        {"retention": "ephemeral", "idle_ttl_seconds": 10, "expires_at": 31.0},
        {"unexpected": "field"},
    ],
)
def test_reject_invalid_binding(updates):
    with pytest.raises(ValidationError):
        BindingDocument.model_validate(binding(**updates))


def test_patch_validates_whole_document_and_preserves_immutable_fields():
    original = BindingDocument.model_validate(binding())
    changed = BindingUpdate(retention="ephemeral", idle_ttl_seconds=10, expires_at=30).apply(
        original
    )
    assert changed.expires_at == 30 and original.retention == "managed"
    restored = BindingUpdate(retention="managed", idle_ttl_seconds=0, expires_at=None).apply(
        changed
    )
    assert restored.expires_at is None
    assert BindingUpdate(status="deleting").apply(changed).status == "deleting"
    with pytest.raises(ValidationError):
        BindingUpdate(last_activity=25).apply(changed)  # Must advance expiration too.
    for fields in (
        {"agent_id": "other"},
        {"id": "other"},
        {"_id": "other"},
        {"sandbox_id": "other"},
        {"created_at": 0},
        {"status": None},
    ):
        with pytest.raises(ValidationError):
            BindingUpdate.model_validate(fields)


@pytest.mark.asyncio
async def test_repository_validates_insert_update_and_reads(tmp_path, registry_factory):
    registry = registry_factory(tmp_path)
    fields = binding()
    sid = fields.pop("_id")
    with pytest.raises(ValidationError):
        await registry.insert(sid, **(fields | {"status": "invalid"}))
    assert await registry.binding(sid) is None
    await registry.insert(sid, **fields)
    for fields in ({"status": None}, {"retention": "ephemeral"}, {"agent_id": "other"}):
        with pytest.raises(ValidationError):
            await registry.update(sid, **fields)
    assert (await registry.binding(sid))["retention"] == "managed"
    await registry.update(sid, retention="ephemeral", idle_ttl_seconds=10, expires_at=30)
    assert (await registry.binding(sid))["expires_at"] == 30
    registry.db.bindings.update_one({"_id": sid}, {"$set": {"retention": "corrupt-secret"}})
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await registry.binding(sid)
    assert exc.value.status_code == 503 and "corrupt-secret" not in exc.value.detail


@pytest.mark.asyncio
async def test_index_keys_match_queries_without_ttl(tmp_path, registry_factory):
    registry = registry_factory(tmp_path)
    # Older deployments may retain this prefix index during a safe rollout.
    registry.db.bindings.create_index([("agent_id", 1), ("created_at", 1)])
    await registry.initialize()
    await registry.initialize()
    indexes = list(registry.db.bindings.list_indexes())
    keys = {tuple(index["key"].items()) for index in indexes}
    assert (("_id", 1),) in keys
    assert (("agent_id", 1), ("created_at", 1), ("_id", 1)) in keys
    assert (("sandbox_id", 1),) in keys
    assert (("status", 1),) in keys
    assert (("retention", 1), ("expires_at", 1)) in keys
    assert all("expireAfterSeconds" not in index for index in indexes)
