"""假日气象健康联动台的核心领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum


class HazardType(str, Enum):
    """纳入联动处置的局地气象风险。"""

    LOCAL_RAINSTORM = "LOCAL_RAINSTORM"  # 局地暴雨
    LOW_TEMPERATURE = "LOW_TEMPERATURE"  # 低温
    LIGHT_FOG = "LIGHT_FOG"  # 轻雾


class EscalationLevel(str, Enum):
    """分级升级档位，按风险持续时间逐级推进。"""

    NONE = "NONE"
    ATTENTION = "ATTENTION"  # 关注
    WARNING = "WARNING"  # 警戒
    SEVERE = "SEVERE"  # 严重


_LEVEL_ORDER = {
    EscalationLevel.NONE: 0,
    EscalationLevel.ATTENTION: 1,
    EscalationLevel.WARNING: 2,
    EscalationLevel.SEVERE: 3,
}


def level_rank(level: EscalationLevel) -> int:
    return _LEVEL_ORDER[level]


class PartKind(str, Enum):
    """一条建议拆成的三类可追溯内容。"""

    WEATHER_FACT = "WEATHER_FACT"  # 气象事实
    HEALTH_MEASURE = "HEALTH_MEASURE"  # 健康措施
    TRAFFIC_HANDLING = "TRAFFIC_HANDLING"  # 交通处置


class NoticeStatus(str, Enum):
    DRAFT = "DRAFT"  # 草拟，尚未生效
    APPROVED = "APPROVED"  # 已批准，尚未送达
    SENT = "SENT"  # 已送达，依据从此冻结


@dataclass(frozen=True)
class HazardObservation:
    hazard: HazardType
    started_at: datetime
    detail: str = ""


@dataclass(frozen=True)
class ForecastVersion:
    """按地区和有效时间登记的某一版气象预报。"""

    forecast_id: str
    region: str
    version: int
    issued_at: datetime
    effective_from: datetime
    effective_to: datetime
    hazards: tuple[HazardObservation, ...] = ()

    def covers(self, moment: datetime) -> bool:
        return self.effective_from <= moment < self.effective_to


@dataclass(frozen=True)
class RiskThreshold:
    """某地区某类风险随持续时间推进的分级阈值。"""

    threshold_id: str
    region: str
    hazard: HazardType
    attention_after: timedelta
    warning_after: timedelta
    severe_after: timedelta

    def level_for(self, elapsed: timedelta) -> EscalationLevel:
        if elapsed >= self.severe_after:
            return EscalationLevel.SEVERE
        if elapsed >= self.warning_after:
            return EscalationLevel.WARNING
        if elapsed >= self.attention_after:
            return EscalationLevel.ATTENTION
        return EscalationLevel.NONE


@dataclass(frozen=True)
class RouteSegment:
    segment_id: str
    origin: str
    destination: str
    mountain_road: bool = False
    medical_point_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class TravelRoute:
    route_id: str
    region: str
    segments: tuple[RouteSegment, ...]


@dataclass(frozen=True)
class PopulationGroup:
    """重点人群，如需要夜间保暖的老人。"""

    group_id: str
    region: str
    label: str
    night_warmth_required: bool = False


@dataclass(frozen=True)
class MedicalPoint:
    point_id: str
    region: str
    name: str
    temporary: bool = False
    available: bool = True


@dataclass(frozen=True)
class NoticeAuthorization:
    """通知授权：谁被允许批准哪个地区的提醒。"""

    auth_id: str
    region: str
    approver: str


@dataclass(frozen=True)
class AdvicePart:
    """建议中的一条可追溯内容，basis 记录全部依据。"""

    part_id: str
    kind: PartKind
    content: str
    basis: tuple[str, ...]  # 如 forecast:F1@v2、segment:S3、point:P1、population:G1


@dataclass(frozen=True)
class Receipt:
    traveler_id: str
    device_id: str
    confirmed_at: datetime


@dataclass
class Notice:
    """一条面向旅客的建议，送达前可随预报修订换基。"""

    notice_id: str
    region: str
    traveler_id: str
    route_id: str
    status: NoticeStatus
    forecast_id: str
    forecast_version: int
    parts: list[AdvicePart]
    population_group_ids: tuple[str, ...] = ()
    approved_by: str | None = None
    sent_at: datetime | None = None
    receipts: list[Receipt] = field(default_factory=list)


@dataclass(frozen=True)
class EscalationRecord:
    region: str
    hazard: HazardType
    level: EscalationLevel
    at: datetime
    trigger: str  # CLOCK 时间推进 / RECEIPT 回执触发 / EVENT 事件合并
    notice_id: str | None = None


@dataclass(frozen=True)
class NoticeReview:
    """复盘视图：依据、批准人、接收人与回执完成情况。"""

    notice_id: str
    region: str
    status: NoticeStatus
    forecast_ref: str
    approved_by: str | None
    sent_at: datetime | None
    recipients: tuple[str, ...]
    receipts: tuple[Receipt, ...]
    receipt_complete: bool
    parts: tuple[AdvicePart, ...]
