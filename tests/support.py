"""测试公用夹具：一个雁荡山区假日值班场景。"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from holiday_health_weather import HolidayHealthService, VirtualClock  # noqa: E402
from holiday_health_weather.clock import CST  # noqa: E402

AREA = "雁荡山区"
OTHER_AREA = "楠溪湖区"


def cst(y: int, m: int, d: int, h: int = 0, minute: int = 0) -> datetime:
    return datetime(y, m, d, h, minute, tzinfo=CST)


def rain(value: float, at: datetime) -> dict:
    return {"hazard": "rain", "value": value, "unit": "mm/h", "at": at.isoformat()}


def low_temp(value: float, at: datetime) -> dict:
    return {"hazard": "low_temp", "value": value, "unit": "℃", "at": at.isoformat()}


def fog(value: float, at: datetime) -> dict:
    return {"hazard": "fog", "value": value, "unit": "m", "at": at.isoformat()}


def build_service(start: datetime | None = None) -> HolidayHealthService:
    """登记好授权、阈值、医疗点、双区段山区线路与一位老年旅客。"""

    svc = HolidayHealthService(VirtualClock(start or cst(2026, 10, 2, 8)))
    svc.grant_authorization("auth-yd", AREA, ["sms", "app"])
    svc.register_threshold(
        "th-yd",
        AREA,
        {
            "rain": [
                {"level": 1, "cutoff": 10, "name": "注意"},
                {"level": 2, "cutoff": 25, "name": "警示"},
                {"level": 3, "cutoff": 50, "name": "暴雨警告"},
            ],
            "low_temp": [
                {"level": 1, "cutoff": 8, "name": "注意"},
                {"level": 2, "cutoff": 5, "name": "警示"},
                {"level": 3, "cutoff": 0, "name": "低温警告"},
            ],
            "fog": [
                {"level": 1, "cutoff": 1000, "name": "注意"},
                {"level": 2, "cutoff": 500, "name": "警示"},
                {"level": 3, "cutoff": 200, "name": "浓雾警告"},
            ],
        },
    )
    svc.register_medical_point("mp-lingfeng", AREA, "灵峰临时点", backup_point_id="mp-town")
    svc.register_medical_point("mp-town", AREA, "雁荡镇卫生院")
    svc.register_medical_point("mp-lake", OTHER_AREA, "楠溪湖救护站")
    svc.register_route(
        "r-lingfeng",
        AREA,
        "灵峰线",
        [
            {
                "segment_id": "seg-center-lingfeng",
                "from": "游客中心",
                "to": "灵峰",
                "mountain": True,
                "medical_point_ids": ["mp-lingfeng"],
            },
            {
                "segment_id": "seg-lingfeng-dalongqiu",
                "from": "灵峰",
                "to": "大龙湫",
                "mountain": True,
                "scenic_area_id": "scenic-dalongqiu",
                "medical_point_ids": ["mp-town"],
            },
        ],
    )
    svc.register_route(
        "r-bypass",
        AREA,
        "白溪绕行线",
        [
            {
                "segment_id": "seg-baixi",
                "from": "白溪村口",
                "to": "大龙湫后门",
                "mountain": False,
                "medical_point_ids": ["mp-town"],
            }
        ],
    )
    svc.register_person("p-wang", "王老伯", ["elderly", "hypertension"], ["phone-a", "watch-b"])
    return svc


def register_trip(svc: HolidayHealthService, trip_id: str = "trip-wang") -> str:
    svc.register_trip(
        trip_id,
        "p-wang",
        "r-lingfeng",
        "大龙湫",
        ["seg-center-lingfeng", "seg-lingfeng-dalongqiu"],
    )
    return trip_id
