"""只追加的事件登记簿。

解决三类现场问题：

* 断网恢复后工作人员重复点"上报"——相同 ``event_id`` 或相同业务指纹的
  事件只生效一次，第二次调用原样返回已登记事件；
* 晚到消息——事件按 *发生时间* 而不是 *到达时间* 参与重放，离线期间积压
  的旧事实补传后仍能落到正确的时间位置；
* 并发改写——按聚合做乐观版本检查，版本不连续时拒绝并留痕，而不是悄悄覆盖。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any, Mapping

from .event import Event


class EventConflictError(Exception):
    """同一 event_id 以不同内容重复提交，或聚合版本发生冲突。"""

    def __init__(self, message: str, *, existing: Event | None = None) -> None:
        super().__init__(message)
        self.existing = existing


def business_fingerprint(event_type: str, aggregate_id: str, payload: Mapping[str, Any]) -> str:
    """忽略 event_id 与时间戳的业务指纹，用于识别重复上报。

    同一条业务事实在断网前、补传后 event_id 可能相同也可能不同，但
    (类型, 聚合, 业务内容) 一致时视为同一次上报。
    """

    body = json.dumps(
        {"t": event_type, "a": aggregate_id, "p": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"{event_type}:{aggregate_id}:{hashlib.sha256(body.encode('utf-8')).hexdigest()[:16]}"


class EventStore:
    def __init__(self) -> None:
        self._events: list[Event] = []
        self._by_id: dict[str, Event] = {}
        self._seq: dict[str, int] = {}
        self._next_seq = 0
        self._versions: dict[str, int] = {}
        self._fingerprints: dict[str, str] = {}
        # 被拒绝的提交同样留痕，便于值班复盘"为什么这条没进去"
        self.rejections: list[dict[str, Any]] = []

    # ---- 写入 ----------------------------------------------------------

    def append(
        self,
        event: Event,
        *,
        expected_version: int | None = None,
        fingerprint: str | None = None,
    ) -> tuple[Event, bool]:
        """登记事件，返回 ``(事件, 是否为重复而被合并)``。

        expected_version 为该聚合当前应有的版本（新聚合传 0）；
        fingerprint 非空时，相同指纹的事件只生效一次。
        """

        duplicate = self._find_duplicate(event, fingerprint)
        if duplicate is not None:
            return duplicate, True

        current = self._versions.get(event.aggregate_id, 0)
        if expected_version is not None and current != expected_version:
            self._reject(event, "version_conflict", f"期望版本 {expected_version}，当前 {current}")
            raise EventConflictError(
                f"聚合 {event.aggregate_id} 版本冲突：期望 {expected_version}，当前 {current}",
                existing=self._latest(event.aggregate_id),
            )
        if event.version != current + 1:
            self._reject(event, "version_gap", f"事件版本 {event.version}，应为 {current + 1}")
            raise EventConflictError(
                f"聚合 {event.aggregate_id} 版本不连续：事件 {event.version}，当前 {current}"
            )

        self._events.append(event)
        self._by_id[event.event_id] = event
        self._seq[event.event_id] = self._next_seq
        self._next_seq += 1
        self._versions[event.aggregate_id] = event.version
        if fingerprint is not None:
            self._fingerprints[fingerprint] = event.event_id
        return event, False

    def append_many(self, events: Iterable[Event]) -> list[tuple[Event, bool]]:
        """批量补传（如网络恢复后的同步），逐条安全合并。"""

        return [self.append(event) for event in events]

    # ---- 读取 ----------------------------------------------------------

    def all_events(self) -> list[Event]:
        """按发生时间重放；同一时刻按到达顺序，保证晚到消息落到正确位置。"""

        return sorted(self._events, key=lambda item: (item.occurred_at, self._seq[item.event_id]))

    def events_for(self, aggregate_id: str) -> list[Event]:
        return [event for event in self.all_events() if event.aggregate_id == aggregate_id]

    def get(self, event_id: str) -> Event | None:
        return self._by_id.get(event_id)

    def current_version(self, aggregate_id: str) -> int:
        return self._versions.get(aggregate_id, 0)

    def known_fingerprint(self, fingerprint: str) -> bool:
        return fingerprint in self._fingerprints

    # ---- 内部 ----------------------------------------------------------

    def _find_duplicate(self, event: Event, fingerprint: str | None) -> Event | None:
        existing = self._by_id.get(event.event_id)
        if existing is not None:
            if existing.to_dict() != event.to_dict():
                self._reject(event, "id_conflict", f"event_id {event.event_id} 已用于不同内容")
                raise EventConflictError(
                    f"event_id {event.event_id} 已登记为不同内容", existing=existing
                )
            return existing
        if fingerprint is not None and fingerprint in self._fingerprints:
            return self._by_id[self._fingerprints[fingerprint]]
        return None

    def _latest(self, aggregate_id: str) -> Event | None:
        events = [event for event in self._events if event.aggregate_id == aggregate_id]
        return events[-1] if events else None

    def _reject(self, event: Event, code: str, reason: str) -> None:
        self.rejections.append(
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "aggregate_id": event.aggregate_id,
                "code": code,
                "reason": reason,
            }
        )
