"""The shared event format. See docs/EVENTS.md."""
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# spoof (#99): answered with Eddie's own IP so the stub server (spoof_server.py) replies.
Action = Literal["allow", "block", "flag", "spoof"]
Source = Literal["rule", "allowlist", "blocklist", "heuristic", "ai", "home_mode", "away_mode", "default"]


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


MAX_AWAY_MS = 30 * 86_400_000  # away mode's end time is at most 30 days ahead


class ModeUpdate(BaseModel):
    """Home mode, away mode (#109) or both. away_until (ms) is optional and only with away on."""
    home_mode: bool | None = None
    away: bool | None = None
    away_until: int | None = None

    @model_validator(mode="after")
    def check(self) -> "ModeUpdate":
        if self.home_mode is None and self.away is None:
            raise ValueError("expected home_mode or away")
        if self.away_until is not None:
            now = time.time() * 1000
            if self.away is not True:
                raise ValueError("away_until only goes with away: true")
            if not now < self.away_until <= now + MAX_AWAY_MS:
                raise ValueError("away_until must be in the next 30 days, in ms")
        return self


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
