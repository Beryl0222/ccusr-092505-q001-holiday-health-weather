"""事件重放目录。

:class:`Catalog` 是从只追加事件流折叠出的读模型：登记信息、预报各版本、
通知状态与回执全部通过重放得到，任何时候删掉重建结果一致，复盘时也可以
在历史某一时刻重新折叠出当时的目录。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from .event import Event
from .model import (
    AdvisoryItem,
    Authorization,
    Basis,
    Delivery,
    Evidence,
    FocusPerson,
    ForecastVersion,
    Measure,
    MedicalPoint,
    NoticeSnapshot,
    NoticeState,
    Receipt,
    Route,
    ScenicClosure,
    Segment,
    Threshold,
    Trip,
    Cutoff,
)
from .thresholds import DEFAULT_RULES


def parse_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return parsed


class Catalog:
    def __init__(self) -> None:
        self.forecasts: dict[str, list[ForecastVersion]] = {}
        self.thresholds: dict[str, Threshold] = {}
        self.routes: dict[str, Route] = {}
        self.segment_status: dict[tuple[str, str], dict[str, Any]] = {}
        self.persons: dict[str, FocusPerson] = {}
        self.medical_points: dict[str, MedicalPoint] = {}
        self.authorizations: dict[str, Authorization] = {}
        self.closures: dict[str, ScenicClosure] = {}
        self.trips: dict[str, Trip] = {}
        self.trip_versions: dict[str, list[Trip]] = {}
        self.notices: dict[str, NoticeState] = {}
        # 通知与行程的索引
        self.notices_by_trip: dict[str, list[str]] = {}
        # 重放时按规则忽略的事件（如修订到达已送达通知），保留审计线索
        self.ignored: list[dict[str, str]] = []

    # ------------------------------------------------------------------ #
    # 折叠
    # ------------------------------------------------------------------ #

    @classmethod
    def replay(cls, events: list[Event]) -> Catalog:
        catalog = cls()
        for event in events:
            catalog.apply(event)
        return catalog

    def apply(self, event: Event) -> None:
        p = event.payload
        handler = {
            "FORECAST_ISSUED": self._apply_forecast,
            "FORECAST_REVISED": self._apply_forecast,
            "THRESHOLD_REGISTERED": self._apply_threshold,
            "THRESHOLD_UPDATED": self._apply_threshold,
            "ROUTE_REGISTERED": self._apply_route,
            "ROUTE_REROUTED": self._apply_reroute,
            "PERSON_REGISTERED": self._apply_person,
            "MEDICAL_POINT_REGISTERED": self._apply_point,
            "MEDICAL_POINT_WITHDRAWN": self._apply_point,
            "SCENIC_CLOSURE_NOTIFIED": self._apply_closure,
            "SCENIC_REOPENED": self._apply_closure,
            "AUTHORIZATION_GRANTED": self._apply_auth,
            "AUTHORIZATION_REVOKED": self._apply_auth,
            "TRIP_REGISTERED": self._apply_trip,
            "TRIP_DESTINATION_CHANGED": self._apply_trip,
            "ADVISORY_DRAFTED": self._apply_notice_draft,
            "ADVISORY_RECALCULATED": self._apply_notice_recalc,
            "ADVISORY_APPROVED": self._apply_approval,
            "ADVISORY_SENT": self._apply_sent,
            "RISK_ESCALATED": self._apply_escalation,
            "RECEIPT_ACKNOWLEDGED": self._apply_receipt,
            "RECEIPT_COMPLETED": self._apply_receipt,
        }.get(event.event_type)
        if handler:
            handler(event, p)

    # ---- 预报与阈值 ----------------------------------------------------

    def _apply_forecast(self, event: Event, p: Mapping[str, Any]) -> None:
        measures = tuple(
            Measure(
                hazard=m["hazard"],
                value=float(m["value"]),
                unit=m.get("unit", ""),
                at=parse_dt(m["at"]) if m.get("at") else None,
            )
            for m in p["measures"]
        )
        version = ForecastVersion(
            forecast_id=event.aggregate_id,
            version=event.version,
            area=p["area"],
            valid_from=parse_dt(p["valid_from"]),
            valid_to=parse_dt(p["valid_to"]),
            measures=measures,
            event_id=event.event_id,
            issued_at=event.occurred_at,
        )
        self.forecasts.setdefault(event.aggregate_id, []).append(version)

    def _apply_threshold(self, event: Event, p: Mapping[str, Any]) -> None:
        rules = {
            hazard: tuple(
                Cutoff(level=int(row["level"]), cutoff=float(row["cutoff"]), name=row.get("name", ""))
                for row in rows
            )
            for hazard, rows in p["rules"].items()
        }
        self.thresholds[p["area"]] = Threshold(
            threshold_id=event.aggregate_id,
            area=p["area"],
            rules=rules,
            version=event.version,
            event_id=event.event_id,
        )

    # ---- 线路与行程 ----------------------------------------------------

    def _apply_route(self, event: Event, p: Mapping[str, Any]) -> None:
        segments = tuple(
            Segment(
                segment_id=s["segment_id"],
                area=p.get("area", s.get("area", "")),
                from_name=s.get("from", ""),
                to_name=s.get("to", ""),
                mountain=bool(s.get("mountain", False)),
                scenic_area_id=s.get("scenic_area_id"),
                medical_point_ids=tuple(s.get("medical_point_ids", ())),
            )
            for s in p["segments"]
        )
        self.routes[event.aggregate_id] = Route(
            route_id=event.aggregate_id,
            area=p.get("area", ""),
            name=p.get("name", event.aggregate_id),
            segments=segments,
            version=event.version,
            event_id=event.event_id,
        )

    def _apply_reroute(self, event: Event, p: Mapping[str, Any]) -> None:
        key = (event.aggregate_id, p["segment_id"])
        self.segment_status[key] = {
            "closed": bool(p.get("closed", True)),
            "alternative": p.get("alternative"),
            "event_id": event.event_id,
            "at": event.occurred_at,
        }

    def _apply_person(self, event: Event, p: Mapping[str, Any]) -> None:
        self.persons[event.aggregate_id] = FocusPerson(
            person_id=event.aggregate_id,
            name=p.get("name", ""),
            groups=tuple(p.get("groups", ())),
            devices=tuple(p.get("devices", ())),
            version=event.version,
            event_id=event.event_id,
        )

    def _apply_point(self, event: Event, p: Mapping[str, Any]) -> None:
        previous = self.medical_points.get(event.aggregate_id)
        self.medical_points[event.aggregate_id] = MedicalPoint(
            point_id=event.aggregate_id,
            area=p.get("area", previous.area if previous else ""),
            name=p.get("name", previous.name if previous else event.aggregate_id),
            backup_point_id=p.get("backup_point_id", previous.backup_point_id if previous else None),
            active=event.event_type == "MEDICAL_POINT_REGISTERED",
            version=event.version,
            event_id=event.event_id,
        )

    def _apply_closure(self, event: Event, p: Mapping[str, Any]) -> None:
        active = event.event_type == "SCENIC_CLOSURE_NOTIFIED"
        previous = self.closures.get(event.aggregate_id)
        self.closures[event.aggregate_id] = ScenicClosure(
            scenic_area_id=event.aggregate_id,
            name=p.get("name", previous.name if previous else event.aggregate_id),
            area=p.get("area", previous.area if previous else ""),
            closed_from=parse_dt(p["closed_from"]) if p.get("closed_from") else (
                previous.closed_from if previous else event.occurred_at
            ),
            closed_to=parse_dt(p["closed_to"]) if p.get("closed_to") else (
                previous.closed_to if previous else event.occurred_at
            ),
            reason=p.get("reason", previous.reason if previous else ""),
            active=active,
            version=event.version,
            event_id=event.event_id,
        )

    def _apply_auth(self, event: Event, p: Mapping[str, Any]) -> None:
        self.authorizations[p["area"]] = Authorization(
            auth_id=event.aggregate_id,
            area=p["area"],
            channels=tuple(p.get("channels", ())),
            granted=event.event_type == "AUTHORIZATION_GRANTED",
            version=event.version,
            event_id=event.event_id,
        )

    def _apply_trip(self, event: Event, p: Mapping[str, Any]) -> None:
        trip = Trip(
            trip_id=event.aggregate_id,
            person_id=p["person_id"],
            route_id=p["route_id"],
            destination=p["destination"],
            segment_ids=tuple(p["segment_ids"]),
            version=event.version,
            event_id=event.event_id,
        )
        self.trips[event.aggregate_id] = trip
        self.trip_versions.setdefault(event.aggregate_id, []).append(trip)

    # ---- 建议通知 ------------------------------------------------------

    def _apply_notice_draft(self, event: Event, p: Mapping[str, Any]) -> None:
        items = _items_from_payload(p)
        basis = _basis_from_payload(p["basis"]) if p.get("basis") else None
        state = NoticeState(
            notice_id=event.aggregate_id,
            trip_id=p["trip_id"],
            segment_id=p["segment_id"],
            area=p["area"],
            planned_at=parse_dt(p["planned_at"]),
            status="drafted",
            level=int(p.get("level", 0)),
            items=items,
            basis=basis,
        )
        state.history.append(
            NoticeSnapshot(
                version=1,
                at=event.occurred_at,
                level=state.level,
                items=items,
                basis=basis,
                reason=p.get("reason", "draft"),
                event_id=event.event_id,
            )
        )
        self.notices[event.aggregate_id] = state
        self.notices_by_trip.setdefault(p["trip_id"], []).append(event.aggregate_id)

    def _apply_notice_recalc(self, event: Event, p: Mapping[str, Any]) -> None:
        state = self.notices[event.aggregate_id]
        if p.get("action") == "supersede":
            state.status = "superseded"
            state.history.append(
                NoticeSnapshot(
                    version=len(state.history) + 1,
                    at=event.occurred_at,
                    level=state.level,
                    items=(),
                    basis=state.basis,  # 留档时仍保留最后依据
                    reason=p.get("reason", "superseded"),
                    event_id=event.event_id,
                )
            )
            return
        if state.status == "sent":
            # 已送达建议冻结：修订不得改写，事件仍留在流中可审计。
            self.ignored.append(
                {
                    "event_id": event.event_id,
                    "notice_id": event.aggregate_id,
                    "code": "sent_notice_frozen",
                }
            )
            return
        items = _items_from_payload(p)
        basis = _basis_from_payload(p["basis"]) if p.get("basis") else state.basis
        snapshot_no = len(state.history) + 1
        was_approved = state.status == "approved"
        state.level = int(p.get("level", state.level))
        state.items = items
        state.basis = basis
        if was_approved:
            # 内容在送达前被改写：原批准留档，通知退回待批准，需重新授权确认。
            state.status = "drafted"
        state.history.append(
            NoticeSnapshot(
                version=snapshot_no,
                at=event.occurred_at,
                level=state.level,
                items=items,
                basis=basis,
                reason=p.get("reason", "recalculated") + ("；重算后需重新批准" if was_approved else ""),
                event_id=event.event_id,
            )
        )

    def _apply_approval(self, event: Event, p: Mapping[str, Any]) -> None:
        state = self.notices[event.aggregate_id]
        state.status = "approved"
        state.approvals.append(
            {
                "by": p.get("by", ""),
                "at": parse_dt(p["at"]) if p.get("at") else event.occurred_at,
                "event_id": event.event_id,
                "basis": state.basis.to_dict() if state.basis else None,
            }
        )

    def _apply_sent(self, event: Event, p: Mapping[str, Any]) -> None:
        state = self.notices[event.aggregate_id]
        state.status = "sent"
        state.delivery = Delivery(
            at=parse_dt(p["at"]) if p.get("at") else event.occurred_at,
            event_id=event.event_id,
            recipients=tuple(dict(recipient) for recipient in p.get("recipients", ())),
        )

    def _apply_escalation(self, event: Event, p: Mapping[str, Any]) -> None:
        state = self.notices[event.aggregate_id]
        if state.status == "sent":
            self.ignored.append(
                {
                    "event_id": event.event_id,
                    "notice_id": event.aggregate_id,
                    "code": "sent_notice_frozen",
                }
            )
            return
        old_level = state.level
        was_approved = state.status == "approved"
        state.level = int(p["to_level"])
        if p.get("items"):
            state.items = _items_from_payload(p)
        if p.get("basis"):
            state.basis = _basis_from_payload(p["basis"])
        if was_approved:
            # 升级改变了送达内容，原批准保留在审批记录中，需重新批准。
            state.status = "drafted"
        reason = p.get("reason", f"escalated L{old_level}→L{state.level}")
        if was_approved:
            reason += "；升级后需重新批准"
        state.history.append(
            NoticeSnapshot(
                version=len(state.history) + 1,
                at=event.occurred_at,
                level=state.level,
                items=state.items,
                basis=state.basis,
                reason=reason,
                event_id=event.event_id,
            )
        )

    def _apply_receipt(self, event: Event, p: Mapping[str, Any]) -> None:
        state = self.notices[event.aggregate_id]
        stage = "acknowledged" if event.event_type == "RECEIPT_ACKNOWLEDGED" else "completed"
        key = (p["person_id"], stage)
        if key in state.receipts:
            # 同一旅客换设备重复确认：以首次为准，幂等忽略。
            self.ignored.append(
                {
                    "event_id": event.event_id,
                    "notice_id": event.aggregate_id,
                    "code": "duplicate_receipt",
                }
            )
            return
        state.receipts[key] = Receipt(
            person_id=p["person_id"],
            stage=stage,
            devices=tuple(p.get("devices", [p.get("device", "")])),
            at=parse_dt(p["at"]) if p.get("at") else event.occurred_at,
            event_id=event.event_id,
        )

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    def effective_forecast(self, area: str, at: datetime) -> ForecastVersion | None:
        """选取地区内在 ``at`` 时刻生效的最新预报版本。"""

        candidates: list[ForecastVersion] = []
        for versions in self.forecasts.values():
            for version in versions:
                if version.area == area and version.valid_from <= at < version.valid_to:
                    candidates.append(version)
        if not candidates:
            return None
        return max(candidates, key=lambda v: (v.version, v.issued_at))

    def rules_for(self, area: str) -> dict[str, tuple[Cutoff, ...]]:
        threshold = self.thresholds.get(area)
        if threshold is None:
            return {hazard: tuple(rows) for hazard, rows in DEFAULT_RULES.items()}
        return dict(threshold.rules)

    def route_segment(self, route_id: str, segment_id: str) -> Segment | None:
        route = self.routes.get(route_id)
        if route is None:
            return None
        for segment in route.segments:
            if segment.segment_id == segment_id:
                return segment
        return None

    def active_points_for(self, segment: Segment) -> list[str]:
        """区段沿线当前可用的医疗点；下线的点替换为备案点。"""

        result: list[str] = []
        for point_id in segment.medical_point_ids:
            point = self.medical_points.get(point_id)
            if point is not None and not point.active:
                if point.backup_point_id:
                    result.append(point.backup_point_id)
                continue
            result.append(point_id)
        return result

    def notices_for_trip(self, trip_id: str) -> list[NoticeState]:
        return [self.notices[nid] for nid in self.notices_by_trip.get(trip_id, [])]


def _items_from_payload(p: Mapping[str, Any]) -> tuple[AdvisoryItem, ...]:
    return tuple(
        AdvisoryItem(
            category=item["category"],
            text=item["text"],
            evidence=tuple(
                Evidence(
                    kind=e["kind"],
                    aggregate_id=e["aggregate_id"],
                    version=int(e.get("version", 1)),
                    event_id=e.get("event_id", ""),
                    note=e.get("note", ""),
                )
                for e in item.get("evidence", ())
            ),
        )
        for item in p.get("items", ())
    )


def _basis_from_payload(p: Mapping[str, Any]) -> Basis:
    return Basis(
        forecast_id=p["forecast_id"],
        forecast_version=int(p["forecast_version"]),
        event_id=p.get("event_id", ""),
    )
