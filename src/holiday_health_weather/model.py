"""领域实体（事件重放后的读模型）与常量。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Mapping

HazardKind = Literal["rain", "low_temp", "fog"]
# 数值越大越危险（rain）还是越小越危险（low_temp、fog 能见度）
HAZARD_META: dict[str, dict[str, Any]] = {
    "rain": {"direction": "max", "unit": "mm/h", "label": "降雨强度"},
    "low_temp": {"direction": "min", "unit": "℃", "label": "低温"},
    "fog": {"direction": "min", "unit": "m", "label": "能见度"},
}

LEVEL_NAMES = {0: "无", 1: "注意", 2: "警示", 3: "警告"}

# 建议内容三分类
WEATHER_FACT = "weather_fact"          # 气象事实
HEALTH_MEASURE = "health_measure"      # 健康措施
TRAFFIC_DISPOSAL = "traffic_disposal"  # 交通处置
ITEM_CATEGORIES = (WEATHER_FACT, HEALTH_MEASURE, TRAFFIC_DISPOSAL)


@dataclass(frozen=True, slots=True)
class Measure:
    """一次预报中的单项气象量；``at`` 给出窗口内逐时变化点。"""

    hazard: str
    value: float
    unit: str
    at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ForecastVersion:
    forecast_id: str
    version: int
    area: str
    valid_from: datetime
    valid_to: datetime
    measures: tuple[Measure, ...]
    event_id: str
    issued_at: datetime


@dataclass(frozen=True, slots=True)
class Cutoff:
    level: int
    cutoff: float
    name: str


@dataclass(frozen=True, slots=True)
class Threshold:
    threshold_id: str
    area: str
    rules: Mapping[str, tuple[Cutoff, ...]]
    version: int
    event_id: str


@dataclass(frozen=True, slots=True)
class Segment:
    segment_id: str
    area: str
    from_name: str
    to_name: str
    mountain: bool = False
    scenic_area_id: str | None = None
    medical_point_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Route:
    route_id: str
    area: str
    name: str
    segments: tuple[Segment, ...]
    version: int
    event_id: str


@dataclass(frozen=True, slots=True)
class FocusPerson:
    person_id: str
    name: str
    groups: tuple[str, ...]
    devices: tuple[str, ...]
    version: int = 1
    event_id: str = ""


@dataclass(frozen=True, slots=True)
class MedicalPoint:
    point_id: str
    area: str
    name: str
    backup_point_id: str | None
    active: bool
    version: int
    event_id: str


@dataclass(frozen=True, slots=True)
class Authorization:
    auth_id: str
    area: str
    channels: tuple[str, ...]
    granted: bool
    version: int
    event_id: str


@dataclass(frozen=True, slots=True)
class ScenicClosure:
    scenic_area_id: str
    name: str
    area: str
    closed_from: datetime
    closed_to: datetime
    reason: str
    active: bool
    version: int
    event_id: str


@dataclass(frozen=True, slots=True)
class Trip:
    trip_id: str
    person_id: str
    route_id: str
    destination: str
    segment_ids: tuple[str, ...]
    version: int
    event_id: str


@dataclass(frozen=True, slots=True)
class Evidence:
    """一条建议内容的可追溯依据。"""

    kind: str           # forecast / threshold / route / medical_point / scenic_closure / person
    aggregate_id: str
    version: int
    event_id: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "aggregate_id": self.aggregate_id,
            "version": self.version,
            "event_id": self.event_id,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class AdvisoryItem:
    category: str
    text: str
    evidence: tuple[Evidence, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "text": self.text,
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(frozen=True, slots=True)
class Basis:
    """整版建议所依据的预报版本。"""

    forecast_id: str
    forecast_version: int
    event_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "forecast_id": self.forecast_id,
            "forecast_version": self.forecast_version,
            "event_id": self.event_id,
        }


@dataclass(frozen=True, slots=True)
class Receipt:
    person_id: str
    stage: str            # acknowledged / completed
    devices: tuple[str, ...]
    at: datetime
    event_id: str


@dataclass(frozen=True, slots=True)
class Delivery:
    at: datetime
    event_id: str
    recipients: tuple[dict[str, str], ...]


@dataclass(frozen=True, slots=True)
class NoticeSnapshot:
    """建议每次草稿/重算后的内容留档，送达后永久保留当时依据。"""

    version: int
    at: datetime
    level: int
    items: tuple[AdvisoryItem, ...]
    basis: Basis
    reason: str
    event_id: str


@dataclass(eq=False, slots=True)
class NoticeState:
    notice_id: str
    trip_id: str
    segment_id: str
    area: str
    planned_at: datetime
    status: str = "drafted"  # drafted / approved / sent / superseded
    level: int = 0
    items: tuple[AdvisoryItem, ...] = ()
    basis: Basis | None = None
    history: list[NoticeSnapshot] = field(default_factory=list)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    delivery: Delivery | None = None
    receipts: dict[tuple[str, str], Receipt] = field(default_factory=dict)
