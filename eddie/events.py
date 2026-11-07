"""The shared event format. See docs/EVENTS.md."""
from typing import Literal

from pydantic import BaseModel, field_validator

Action = Literal["allow", "block", "flag"]
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
