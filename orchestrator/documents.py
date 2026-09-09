"""Validated MongoDB registry documents and lifecycle updates."""

import math
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

DocumentId = Annotated[str, StringConstraints(min_length=1, pattern=r"^\S+$")]
Timestamp = Annotated[float, Field(ge=0)]
SessionStatus = Literal["allocating", "ready", "deleting"]
RetentionPolicy = Literal["managed", "ephemeral"]


class Document(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        strict=True,
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )

    def to_mongo(self) -> dict:
        """BSON-compatible primitives; alias id as MongoDB's _id."""
        return self.model_dump(mode="python", by_alias=True)


class AgentDocument(Document):
    id: DocumentId = Field(alias="_id")


class BindingDocument(Document):
    id: DocumentId = Field(alias="_id")
    agent_id: DocumentId
    sandbox_id: DocumentId
    status: SessionStatus
    retention: RetentionPolicy
    idle_ttl_seconds: int = Field(ge=0)
    created_at: Timestamp
    last_activity: Timestamp
    expires_at: Timestamp | None

    @model_validator(mode="after")
    def validate_expiration(self) -> Self:
        if self.retention == "managed":
            if self.idle_ttl_seconds != 0 or self.expires_at is not None:
                raise ValueError("Managed bindings require zero TTL and null expires_at")
        elif self.idle_ttl_seconds <= 0 or self.expires_at is None:
            raise ValueError("Ephemeral bindings require positive TTL and expires_at")
        elif not math.isclose(
            self.expires_at,
            self.last_activity + self.idle_ttl_seconds,
            rel_tol=0,
            abs_tol=1e-6,
        ):
            raise ValueError("expires_at must equal last_activity plus idle_ttl_seconds")
        return self

    def public(self) -> dict:
        return self.model_dump(mode="python", by_alias=False)


class BindingUpdate(Document):
    """Only lifecycle fields are mutable; omission and explicit null are different."""

    status: SessionStatus | None = None
    retention: RetentionPolicy | None = None
    idle_ttl_seconds: int | None = Field(default=None, ge=0)
    last_activity: Timestamp | None = None
    expires_at: Timestamp | None = None

    @model_validator(mode="after")
    def reject_null_nonnullable_fields(self) -> Self:
        for name in self.model_fields_set - {"expires_at"}:
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self

    def apply(self, document: BindingDocument) -> BindingDocument:
        # model_copy(update=...) bypasses validation; validate the entire merged document.
        return BindingDocument.model_validate(
            document.public() | self.model_dump(exclude_unset=True)
        )
