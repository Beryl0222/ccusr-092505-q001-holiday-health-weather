"""领域事件定义。

事件信封遵循 ``contracts/domain.schema.json``：每个事件有全局唯一的
``event_id``、聚合标识、单调版本号与带时区的发生时间。事件一经写入只追加，
已送达建议所依据的事实因此可以永久复核。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

# 已登记的事件类型（与 contracts/domain.schema.json 保持同步）
EVENT_TYPES: frozenset[str] = frozenset(
    {
        "FORECAST_ISSUED",
        "FORECAST_REVISED",
        "THRESHOLD_REGISTERED",
        "THRESHOLD_UPDATED",
        "ROUTE_REGISTERED",
        "ROUTE_REROUTED",
        "PERSON_REGISTERED",
        "MEDICAL_POINT_REGISTERED",
        "MEDICAL_POINT_WITHDRAWN",
        "SCENIC_CLOSURE_NOTIFIED",
        "SCENIC_REOPENED",
        "AUTHORIZATION_GRANTED",
        "AUTHORIZATION_REVOKED",
        "TRIP_REGISTERED",
        "TRIP_DESTINATION_CHANGED",
        "ADVISORY_DRAFTED",
        "ADVISORY_APPROVED",
        "ADVISORY_SENT",
        "ADVISORY_RECALCULATED",
        "RISK_ESCALATED",
        "RECEIPT_ACKNOWLEDGED",
        "RECEIPT_COMPLETED",
    }
)

AGGREGATE_TYPES: frozenset[str] = frozenset(
    {
        "forecast",
        "risk_threshold",
        "travel_segment",
        "focus_person",
        "medical_point",
        "scenic_closure",
        "authorization",
        "trip",
        "health_notice",
    }
)


@dataclass(frozen=True, slots=True)
class Event:
    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    occurred_at: datetime
    version: int
    summary: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    # 产生该事件的因果链：如重算触发事件、升级所依据的预报版本
    causation_id: str | None = None
    # 同一业务意图的相关事件归到同一关联串（如旅客行程、某次断网补传批次）
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        if not self.event_id or not self.event_id.strip():
            raise ValueError("event_id 必须是非空字符串")
        if self.event_type not in EVENT_TYPES:
            raise ValueError(f"未登记的事件类型: {self.event_type}")
        if self.aggregate_type not in AGGREGATE_TYPES:
            raise ValueError(f"未登记的聚合类型: {self.aggregate_type}")
        if not self.aggregate_id or not self.aggregate_id.strip():
            raise ValueError("aggregate_id 必须是非空字符串")
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at 必须包含时区")
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 1:
            raise ValueError("version 必须是正整数")

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "occurred_at": self.occurred_at.isoformat(),
            "version": self.version,
            "summary": self.summary,
            "payload": dict(self.payload),
        }
        if self.causation_id:
            data["causation_id"] = self.causation_id
        if self.correlation_id:
            data["correlation_id"] = self.correlation_id
        return data
