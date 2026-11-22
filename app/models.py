"""Request models and strict validation for the attribution API."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import List, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)


MAX_EVENTS_PER_BATCH = 5_000
MAX_WINDOW_SECONDS = 10 * 365 * 24 * 60 * 60
MAX_CAPACITY = 1_000_000


def parse_timestamp(value: object) -> datetime:
    """Parse an ISO-8601 timestamp and normalise it to UTC.

    Naive timestamps are interpreted as UTC, which keeps local-only clients
    usable without sacrificing deterministic ordering.
    """

    if not isinstance(value, str):
        raise ValueError("occurred_at 必须是 ISO 8601 格式的字符串")
    raw = value.strip()
    if not raw:
        raise ValueError("occurred_at 不能为空")
    candidate = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError("occurred_at 不是合法的 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TouchpointIn(StrictModel):
    id: str = Field(min_length=1, max_length=200)
    occurred_at: datetime
    channel: str = Field(min_length=1, max_length=100)
    # Higher numbers are preferred. Default 0 keeps the field optional.
    priority: StrictInt = Field(default=0, ge=-(2**31), le=2**31 - 1)
    # None means "use the request-level default capacity".
    capacity: Optional[StrictInt] = Field(default=None, ge=0, le=MAX_CAPACITY)

    @field_validator("occurred_at", mode="before")
    @classmethod
    def _validate_occurred_at(cls, value: object) -> datetime:
        return parse_timestamp(value)


class ConversionIn(StrictModel):
    id: str = Field(min_length=1, max_length=200)
    occurred_at: datetime
    value: float = Field(default=0.0, ge=0)

    @field_validator("occurred_at", mode="before")
    @classmethod
    def _validate_occurred_at(cls, value: object) -> datetime:
        return parse_timestamp(value)

    @field_validator("value", mode="before")
    @classmethod
    def _validate_value(cls, value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("value 必须是数字")
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise ValueError("value 必须是有限的非负数")
        return number


class AttributeRequest(StrictModel):
    user_id: str = Field(min_length=1, max_length=200)
    attribution_window_seconds: StrictInt = Field(
        gt=0, le=MAX_WINDOW_SECONDS, description="归因窗口长度（秒）"
    )
    default_touch_capacity: StrictInt = Field(
        default=1,
        ge=0,
        le=MAX_CAPACITY,
        description="未显式声明 capacity 的触点的默认承接上限",
    )
    touchpoints: List[TouchpointIn] = Field(max_length=MAX_EVENTS_PER_BATCH)
    conversions: List[ConversionIn] = Field(max_length=MAX_EVENTS_PER_BATCH)

    @model_validator(mode="after")
    def _require_events(self) -> "AttributeRequest":
        if not self.touchpoints and not self.conversions:
            raise ValueError("批次至少需要包含一个触点或转化事件")
        return self


class ApiError(Exception):
    """An expected API-level error that should be rendered as structured JSON."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        errors: Optional[List[dict]] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.errors = errors or []


__all__ = [
    "ApiError",
    "AttributeRequest",
    "ConversionIn",
    "MAX_CAPACITY",
    "MAX_EVENTS_PER_BATCH",
    "MAX_WINDOW_SECONDS",
    "StrictModel",
    "TouchpointIn",
    "parse_timestamp",
]
