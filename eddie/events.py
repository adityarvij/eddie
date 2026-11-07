"""The shared event format. See docs/EVENTS.md."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# spoof (#99): answered with Eddie's own IP so the stub server (spoof_server.py) replies.
Action = Literal["allow", "block", "flag", "spoof"]
Source = Literal["rule", "allowlist", "blocklist", "heuristic", "ai", "home_mode", "default"]


class QueryEvent(BaseModel):
    type: Literal["query"] = "query"
    ts: int
    device_mac: str
    device: str
    domain: str
    qtype: str = "A"
    action: Action
    source: Source
    reason: str = ""

    @field_validator("domain")
    @classmethod
    def normalize_domain(cls, v: str) -> str:
        return v.strip().lower().rstrip(".")


class ModeUpdate(BaseModel):
    home_mode: bool


class OfflineUpdate(BaseModel):
    offline: bool


class AIVerdictEvent(BaseModel):
    model_config = ConfigDict(extra='forbid')
    domain: str = Field(min_length=1, max_length=253, pattern=r'^[a-z0-9_-]+(?:\.[a-z0-9_-]+)*$')
    label: Literal['tracker', 'needed', 'unsure']
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False, strict=True)
    reason: str = Field(min_length=1, max_length=120)
    ts: int = Field(ge=0, strict=True)

    @field_validator('reason')
    @classmethod
    def valid_reason(cls, reason):
        if not reason.isprintable() or len(reason.split()) > 14 or not reason.strip():
            raise ValueError('Expected a short printable reason')
        return reason
