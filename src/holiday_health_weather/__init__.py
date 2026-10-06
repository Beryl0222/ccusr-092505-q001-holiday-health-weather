"""假日气象健康联动台：领域契约与联动服务。"""

from .contracts import ContractIssue, validate_event
from .clock import Clock, ManualClock, SystemClock
from .models import (
    AdvicePart,
    EscalationLevel,
    EscalationRecord,
    ForecastVersion,
    HazardObservation,
    HazardType,
    MedicalPoint,
    Notice,
    NoticeAuthorization,
    NoticeReview,
    NoticeStatus,
    PartKind,
    PopulationGroup,
    Receipt,
    RiskThreshold,
    RouteSegment,
    TravelRoute,
)
from .service import LinkageError, LinkageService, NotFoundError, StateError

__all__ = [
    "AdvicePart",
    "Clock",
    "ContractIssue",
    "EscalationLevel",
    "EscalationRecord",
    "ForecastVersion",
    "HazardObservation",
    "HazardType",
    "LinkageError",
    "LinkageService",
    "ManualClock",
    "MedicalPoint",
    "NotFoundError",
    "Notice",
    "NoticeAuthorization",
    "NoticeReview",
    "NoticeStatus",
    "PartKind",
    "PopulationGroup",
    "Receipt",
    "RiskThreshold",
    "RouteSegment",
    "StateError",
    "SystemClock",
    "TravelRoute",
    "validate_event",
]
