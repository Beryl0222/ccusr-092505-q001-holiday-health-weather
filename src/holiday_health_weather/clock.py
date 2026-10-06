"""可注入的时间源。

业务规则一律不直接读取系统时钟：值班联调与测试时注入
:class:`VirtualClock` 推进时间，生产环境再换成真实时钟实现。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

CST = timezone(timedelta(hours=8))


class Clock(Protocol):
    def now(self) -> datetime:
        """返回带时区的当前时间。"""


class VirtualClock:
    """可手动设定、推进的虚拟时钟，默认使用东八区。"""

    def __init__(self, start: datetime | None = None) -> None:
        if start is None:
            start = datetime(2026, 10, 1, 0, 0, tzinfo=CST)
        self._now = _aware(start)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta | None = None, **kwargs: float) -> datetime:
        """按时间差或关键字（hours=、minutes= 等）推进，返回新时刻。"""
        if delta is None:
            delta = timedelta(**kwargs)
        self._now = _aware(self._now + delta)
        return self._now

    def set(self, value: datetime) -> datetime:
        self._now = _aware(value)
        return self._now


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc).astimezone()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return value
