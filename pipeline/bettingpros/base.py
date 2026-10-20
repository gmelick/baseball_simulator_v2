"""The state every part of the BettingPros provider shares."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, ClassVar

from pipeline.bettingpros.schedule import _ScheduleInfo


class ProviderBase:
    """The provider's settings and caches, declared once so each mixin sees them."""

    API_KEY_ENV: ClassVar[str]
    MAX_RETRIES_ENV: ClassVar[str]
    RETRY_WAIT_ENV: ClassVar[str]
    DEFAULT_MAX_RETRIES: ClassVar[int]
    DEFAULT_RETRY_WAIT_S: ClassVar[float]
    RETRY_WAIT_CAP_S: ClassVar[float]
    OFFERS_CACHE_TTL_ENV: ClassVar[str]
    DEFAULT_OFFERS_CACHE_TTL_S: ClassVar[float]
    _api_key: str | None
    _prefer_book_id: int | None
    _timeout: float
    _offers_cache_ttl_s: float
    _clock: Callable[[], float]
    _max_retries: int
    _retry_wait_s: float
    _retry_sleep: Callable[[float], None]
    read_failures: int
    _event_cache: dict[int, dict[str, Any]]
    _game_meta_cache: dict[int, tuple[str, str, str, datetime | None]]
    _game_schedule_cache: dict[int, _ScheduleInfo]
    _player_name_cache: dict[int, str]
    _failed_lookups: dict[tuple[str, int], tuple[float, None]]
    _offers_cache: dict[tuple[int, int], tuple[float, list[dict[str, Any]]]]
    _events_by_date_cache: dict[str, tuple[float, list[dict[str, Any]]]]
    _f5_logged: set[tuple[int, int, str]]
