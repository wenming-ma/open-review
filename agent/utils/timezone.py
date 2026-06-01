"""Shared application timezone helpers."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

OPEN_REVIEW_TIMEZONE_NAME = "Asia/Shanghai"
_OPEN_REVIEW_TIMEZONE = ZoneInfo(OPEN_REVIEW_TIMEZONE_NAME)


def open_review_timezone() -> ZoneInfo:
    return _OPEN_REVIEW_TIMEZONE


def now_in_open_review_tz() -> datetime:
    return datetime.now(_OPEN_REVIEW_TIMEZONE)


def iso_now() -> str:
    return now_in_open_review_tz().isoformat()


def parse_iso_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def to_open_review_tz(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=_OPEN_REVIEW_TIMEZONE)
    return value.astimezone(_OPEN_REVIEW_TIMEZONE)


def compact_timestamp(value: datetime | None = None) -> str:
    target = now_in_open_review_tz() if value is None else to_open_review_tz(value)
    return target.strftime("%Y%m%d%H%M%S")


def format_beijing_display(value: datetime) -> str:
    return f"{to_open_review_tz(value).strftime('%Y-%m-%d %H:%M:%S')} 北京时间"
