from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=8, max_length=256)


class ManagedUserInput(BaseModel):
    username: str = Field(default="", max_length=32)
    role: Literal["admin", "user"] = "user"


class ManagedUserAction(BaseModel):
    id: int = Field(gt=0)
    action: Literal["reset_password", "set_active"]
    active: bool | None = None


class IPBlockInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ip: str = Field(min_length=1, max_length=80)
    reason: str = Field(default="", max_length=200)


class IPBlockAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ip: str = Field(min_length=1, max_length=80)
    active: bool


class BatchRevealKeyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(gt=0, le=9_223_372_036_854_775_807, strict=True)
    type: str = Field(min_length=1, max_length=64, strict=True)
    status: Literal["valid", "usage_limit_reached", "insufficient_credit", "invalid"]


class BatchRevealInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keys: list[BatchRevealKeyInput] = Field(min_length=1, max_length=100)

    @field_validator("keys")
    @classmethod
    def unique_keys(cls, keys: list[BatchRevealKeyInput]) -> list[BatchRevealKeyInput]:
        identities = [(item.type, item.status, item.id) for item in keys]
        if len(identities) != len(set(identities)):
            raise ValueError("所选密钥不能重复")
        return keys


class BatchStartInput(BaseModel):
    provider_label: str = Field(default="Unknown", max_length=120)
    total: int = Field(ge=1, le=1_000_000)


class CredentialInput(BaseModel):
    provider: str = Field(min_length=1, max_length=80)
    kind: str = Field(min_length=1, max_length=80)
    storage_type: str | None = Field(default=None, max_length=80)
    storageType: str | None = Field(default=None, max_length=80)
    secret: str = Field(min_length=1, max_length=16_384)
    masked: str = Field(default="", max_length=300)
    category: str = Field(default="未分类", max_length=80)

    @property
    def requested_storage_type(self) -> str | None:
        return self.storage_type or self.storageType


class ValidationResultInput(BaseModel):
    index: int = Field(ge=1)
    status: str = Field(min_length=1, max_length=80)
    usable: bool = False
    credential: CredentialInput
    model: str | None = Field(default=None, max_length=300)
    http_status: int | None = None
    attempts: int | None = None
    duration_ms: int | None = None
    region: str | None = Field(default=None, max_length=120)
    message: str | None = Field(default=None, max_length=2_000)
    reset_at: str | None = Field(default=None, max_length=120)


class BatchCompleteInput(BaseModel):
    results: list[ValidationResultInput] = Field(min_length=1, max_length=50)

    @field_validator("results")
    @classmethod
    def unique_indexes(cls, results: list[ValidationResultInput]) -> list[ValidationResultInput]:
        indexes = [item.index for item in results]
        if len(indexes) != len(set(indexes)):
            raise ValueError("同一分片内的结果序号不能重复")
        return results
