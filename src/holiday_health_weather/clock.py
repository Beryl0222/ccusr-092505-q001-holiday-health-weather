"""可注入的时间来源，让分级升级与复盘可以按演练节奏推进。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    """服务读取当前时间的唯一入口。"""

    def now(self) -> datetime: ...


class SystemClock:
    """生产环境使用的系统时钟，统一返回带时区的时间。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ManualClock:
    """演练与测试使用的手动时钟，只能向前推进。"""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("手动时钟起点必须带时区")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        if delta.total_seconds() < 0:
            raise ValueError("手动时钟不允许回拨")
        self._now = self._now + delta
        return self._now
