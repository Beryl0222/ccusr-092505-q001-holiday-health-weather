"""假日气象健康联动服务：登记、建议生成、分级升级与安全合并。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any, Mapping

from .clock import Clock, SystemClock
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
    level_rank,
)


class LinkageError(Exception):
    """业务规则冲突时抛出。"""


class NotFoundError(LinkageError):
    """登记对象不存在。"""


class StateError(LinkageError):
    """当前状态不允许该操作。"""


def _forecast_ref(forecast: ForecastVersion) -> str:
    return f"forecast:{forecast.forecast_id}@v{forecast.version}"


def _segment_ref(part: AdvicePart) -> str | None:
    for ref in part.basis:
        if ref.startswith("segment:"):
            return ref[len("segment:") :]
    return None


class LinkageService:
    """联动台核心服务，所有时间读取都经过注入的时钟。"""

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._forecasts: dict[str, list[ForecastVersion]] = {}
        self._thresholds: dict[tuple[str, HazardType], RiskThreshold] = {}
        self._routes: dict[str, TravelRoute] = {}
        self._populations: dict[str, PopulationGroup] = {}
        self._points: dict[str, MedicalPoint] = {}
        self._auths: dict[str, NoticeAuthorization] = {}
        self._notices: dict[str, Notice] = {}
        self._escalations: list[EscalationRecord] = []
        self._events: list[dict[str, Any]] = []
        self._seen_event_ids: set[str] = set()
        self._aggregate_versions: dict[str, int] = {}
        self._seq = 0
        self._notice_seq = 0

    # ------------------------------------------------------------------
    # 登记
    # ------------------------------------------------------------------
    def register_forecast(self, forecast: ForecastVersion) -> ForecastVersion:
        versions = self._forecasts.setdefault(forecast.region, [])
        if any(
            item.forecast_id == forecast.forecast_id and item.version == forecast.version
            for item in versions
        ):
            raise StateError("同一预报版本不得重复登记")
        versions.append(forecast)
        versions.sort(key=lambda item: (item.issued_at, item.version))
        self._emit(
            "FORECAST_ISSUED",
            "forecast",
            forecast.forecast_id,
            f"登记{forecast.region}第{forecast.version}版预报",
            {"forecast": self._forecast_payload(forecast)},
        )
        # 预报修订只影响尚未生效的通知
        self._rebase_pending(forecast.region)
        return forecast

    def register_threshold(self, threshold: RiskThreshold) -> RiskThreshold:
        self._thresholds[(threshold.region, threshold.hazard)] = threshold
        return threshold

    def register_route(self, route: TravelRoute) -> TravelRoute:
        self._routes[route.route_id] = route
        return route

    def register_population(self, group: PopulationGroup) -> PopulationGroup:
        self._populations[group.group_id] = group
        return group

    def register_medical_point(self, point: MedicalPoint) -> MedicalPoint:
        self._points[point.point_id] = point
        return point

    def register_authorization(self, auth: NoticeAuthorization) -> NoticeAuthorization:
        self._auths[auth.auth_id] = auth
        return auth

    # ------------------------------------------------------------------
    # 建议生成与生命周期
    # ------------------------------------------------------------------
    def compose_notice(
        self,
        traveler_id: str,
        route_id: str,
        population_group_ids: tuple[str, ...] = (),
    ) -> Notice:
        """按当前生效预报生成一条建议，内容拆成三类可追溯部分。"""
        route = self._routes.get(route_id)
        if route is None:
            raise NotFoundError(f"线路不存在：{route_id}")
        forecast = self._effective_forecast(route.region, self._clock.now())
        groups = tuple(self._require_group(group_id, route.region) for group_id in population_group_ids)
        self._notice_seq += 1
        notice_id = f"N-{self._notice_seq:06d}"
        notice = Notice(
            notice_id=notice_id,
            region=route.region,
            traveler_id=traveler_id,
            route_id=route_id,
            status=NoticeStatus.DRAFT,
            forecast_id=forecast.forecast_id,
            forecast_version=forecast.version,
            parts=self._build_parts(notice_id, route, forecast, groups),
            population_group_ids=tuple(group.group_id for group in groups),
        )
        self._notices[notice_id] = notice
        return notice

    def approve_notice(self, notice_id: str, auth_id: str) -> Notice:
        notice = self._require_notice(notice_id)
        auth = self._auths.get(auth_id)
        if auth is None:
            raise NotFoundError(f"授权不存在：{auth_id}")
        if auth.region != notice.region:
            raise StateError("授权区域与通知区域不匹配")
        if notice.status != NoticeStatus.DRAFT:
            raise StateError("只有草拟状态的通知可以批准")
        notice.status = NoticeStatus.APPROVED
        notice.approved_by = auth.approver
        self._emit(
            "NOTICE_APPROVED",
            "health_notice",
            notice_id,
            f"{auth.approver}批准了通知{notice_id}",
            {"notice_id": notice_id, "approved_by": auth.approver},
        )
        return notice

    def send_notice(self, notice_id: str) -> Notice:
        notice = self._require_notice(notice_id)
        if notice.status != NoticeStatus.APPROVED:
            raise StateError("通知必须先批准再送达")
        notice.status = NoticeStatus.SENT
        notice.sent_at = self._clock.now()
        self._emit(
            "NOTICE_SENT",
            "health_notice",
            notice_id,
            f"通知{notice_id}已送达旅客{notice.traveler_id}",
            {"notice_id": notice_id, "sent_at": notice.sent_at.isoformat()},
        )
        return notice

    def confirm_receipt(self, notice_id: str, traveler_id: str, device_id: str) -> tuple[Receipt, bool]:
        """旅客确认收到。同一旅客跨设备重复确认幂等，不重复触发升级。

        返回（回执， 本次是否新触发了升级）。
        """
        notice = self._require_notice(notice_id)
        if notice.status != NoticeStatus.SENT:
            raise StateError("通知尚未送达，不能确认回执")
        for receipt in notice.receipts:
            if receipt.traveler_id == traveler_id:
                return receipt, False
        receipt = Receipt(traveler_id=traveler_id, device_id=device_id, confirmed_at=self._clock.now())
        notice.receipts.append(receipt)
        return receipt, self._trigger_escalation_for(notice)

    def change_destination(self, notice_id: str, new_route_id: str) -> Notice:
        """目的地变更：只重算受影响区段，未受影响的内容与依据保持原样。"""
        notice = self._require_notice(notice_id)
        if notice.status == NoticeStatus.SENT:
            raise StateError("已送达的建议保留当时依据，不支持变更目的地")
        new_route = self._routes.get(new_route_id)
        if new_route is None:
            raise NotFoundError(f"线路不存在：{new_route_id}")
        if new_route.region != notice.region:
            raise StateError("暂不支持跨区域变更目的地")
        old_route = self._routes[notice.route_id]
        old_segments = {seg.segment_id: seg for seg in old_route.segments}
        new_segments = {seg.segment_id: seg for seg in new_route.segments}
        affected = {
            segment_id
            for segment_id, seg in new_segments.items()
            if old_segments.get(segment_id) != seg
        } | {segment_id for segment_id in old_segments if segment_id not in new_segments}
        forecast = self._notice_forecast(notice)
        rebuilt = {
            seg.segment_id: self._traffic_part(notice.notice_id, seg, forecast)
            for seg in new_route.segments
            if seg.segment_id in affected
        }
        kept = [
            part
            for part in notice.parts
            if not (part.kind == PartKind.TRAFFIC_HANDLING and _segment_ref(part) in affected)
        ]
        order = {seg.segment_id: index for index, seg in enumerate(new_route.segments)}
        traffic = sorted(
            [part for part in kept if part.kind == PartKind.TRAFFIC_HANDLING]
            + [rebuilt[sid] for sid in affected if sid in rebuilt],
            key=lambda part: order.get(_segment_ref(part) or "", len(order)),
        )
        notice.parts = [part for part in kept if part.kind != PartKind.TRAFFIC_HANDLING] + traffic
        notice.route_id = new_route_id
        return notice

    # ------------------------------------------------------------------
    # 分级升级（按注入时钟推进）
    # ------------------------------------------------------------------
    def escalation_level(
        self, region: str, hazard: HazardType, at: datetime | None = None
    ) -> EscalationLevel:
        moment = at or self._clock.now()
        observation = self._active_observation(region, hazard, moment)
        if observation is None:
            return EscalationLevel.NONE
        threshold = self._thresholds.get((region, hazard))
        if threshold is None:
            return EscalationLevel.ATTENTION
        return threshold.level_for(moment - observation.started_at)

    def refresh_escalations(self) -> list[EscalationRecord]:
        """按当前注入时间扫描所有活动风险，只记录档位上升。"""
        now = self._clock.now()
        new_records: list[EscalationRecord] = []
        for region, versions in self._forecasts.items():
            forecast = self._effective_forecast(region, now, required=False)
            if forecast is None:
                continue
            for observation in forecast.hazards:
                level = self.escalation_level(region, observation.hazard, now)
                if level == EscalationLevel.NONE:
                    continue
                current = self._recorded_level(region, observation.hazard)
                if level_rank(level) <= level_rank(current):
                    continue
                record = EscalationRecord(
                    region=region,
                    hazard=observation.hazard,
                    level=level,
                    at=now,
                    trigger="CLOCK",
                )
                self._escalations.append(record)
                new_records.append(record)
                self._emit(
                    "RISK_ESCALATED",
                    "forecast",
                    forecast.forecast_id,
                    f"{region}{observation.hazard.value}升级为{level.value}",
                    {
                        "region": region,
                        "hazard": observation.hazard.value,
                        "level": level.value,
                        "at": now.isoformat(),
                    },
                )
        return new_records

    # ------------------------------------------------------------------
    # 医疗点下线：只替换相关线路，不影响其他区域
    # ------------------------------------------------------------------
    def withdraw_medical_point(self, point_id: str) -> MedicalPoint:
        point = self._points.get(point_id)
        if point is None:
            raise NotFoundError(f"医疗点不存在：{point_id}")
        if not point.available:
            return point
        updated = self._withdraw_point(point_id)
        self._emit(
            "POINT_WITHDRAWN",
            "medical_point",
            point_id,
            f"医疗点{point_id}下线，相关线路已替换",
            {"point_id": point_id},
        )
        return updated

    def _withdraw_point(self, point_id: str) -> MedicalPoint:
        point = self._points[point_id]
        updated = replace(point, available=False)
        self._points[point_id] = updated
        for route_id, route in list(self._routes.items()):
            if route.region != point.region:
                continue
            if not any(point_id in seg.medical_point_ids for seg in route.segments):
                continue
            self._routes[route_id] = self._repoint_route(route, point_id)
        for notice in self._notices.values():
            if notice.region != point.region or notice.status == NoticeStatus.SENT:
                continue
            self._repoint_notice(notice, point_id)
        return updated

    # ------------------------------------------------------------------
    # 事件合并：网络恢复后的重复上报与晚到消息
    # ------------------------------------------------------------------
    def ingest(self, event: Mapping[str, Any]) -> str:
        """合并一条外来事件，返回 APPLIED / DUPLICATE / STALE / REJECTED。"""
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id.strip():
            return "REJECTED"
        if event_id in self._seen_event_ids:
            return "DUPLICATE"
        aggregate_id = event.get("aggregate_id")
        version = event.get("version")
        if (
            isinstance(aggregate_id, str)
            and isinstance(version, int)
            and not isinstance(version, bool)
            and version <= self._aggregate_versions.get(aggregate_id, 0)
        ):
            self._seen_event_ids.add(event_id)
            return "STALE"
        if not self._apply_event(event):
            return "REJECTED"
        self._seen_event_ids.add(event_id)
        if isinstance(aggregate_id, str) and isinstance(version, int) and not isinstance(version, bool):
            self._aggregate_versions[aggregate_id] = max(
                version, self._aggregate_versions.get(aggregate_id, 0)
            )
        self._events.append(dict(event))
        return "APPLIED"

    @property
    def event_log(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._events)

    @property
    def escalations(self) -> tuple[EscalationRecord, ...]:
        return tuple(self._escalations)

    # ------------------------------------------------------------------
    # 复盘
    # ------------------------------------------------------------------
    def review_notice(self, notice_id: str) -> NoticeReview:
        """复盘一条提醒：哪版预报产生、谁批准、谁收到、回执是否完成。"""
        notice = self._require_notice(notice_id)
        recipients = (notice.traveler_id,) if notice.status == NoticeStatus.SENT else ()
        receipt_complete = any(
            receipt.traveler_id == notice.traveler_id for receipt in notice.receipts
        )
        return NoticeReview(
            notice_id=notice.notice_id,
            region=notice.region,
            status=notice.status,
            forecast_ref=f"forecast:{notice.forecast_id}@v{notice.forecast_version}",
            approved_by=notice.approved_by,
            sent_at=notice.sent_at,
            recipients=recipients,
            receipts=tuple(notice.receipts),
            receipt_complete=receipt_complete,
            parts=tuple(notice.parts),
        )

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _require_notice(self, notice_id: str) -> Notice:
        notice = self._notices.get(notice_id)
        if notice is None:
            raise NotFoundError(f"通知不存在：{notice_id}")
        return notice

    def _require_group(self, group_id: str, region: str) -> PopulationGroup:
        group = self._populations.get(group_id)
        if group is None:
            raise NotFoundError(f"重点人群不存在：{group_id}")
        if group.region != region:
            raise StateError("重点人群与线路不在同一地区")
        return group

    def _effective_forecast(
        self, region: str, moment: datetime, required: bool = True
    ) -> ForecastVersion | None:
        versions = self._forecasts.get(region, [])
        covering = [item for item in versions if item.covers(moment)]
        if covering:
            return covering[-1]
        if required:
            raise NotFoundError(f"{region}当前没有生效中的预报")
        return None

    def _notice_forecast(self, notice: Notice) -> ForecastVersion:
        for item in self._forecasts.get(notice.region, []):
            if item.forecast_id == notice.forecast_id and item.version == notice.forecast_version:
                return item
        return self._effective_forecast(notice.region, self._clock.now())

    def _build_parts(
        self,
        notice_id: str,
        route: TravelRoute,
        forecast: ForecastVersion,
        groups: tuple[PopulationGroup, ...],
    ) -> list[AdvicePart]:
        parts = [self._weather_part(notice_id, route, forecast)]
        parts.extend(self._health_part(notice_id, group, forecast) for group in groups)
        parts.extend(self._traffic_part(notice_id, seg, forecast) for seg in route.segments)
        return parts

    def _weather_part(
        self, notice_id: str, route: TravelRoute, forecast: ForecastVersion
    ) -> AdvicePart:
        if forecast.hazards:
            summary = "；".join(
                f"{obs.hazard.value}（{obs.detail + '，' if obs.detail else ''}"
                f"自{obs.started_at.isoformat()}起）"
                for obs in forecast.hazards
            )
        else:
            summary = "无显著气象风险"
        return AdvicePart(
            part_id=f"{notice_id}-W1",
            kind=PartKind.WEATHER_FACT,
            content=f"{route.region}气象事实：{summary}",
            basis=(_forecast_ref(forecast),),
        )

    def _health_part(
        self, notice_id: str, group: PopulationGroup, forecast: ForecastVersion
    ) -> AdvicePart:
        measures = []
        if group.night_warmth_required:
            measures.append("夜间注意保暖")
        if any(obs.hazard == HazardType.LOW_TEMPERATURE for obs in forecast.hazards):
            measures.append("关注低温变化，减少夜间外出")
        content = f"{group.label}健康措施：" + ("；".join(measures) if measures else "按常规防护")
        return AdvicePart(
            part_id=f"{notice_id}-H{group.group_id}",
            kind=PartKind.HEALTH_MEASURE,
            content=content,
            basis=(f"population:{group.group_id}", _forecast_ref(forecast)),
        )

    def _traffic_part(
        self, notice_id: str, segment: RouteSegment, forecast: ForecastVersion
    ) -> AdvicePart:
        points = [self._points[pid] for pid in segment.medical_point_ids if pid in self._points]
        available = [point for point in points if point.available]
        road = "山区道路，谨慎慢行" if segment.mountain_road else "常规道路"
        if available:
            medical = "、".join(point.name for point in available)
        else:
            medical = "暂无可用医疗点"
        basis = [f"segment:{segment.segment_id}"]
        basis.extend(f"point:{point.point_id}" for point in available)
        basis.append(_forecast_ref(forecast))
        return AdvicePart(
            part_id=f"{notice_id}-T{segment.segment_id}",
            kind=PartKind.TRAFFIC_HANDLING,
            content=(
                f"{segment.origin}至{segment.destination}：{road}；"
                f"沿途医疗点：{medical}"
            ),
            basis=tuple(basis),
        )

    def _rebase_pending(self, region: str) -> None:
        forecast = self._effective_forecast(region, self._clock.now(), required=False)
        if forecast is None:
            return
        for notice in self._notices.values():
            if notice.region != region or notice.status == NoticeStatus.SENT:
                continue
            if (notice.forecast_id, notice.forecast_version) == (
                forecast.forecast_id,
                forecast.version,
            ):
                continue
            route = self._routes[notice.route_id]
            groups = tuple(self._populations[gid] for gid in notice.population_group_ids)
            notice.parts = self._build_parts(notice.notice_id, route, forecast, groups)
            notice.forecast_id = forecast.forecast_id
            notice.forecast_version = forecast.version

    def _active_observation(
        self, region: str, hazard: HazardType, moment: datetime
    ) -> HazardObservation | None:
        forecast = self._effective_forecast(region, moment, required=False)
        if forecast is None:
            return None
        for observation in forecast.hazards:
            if observation.hazard == hazard and observation.started_at <= moment:
                return observation
        return None

    def _recorded_level(self, region: str, hazard: HazardType) -> EscalationLevel:
        level = EscalationLevel.NONE
        for record in self._escalations:
            if record.region == region and record.hazard == hazard:
                if level_rank(record.level) > level_rank(level):
                    level = record.level
        return level

    def _trigger_escalation_for(self, notice: Notice) -> bool:
        now = self._clock.now()
        forecast = self._effective_forecast(notice.region, now, required=False)
        if forecast is None:
            return False
        triggered = False
        for observation in forecast.hazards:
            level = self.escalation_level(notice.region, observation.hazard, now)
            if level == EscalationLevel.NONE:
                continue
            duplicate = any(
                record.region == notice.region
                and record.hazard == observation.hazard
                and record.level == level
                and record.notice_id == notice.notice_id
                for record in self._escalations
            )
            if duplicate:
                continue
            self._escalations.append(
                EscalationRecord(
                    region=notice.region,
                    hazard=observation.hazard,
                    level=level,
                    at=now,
                    trigger="RECEIPT",
                    notice_id=notice.notice_id,
                )
            )
            triggered = True
        return triggered

    def _repoint_route(self, route: TravelRoute, withdrawn_id: str) -> TravelRoute:
        replacement = self._replacement_point(route.region, withdrawn_id)
        segments = []
        for seg in route.segments:
            if withdrawn_id not in seg.medical_point_ids:
                segments.append(seg)
                continue
            ids = tuple(pid for pid in seg.medical_point_ids if pid != withdrawn_id)
            if replacement is not None:
                ids = ids + (replacement.point_id,)
            segments.append(replace(seg, medical_point_ids=ids))
        return replace(route, segments=tuple(segments))

    def _repoint_notice(self, notice: Notice, withdrawn_id: str) -> None:
        route = self._routes[notice.route_id]
        forecast = self._notice_forecast(notice)
        rebuilt = {seg.segment_id: self._traffic_part(notice.notice_id, seg, forecast) for seg in route.segments}
        notice.parts = [
            rebuilt.get(_segment_ref(part) or "", part)
            if part.kind == PartKind.TRAFFIC_HANDLING
            else part
            for part in notice.parts
        ]

    def _replacement_point(self, region: str, exclude_id: str) -> MedicalPoint | None:
        candidates = [
            point
            for point in self._points.values()
            if point.region == region and point.available and point.point_id != exclude_id
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda point: (point.temporary, point.point_id))
        return candidates[0]

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    def _emit(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        summary: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self._seq += 1
        version = self._aggregate_versions.get(aggregate_id, 0) + 1
        self._aggregate_versions[aggregate_id] = version
        event = {
            "event_id": f"evt-{self._seq:06d}",
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": self._clock.now().isoformat(),
            "version": version,
            "summary": summary,
            "payload": payload,
        }
        self._events.append(event)
        self._seen_event_ids.add(event["event_id"])
        return event

    @staticmethod
    def _forecast_payload(forecast: ForecastVersion) -> dict[str, Any]:
        return {
            "forecast_id": forecast.forecast_id,
            "region": forecast.region,
            "version": forecast.version,
            "issued_at": forecast.issued_at.isoformat(),
            "effective_from": forecast.effective_from.isoformat(),
            "effective_to": forecast.effective_to.isoformat(),
            "hazards": [
                {
                    "hazard": obs.hazard.value,
                    "started_at": obs.started_at.isoformat(),
                    "detail": obs.detail,
                }
                for obs in forecast.hazards
            ],
        }

    def _apply_event(self, event: Mapping[str, Any]) -> bool:
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            return False
        event_type = event.get("event_type")
        if event_type == "FORECAST_ISSUED":
            data = payload.get("forecast")
            if not isinstance(data, Mapping):
                return False
            forecast = ForecastVersion(
                forecast_id=str(data["forecast_id"]),
                region=str(data["region"]),
                version=int(data["version"]),
                issued_at=datetime.fromisoformat(str(data["issued_at"])),
                effective_from=datetime.fromisoformat(str(data["effective_from"])),
                effective_to=datetime.fromisoformat(str(data["effective_to"])),
                hazards=tuple(
                    HazardObservation(
                        hazard=HazardType(str(item["hazard"])),
                        started_at=datetime.fromisoformat(str(item["started_at"])),
                        detail=str(item.get("detail", "")),
                    )
                    for item in data.get("hazards", [])
                ),
            )
            versions = self._forecasts.setdefault(forecast.region, [])
            if any(
                item.forecast_id == forecast.forecast_id and item.version == forecast.version
                for item in versions
            ):
                return True  # 内容一致的重复上报，安全合并
            versions.append(forecast)
            versions.sort(key=lambda item: (item.issued_at, item.version))
            self._rebase_pending(forecast.region)
            return True
        if event_type == "NOTICE_APPROVED":
            notice = self._notices.get(str(payload.get("notice_id")))
            if notice is None or notice.status != NoticeStatus.DRAFT:
                return notice is not None
            notice.status = NoticeStatus.APPROVED
            notice.approved_by = str(payload.get("approved_by"))
            return True
        if event_type == "NOTICE_SENT":
            notice = self._notices.get(str(payload.get("notice_id")))
            if notice is None or notice.status == NoticeStatus.SENT:
                return notice is not None
            notice.status = NoticeStatus.SENT
            notice.sent_at = datetime.fromisoformat(str(payload["sent_at"]))
            return True
        if event_type == "RISK_ESCALATED":
            record = EscalationRecord(
                region=str(payload["region"]),
                hazard=HazardType(str(payload["hazard"])),
                level=EscalationLevel(str(payload["level"])),
                at=datetime.fromisoformat(str(payload["at"])),
                trigger="EVENT",
            )
            if any(
                item.region == record.region
                and item.hazard == record.hazard
                and item.level == record.level
                for item in self._escalations
            ):
                return True
            self._escalations.append(record)
            return True
        if event_type == "POINT_WITHDRAWN":
            point = self._points.get(str(payload.get("point_id")))
            if point is None:
                return False
            if point.available:
                self._withdraw_point(point.point_id)
            return True
        return False
