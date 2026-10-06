"""假日气象健康联动服务。

在事件登记簿之上提供值班操作：

* 登记：预报版本、风险阈值、出行线路、重点人群、医疗点、通知授权、景区封闭；
* 建议：按行程区段起草三类可追溯建议，审批与送达；
* 联动：预报修订只重算未送达通知；时钟推进触发分级升级；医疗点下线、
  目的地变更只重算受影响区段；
* 回执：同一旅客跨设备确认幂等；
* 同步：断网恢复后重复上报与晚到消息安全合并；
* 复盘：:meth:`trace` 还原一条提醒的完整来历。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from .builder import AdvisoryBuilder
from .catalog import Catalog, parse_dt
from .clock import Clock
from .event import Event
from .model import LEVEL_NAMES
from .store import EventConflictError, EventStore, business_fingerprint


class AuthorizationError(RuntimeError):
    """缺少该地区的通知授权。"""


class WorkflowError(RuntimeError):
    """通知状态不允许该操作（如未审批即送达、已送达又修改）。"""


class HolidayHealthService:
    def __init__(self, clock: Clock, store: EventStore | None = None) -> None:
        self.clock = clock
        self.store = store or EventStore()
        self._seq = 0
        # 修订/升级到达时已送达通知被跳过的留痕，复盘"为何没被改写"可查。
        self.audit: list[dict[str, Any]] = []

    def _skip_frozen(self, notice_id: str, reason: str, causation_id: str | None) -> None:
        self.audit.append(
            {
                "notice_id": notice_id,
                "code": "sent_notice_frozen",
                "reason": reason,
                "causation_id": causation_id,
                "at": self.clock.now().isoformat(),
            }
        )

    # ================================================================== #
    # 基础工具
    # ================================================================== #

    @property
    def catalog(self) -> Catalog:
        return Catalog.replay(self.store.all_events())

    def _next_event_id(self, event_type: str, aggregate_id: str) -> str:
        self._seq += 1
        return f"{self._seq:06d}-{event_type}-{aggregate_id}"

    def _emit(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
        *,
        summary: str,
        occurred_at: datetime | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
        event_id: str | None = None,
        fingerprint: str | None = None,
        expected_version: int | None = None,
    ) -> tuple[Event, bool]:
        version = self.store.current_version(aggregate_id) + 1
        event = Event(
            event_id=event_id or self._next_event_id(event_type, aggregate_id),
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            occurred_at=occurred_at or self.clock.now(),
            version=version,
            summary=summary,
            payload=payload,
            causation_id=causation_id,
            correlation_id=correlation_id,
        )
        if fingerprint is None:
            fingerprint = business_fingerprint(event_type, aggregate_id, payload)
        return self.store.append(event, expected_version=expected_version, fingerprint=fingerprint)

    # ================================================================== #
    # 登记：预报、阈值、线路、人群、医疗点、授权、景区
    # ================================================================== #

    def register_forecast(
        self,
        forecast_id: str,
        area: str,
        valid_from: datetime | str,
        valid_to: datetime | str,
        measures: Sequence[dict[str, Any]],
        *,
        summary: str = "",
    ) -> list[Event]:
        """登记一版预报；同一 forecast_id 再次登记即视为修订。

        修订会触发同地区 *尚未送达* 通知的重算，已送达通知保持冻结。
        """

        revision = self.store.current_version(forecast_id) > 0
        payload = {
            "area": area,
            "valid_from": _dt_str(valid_from),
            "valid_to": _dt_str(valid_to),
            "measures": list(measures),
        }
        event, _ = self._emit(
            "FORECAST_REVISED" if revision else "FORECAST_ISSUED",
            "forecast",
            forecast_id,
            payload,
            summary=summary or (f"{area} 预报修订 v{self.store.current_version(forecast_id) + 1}"
                                if revision else f"{area} 预报发布"),
        )
        emitted = [event]
        if revision:
            emitted.extend(self.recalculate_area(area, reason="forecast_revised", causation_id=event.event_id))
        return emitted

    def register_threshold(self, threshold_id: str, area: str, rules: dict[str, list[dict[str, Any]]]) -> Event:
        update = self.store.current_version(threshold_id) > 0
        event, _ = self._emit(
            "THRESHOLD_UPDATED" if update else "THRESHOLD_REGISTERED",
            "risk_threshold",
            threshold_id,
            {"area": area, "rules": rules},
            summary=f"{area} 风险阈值登记",
        )
        return event

    def register_route(self, route_id: str, area: str, name: str, segments: Sequence[dict[str, Any]]) -> Event:
        event, _ = self._emit(
            "ROUTE_REGISTERED",
            "travel_segment",
            route_id,
            {"area": area, "name": name, "segments": list(segments)},
            summary=f"{area} 线路 {name} 登记",
        )
        return event

    def reroute_segment(
        self, route_id: str, segment_id: str, *, alternative: str | None = None, closed: bool = True
    ) -> list[Event]:
        event, _ = self._emit(
            "ROUTE_REROUTED",
            "travel_segment",
            route_id,
            {"segment_id": segment_id, "closed": closed, "alternative": alternative},
            summary=f"区段 {segment_id} 封闭/绕行",
        )
        # 只影响经过该区段的行程
        affected = [event]
        for trip in self.catalog.trips.values():
            if trip.route_id == route_id and segment_id in trip.segment_ids:
                affected.extend(
                    self._recalculate_trip(
                        trip.trip_id,
                        affected_segments={segment_id},
                        reason="segment_rerouted",
                        causation_id=event.event_id,
                    )
                )
        return affected

    def register_person(self, person_id: str, name: str, groups: Sequence[str], devices: Sequence[str]) -> Event:
        event, _ = self._emit(
            "PERSON_REGISTERED",
            "focus_person",
            person_id,
            {"name": name, "groups": list(groups), "devices": list(devices)},
            summary=f"重点旅客 {name} 登记",
        )
        return event

    def register_medical_point(
        self, point_id: str, area: str, name: str, *, backup_point_id: str | None = None
    ) -> Event:
        event, _ = self._emit(
            "MEDICAL_POINT_REGISTERED",
            "medical_point",
            point_id,
            {"area": area, "name": name, "backup_point_id": backup_point_id},
            summary=f"医疗点 {name} 登记",
        )
        return event

    def withdraw_medical_point(self, point_id: str, *, reason: str = "临时医疗点下线") -> list[Event]:
        """医疗点下线：只重算引用该点的区段，其他区域服务不中断。"""

        event, _ = self._emit(
            "MEDICAL_POINT_WITHDRAWN",
            "medical_point",
            point_id,
            {"reason": reason},
            summary=reason,
        )
        affected = [event]
        catalog = self.catalog
        for trip in catalog.trips.values():
            segment_ids: set[str] = set()
            for segment in self._trip_segments(catalog, trip):
                if point_id in segment.medical_point_ids:
                    segment_ids.add(segment.segment_id)
            if segment_ids:
                affected.extend(
                    self._recalculate_trip(
                        trip.trip_id,
                        affected_segments=segment_ids,
                        reason="medical_point_withdrawn",
                        causation_id=event.event_id,
                    )
                )
        return affected

    def notify_scenic_closure(
        self,
        scenic_area_id: str,
        name: str,
        area: str,
        closed_from: datetime | str,
        closed_to: datetime | str,
        reason: str,
    ) -> list[Event]:
        event, _ = self._emit(
            "SCENIC_CLOSURE_NOTIFIED",
            "scenic_closure",
            scenic_area_id,
            {
                "name": name,
                "area": area,
                "closed_from": _dt_str(closed_from),
                "closed_to": _dt_str(closed_to),
                "reason": reason,
            },
            summary=f"景区 {name} 封闭通知",
        )
        affected = [event]
        catalog = self.catalog
        for trip in catalog.trips.values():
            segment_ids = {
                s.segment_id
                for s in self._trip_segments(catalog, trip)
                if s.scenic_area_id == scenic_area_id
            }
            if segment_ids:
                affected.extend(
                    self._recalculate_trip(
                        trip.trip_id,
                        affected_segments=segment_ids,
                        reason="scenic_closure",
                        causation_id=event.event_id,
                    )
                )
        return affected

    def grant_authorization(self, auth_id: str, area: str, channels: Sequence[str]) -> Event:
        event, _ = self._emit(
            "AUTHORIZATION_GRANTED",
            "authorization",
            auth_id,
            {"area": area, "channels": list(channels)},
            summary=f"{area} 通知授权开通",
        )
        return event

    def revoke_authorization(self, auth_id: str, area: str) -> Event:
        event, _ = self._emit(
            "AUTHORIZATION_REVOKED",
            "authorization",
            auth_id,
            {"area": area, "channels": []},
            summary=f"{area} 通知授权撤销",
        )
        return event

    # ================================================================== #
    # 行程与目的地变更
    # ================================================================== #

    def register_trip(
        self,
        trip_id: str,
        person_id: str,
        route_id: str,
        destination: str,
        segment_ids: Sequence[str],
    ) -> Event:
        event, _ = self._emit(
            "TRIP_REGISTERED",
            "trip",
            trip_id,
            {
                "person_id": person_id,
                "route_id": route_id,
                "destination": destination,
                "segment_ids": list(segment_ids),
            },
            summary=f"行程登记，目的地 {destination}",
            correlation_id=trip_id,
        )
        return event

    def change_destination(self, trip_id: str, new_destination: str, new_segment_ids: Sequence[str]) -> list[Event]:
        """目的地变更：只重算受影响区段，未变化区段保留既有研判与审批。"""

        catalog = self.catalog
        old = catalog.trips[trip_id]
        old_set = set(old.segment_ids)
        new_set = set(new_segment_ids)
        kept = old_set & new_set
        changed = new_set ^ old_set

        event, _ = self._emit(
            "TRIP_DESTINATION_CHANGED",
            "trip",
            trip_id,
            {
                "person_id": old.person_id,
                "route_id": old.route_id,
                "destination": new_destination,
                "segment_ids": list(new_segment_ids),
                "affected_segments": sorted(changed),
            },
            summary=f"目的地变更为 {new_destination}",
            correlation_id=trip_id,
        )
        affected_events = [event]
        affected_events.extend(
            self._recalculate_trip(
                trip_id,
                affected_segments=changed,
                removed_segments=old_set - new_set,
                reason="destination_changed",
                causation_id=event.event_id,
            )
        )
        return affected_events

    # ================================================================== #
    # 建议：起草、重算、审批、送达
    # ================================================================== #

    def draft_for_trip(self, trip_id: str, *, at: datetime | None = None) -> list[Event]:
        """为行程中尚无有效通知的区段起草建议；已取消(superseded)通知不阻挡新草稿。"""

        catalog = self.catalog
        trip = catalog.trips[trip_id]
        events: list[Event] = []
        for segment in self._trip_segments(catalog, trip):
            if self._active_notice(catalog, trip_id, segment.segment_id) is not None:
                continue
            events.append(self._draft_one(trip, segment, at=at))
        return events

    @staticmethod
    def _active_notice(catalog: Catalog, trip_id: str, segment_id: str):  # type: ignore[no-untyped-def]
        for state in catalog.notices_for_trip(trip_id):
            if state.segment_id == segment_id and state.status != "superseded":
                return state
        return None

    def recalculate_area(self, area: str, *, reason: str, causation_id: str | None = None) -> list[Event]:
        """预报修订后：重算该地区所有尚未送达的通知。"""

        events: list[Event] = []
        catalog = self.catalog
        for trip in catalog.trips.values():
            segment_ids = {s.segment_id for s in self._trip_segments(catalog, trip) if s.area == area}
            if segment_ids:
                events.extend(
                    self._recalculate_trip(
                        trip.trip_id,
                        affected_segments=segment_ids,
                        reason=reason,
                        causation_id=causation_id,
                    )
                )
        return events

    def approve_notice(self, notice_id: str, by: str) -> Event:
        catalog = self.catalog
        state = catalog.notices[notice_id]
        if state.status in ("sent", "superseded"):
            raise WorkflowError(f"通知 {notice_id} 状态为 {state.status}，不能审批")
        if state.status == "approved":
            raise WorkflowError(f"通知 {notice_id} 已批准，送达前如需变更请等待重算后重新批准")
        auth = catalog.authorizations.get(state.area)
        if auth is None or not auth.granted:
            raise AuthorizationError(f"地区 {state.area} 尚无有效通知授权，不能批准送达")
        event, _ = self._emit(
            "ADVISORY_APPROVED",
            "health_notice",
            notice_id,
            {"by": by, "at": self.clock.now().isoformat(), "trip_id": state.trip_id},
            summary=f"{by} 批准 {notice_id}",
            correlation_id=state.trip_id,
        )
        return event

    def send_notice(self, notice_id: str, *, recipients: Sequence[dict[str, str]] | None = None) -> Event:
        catalog = self.catalog
        state = catalog.notices[notice_id]
        if state.status == "superseded":
            raise WorkflowError(f"通知 {notice_id} 已随区段取消，不再送达")
        if state.status != "approved":
            raise WorkflowError(f"通知 {notice_id} 尚未审批，不能送达")
        if recipients is None:
            trip = catalog.trips[state.trip_id]
            person = catalog.persons.get(trip.person_id)
            devices = person.devices if person else ()
            recipients = [
                {"person_id": trip.person_id, "name": person.name if person else trip.person_id, "device": device}
                for device in devices
            ]
        event, _ = self._emit(
            "ADVISORY_SENT",
            "health_notice",
            notice_id,
            {"at": self.clock.now().isoformat(), "trip_id": state.trip_id, "recipients": list(recipients)},
            summary=f"建议 {notice_id} 已送达",
            correlation_id=state.trip_id,
        )
        return event

    def acknowledge(self, notice_id: str, person_id: str, device: str) -> tuple[Event, bool]:
        """旅客确认（可能来自任意设备）。返回 (事件, 是否重复被合并)。"""

        state = self.catalog.notices[notice_id]
        payload = {
            "person_id": person_id,
            "device": device,
            "devices": [device],
            "at": self.clock.now().isoformat(),
        }
        # 指纹跨设备一致：同一旅客同一阶段只确认一次，重复确认不触发升级。
        fingerprint = f"receipt:{notice_id}:{person_id}:acknowledged"
        event, duplicate = self._emit(
            "RECEIPT_ACKNOWLEDGED",
            "health_notice",
            notice_id,
            payload,
            summary=f"{person_id} 经 {device} 确认收到",
            correlation_id=state.trip_id,
            fingerprint=fingerprint,
        )
        return event, duplicate

    def complete_receipt(self, notice_id: str, person_id: str, device: str) -> tuple[Event, bool]:
        state = self.catalog.notices[notice_id]
        payload = {
            "person_id": person_id,
            "device": device,
            "devices": [device],
            "at": self.clock.now().isoformat(),
        }
        fingerprint = f"receipt:{notice_id}:{person_id}:completed"
        event, duplicate = self._emit(
            "RECEIPT_COMPLETED",
            "health_notice",
            notice_id,
            payload,
            summary=f"{person_id} 完成回执",
            correlation_id=state.trip_id,
            fingerprint=fingerprint,
        )
        return event, duplicate

    # ================================================================== #
    # 时间推进：分级升级
    # ================================================================== #

    def advance_and_evaluate(self, *, hours: float = 0, minutes: float = 0) -> list[Event]:
        """推进虚拟时钟，并对所有尚未送达的通知重新研判。

        级别升高时产生 RISK_ESCALATED；级别不变或下降不产生事件，
        已送达通知永不参与。
        """

        from datetime import timedelta

        if hours or minutes:
            self.clock.advance(timedelta(hours=hours, minutes=minutes))
        return self.evaluate_due()

    def evaluate_due(self) -> list[Event]:
        catalog = self.catalog
        builder = AdvisoryBuilder(catalog)
        events: list[Event] = []
        for state in list(catalog.notices.values()):
            if state.status in ("sent", "superseded"):
                if state.status == "sent":
                    self._skip_frozen(state.notice_id, "时钟推进评估：已送达通知不再升级", None)
                continue
            trip = catalog.trips.get(state.trip_id)
            if trip is None:
                continue
            segment = next(
                (candidate for candidate in self._trip_segments(catalog, trip)
                 if candidate.segment_id == state.segment_id),
                None,
            )
            if segment is None:
                continue
            person = catalog.persons.get(trip.person_id)
            draft = builder.evaluate(segment, self.clock.now(), person)
            if draft.level > state.level:
                event, _ = self._emit(
                    "RISK_ESCALATED",
                    "health_notice",
                    state.notice_id,
                    {
                        "trip_id": state.trip_id,
                        "segment_id": state.segment_id,
                        "from_level": state.level,
                        "to_level": draft.level,
                        "level": draft.level,
                        "items": [item.to_dict() for item in draft.items],
                        "basis": draft.basis.to_dict() if draft.basis else None,
                        "reason": f"升级至{LEVEL_NAMES[draft.level]}",
                    },
                    summary=f"{state.segment_id} 风险升级 L{state.level}→L{draft.level}",
                    causation_id=draft.basis.event_id if draft.basis else None,
                    correlation_id=state.trip_id,
                )
                events.append(event)
        return events

    # ================================================================== #
    # 断网恢复：安全合并
    # ================================================================== #

    def sync(self, events: Iterable[Event]) -> list[tuple[Event, bool]]:
        """合并离线期间积压的事件。

        先按发生时间排序（晚到消息落位），再逐条登记；相同 event_id、
        相同业务指纹的重复上报，以及同一旅客跨设备的重复确认，都只生效一次。
        """

        ordered = sorted(events, key=lambda e: (e.occurred_at, e.event_id))
        results: list[tuple[Event, bool]] = []
        revised_forecasts: list[str] = []
        for event in ordered:
            fingerprint = self._sync_fingerprint(event)
            try:
                stored, duplicate = self.store.append(event, fingerprint=fingerprint)
            except EventConflictError:
                # 补传流中的版本冲突不中断整批同步，可从 store.rejections 查看留痕。
                continue
            results.append((stored, duplicate))
            if event.event_type == "FORECAST_REVISED" and not duplicate:
                revised_forecasts.append(event.aggregate_id)
        # 同步进来的新预报修订同样触发未送达通知重算
        for forecast_id in set(revised_forecasts):
            catalog = self.catalog
            forecast = catalog.forecasts.get(forecast_id, [None])[-1]
            if forecast is not None:
                self.recalculate_area(forecast.area, reason="forecast_revised_sync")
        return results

    @staticmethod
    def _sync_fingerprint(event: Event) -> str:
        if event.event_type in ("RECEIPT_ACKNOWLEDGED", "RECEIPT_COMPLETED"):
            stage = "acknowledged" if event.event_type == "RECEIPT_ACKNOWLEDGED" else "completed"
            return f"receipt:{event.aggregate_id}:{event.payload['person_id']}:{stage}"
        return business_fingerprint(event.event_type, event.aggregate_id, dict(event.payload))

    # ================================================================== #
    # 复盘
    # ================================================================== #

    def trace(self, notice_id: str) -> dict[str, Any]:
        catalog = self.catalog
        state = catalog.notices[notice_id]
        trip = catalog.trips.get(state.trip_id)

        versions_used: dict[str, int] = {}
        for snapshot in state.history:
            versions_used[snapshot.basis.forecast_id] = snapshot.basis.forecast_version

        trace_events = [
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "at": event.occurred_at.isoformat(),
                "causation_id": event.causation_id,
                "summary": event.summary,
            }
            for event in self.store.events_for(notice_id)
        ]

        return {
            "notice_id": notice_id,
            "trip_id": state.trip_id,
            "person_id": trip.person_id if trip else None,
            "segment_id": state.segment_id,
            "area": state.area,
            "status": state.status,
            "level": state.level,
            "forecast_versions_used": versions_used,
            "current_basis": state.basis.to_dict() if state.basis else None,
            "history": [
                {
                    "snapshot": snapshot.version,
                    "at": snapshot.at.isoformat(),
                    "level": snapshot.level,
                    "reason": snapshot.reason,
                    "event_id": snapshot.event_id,
                    "basis": snapshot.basis.to_dict() if snapshot.basis else None,
                    "items": [item.to_dict() for item in snapshot.items],
                }
                for snapshot in state.history
            ],
            "approved_by": [
                {"by": row["by"], "at": row["at"].isoformat() if isinstance(row["at"], datetime) else row["at"],
                 "event_id": row["event_id"], "basis": row["basis"]}
                for row in state.approvals
            ],
            "delivered": (
                {
                    "at": state.delivery.at.isoformat(),
                    "event_id": state.delivery.event_id,
                    "recipients": list(state.delivery.recipients),
                }
                if state.delivery
                else None
            ),
            "receipts": [
                {
                    "person_id": receipt.person_id,
                    "stage": receipt.stage,
                    "devices": list(receipt.devices),
                    "at": receipt.at.isoformat(),
                    "event_id": receipt.event_id,
                }
                for receipt in state.receipts.values()
            ],
            "events": trace_events,
        }

    # ================================================================== #
    # 内部
    # ================================================================== #

    def _trip_segments(self, catalog: Catalog, trip):  # type: ignore[no-untyped-def]
        """按行程的区段顺序返回区段；主线路缺少的区段（改走绕行线等）到其他线路补齐。"""

        order = {segment_id: index for index, segment_id in enumerate(trip.segment_ids)}
        found: dict[str, object] = {}
        route = catalog.routes.get(trip.route_id)
        if route is not None:
            for segment in route.segments:
                if segment.segment_id in order:
                    found[segment.segment_id] = segment
        for other in catalog.routes.values():
            for segment in other.segments:
                if segment.segment_id in order and segment.segment_id not in found:
                    found[segment.segment_id] = segment
        return sorted(found.values(), key=lambda segment: order[segment.segment_id])  # type: ignore[arg-type]

    def _draft_one(self, trip, segment, *, at: datetime | None = None):  # type: ignore[no-untyped-def]
        catalog = self.catalog
        person = catalog.persons.get(trip.person_id)
        moment = at or self.clock.now()
        draft = AdvisoryBuilder(catalog).evaluate(segment, moment, person)
        # 同一区段可能经历取消后重新加入行程，重起草使用带序号的新聚合标识，
        # 旧通知（含当时依据与送达记录）完整保留。
        prior = [
            state
            for state in catalog.notices_for_trip(trip.trip_id)
            if state.segment_id == segment.segment_id
        ]
        notice_id = (
            f"notice-{trip.trip_id}-{segment.segment_id}"
            if not prior
            else f"notice-{trip.trip_id}-{segment.segment_id}-r{len(prior) + 1}"
        )
        event, _ = self._emit(
            "ADVISORY_DRAFTED",
            "health_notice",
            notice_id,
            {
                "trip_id": trip.trip_id,
                "segment_id": segment.segment_id,
                "area": segment.area,
                "planned_at": moment.isoformat(),
                "level": draft.level,
                "items": [item.to_dict() for item in draft.items],
                "basis": draft.basis.to_dict() if draft.basis else None,
                "reason": "draft",
            },
            summary=f"为区段 {segment.segment_id} 起草建议（L{draft.level}）",
            correlation_id=trip.trip_id,
        )
        return event

    def _recalculate_trip(
        self,
        trip_id: str,
        *,
        affected_segments: set[str],
        removed_segments: set[str] | None = None,
        reason: str,
        causation_id: str | None,
    ) -> list[Event]:
        catalog = self.catalog
        trip = catalog.trips[trip_id]
        person = catalog.persons.get(trip.person_id)
        builder = AdvisoryBuilder(catalog)
        events: list[Event] = []
        removed_segments = removed_segments or set()

        for state in catalog.notices_for_trip(trip_id):
            if state.segment_id not in affected_segments:
                continue
            if state.status == "sent":
                # 已送达建议保留当时依据：修订不得改写，但留下跳过留痕。
                self._skip_frozen(
                    state.notice_id,
                    f"{reason} 到达时通知已送达，保留当时依据",
                    causation_id,
                )
                continue
            if state.segment_id in removed_segments:
                event, _ = self._emit(
                    "ADVISORY_RECALCULATED",
                    "health_notice",
                    state.notice_id,
                    {
                        "trip_id": trip_id,
                        "segment_id": state.segment_id,
                        "action": "supersede",
                        "reason": reason,
                        "items": [],
                        "basis": state.basis.to_dict() if state.basis else None,
                        "level": state.level,
                    },
                    summary=f"区段 {state.segment_id} 随目的地变更取消",
                    causation_id=causation_id,
                    correlation_id=trip_id,
                )
                events.append(event)
                continue

            segment = next(
                (candidate for candidate in self._trip_segments(catalog, trip)
                 if candidate.segment_id == state.segment_id),
                None,
            )
            if segment is None:
                continue
            draft = builder.evaluate(segment, self.clock.now(), person)
            event, _ = self._emit(
                "ADVISORY_RECALCULATED",
                "health_notice",
                state.notice_id,
                {
                    "trip_id": trip_id,
                    "segment_id": state.segment_id,
                    "reason": reason,
                    "level": draft.level,
                    "items": [item.to_dict() for item in draft.items],
                    "basis": draft.basis.to_dict() if draft.basis else None,
                },
                summary=f"按 {reason} 重算 {state.segment_id}",
                causation_id=causation_id,
                correlation_id=trip_id,
            )
            events.append(event)

        # 新增进行程的区段补起草（旧通知若已 superseded，不阻挡新草稿）
        latest = self.catalog
        existing = {
            state.segment_id
            for state in latest.notices_for_trip(trip_id)
            if state.status != "superseded"
        }
        for segment in self._trip_segments(latest, trip):
            if segment.segment_id in affected_segments and segment.segment_id not in existing and segment.segment_id not in removed_segments:
                events.append(self._draft_one(trip, segment))
        return events


def _dt_str(value: datetime | str) -> str:
    if isinstance(value, str):
        return parse_dt(value).isoformat()
    return value.isoformat()
