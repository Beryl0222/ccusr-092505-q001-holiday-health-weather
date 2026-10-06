"""假日气象健康联动台：事件溯源的登记、建议、升级、回执与复盘服务。"""

from .builder import AdvisoryBuilder, AdvisoryDraft
from .catalog import Catalog
from .clock import Clock, SystemClock, VirtualClock
from .contracts import ContractIssue, validate_event
from .event import AGGREGATE_TYPES, EVENT_TYPES, Event
from .model import (
    HEALTH_MEASURE,
    ITEM_CATEGORIES,
    LEVEL_NAMES,
    TRAFFIC_DISPOSAL,
    WEATHER_FACT,
    AdvisoryItem,
    Basis,
    Evidence,
)
from .service import (
    AuthorizationError,
    HolidayHealthService,
    WorkflowError,
)
from .store import EventConflictError, EventStore, business_fingerprint
from .thresholds import DEFAULT_RULES, level_for

__all__ = [
    "AdvisoryBuilder",
    "AdvisoryDraft",
    "AuthorizationError",
    "AGGREGATE_TYPES",
    "Basis",
    "Catalog",
    "ContractIssue",
    "DEFAULT_RULES",
    "EVENT_TYPES",
    "Event",
    "EventConflictError",
    "EventStore",
    "HEALTH_MEASURE",
    "ITEM_CATEGORIES",
    "LEVEL_NAMES",
    "HolidayHealthService",
    "SystemClock",
    "TRAFFIC_DISPOSAL",
    "VirtualClock",
    "WEATHER_FACT",
    "WorkflowError",
    "AdvisoryItem",
    "Evidence",
    "business_fingerprint",
    "level_for",
    "validate_event",
]
