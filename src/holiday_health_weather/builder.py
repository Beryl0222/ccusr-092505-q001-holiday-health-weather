"""建议构建：把一次研判拆成气象事实、健康措施、交通处置三类内容。

每条内容都携带 :class:`Evidence` 证据链，值班复盘时可以顺着证据回到
具体的预报版本、阈值版本、线路版本或医疗点事件。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .catalog import Catalog
from .model import (
    AdvisoryItem,
    Basis,
    Evidence,
    ForecastVersion,
    HAZARD_META,
    HEALTH_MEASURE,
    LEVEL_NAMES,
    Measure,
    FocusPerson,
    Segment,
    TRAFFIC_DISPOSAL,
    WEATHER_FACT,
)
from .thresholds import level_for, describe


@dataclass(frozen=True, slots=True)
class AdvisoryDraft:
    segment_id: str
    area: str
    level: int
    items: tuple[AdvisoryItem, ...]
    basis: Basis | None


def measure_at(forecast: ForecastVersion, hazard: str, at: datetime) -> Measure | None:
    """取预报中该灾种在 ``at`` 所在小时的量值。

    逐时预报值代表该小时的预计等级，按小时精确匹配；窗口均值（``at`` 为空）
    作为兜底。跨小时不沿用旧值，避免把昨天/上一小时的等级带进当前建议。
    """

    window: Measure | None = None
    for measure in forecast.measures:
        if measure.hazard != hazard:
            continue
        if measure.at is None:
            window = measure
        elif (measure.at.year, measure.at.month, measure.at.day, measure.at.hour) == (
            at.year,
            at.month,
            at.day,
            at.hour,
        ):
            return measure
    return window


class AdvisoryBuilder:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    def evaluate(self, segment: Segment, at: datetime, person: FocusPerson | None = None) -> AdvisoryDraft:
        area = segment.area
        rules = self.catalog.rules_for(area)
        forecast = self.catalog.effective_forecast(area, at)

        items: list[AdvisoryItem] = []
        level = 0
        basis: Basis | None = None

        if forecast is not None:
            basis = Basis(
                forecast_id=forecast.forecast_id,
                forecast_version=forecast.version,
                event_id=forecast.event_id,
            )
            for hazard in HAZARD_META:
                measure = measure_at(forecast, hazard, at)
                if measure is None:
                    # 当前小时无此灾种：检查预报窗口内稍后是否将出现，
                    # 提前给出健康防护（如老人夜间保暖），但不抬高当前等级、
                    # 不提前产生交通处置。
                    items.extend(self._upcoming_health(forecast, hazard, at, person))
                    continue
                hazard_level = level_for(hazard, measure.value, rules)
                level = max(level, hazard_level)
                items.append(self._weather_fact(hazard, measure, hazard_level, rules, forecast))
                items.extend(self._health_measures(hazard, hazard_level, forecast, person))
                items.extend(self._traffic_measures(hazard, hazard_level, segment, forecast, at))

        # 景区封闭与气象量值无关，属于独立的交通处置依据
        items.extend(self._closure_items(segment, forecast))
        # 线路调度封闭/绕行
        items.extend(self._reroute_items(segment))
        # 医疗点可用性变化同样只产生交通/就医处置
        items.extend(self._medical_items(segment))

        return AdvisoryDraft(
            segment_id=segment.segment_id,
            area=area,
            level=level,
            items=tuple(items),
            basis=basis,
        )

    # ------------------------------------------------------------------ #

    def _weather_fact(
        self,
        hazard: str,
        measure: Measure,
        hazard_level: int,
        rules: dict,
        forecast: ForecastVersion,
    ) -> AdvisoryItem:
        meta = HAZARD_META[hazard]
        grade = describe(hazard, measure.value, rules) or "未达阈值"
        when = measure.at.strftime("%H:%M") if measure.at else "预报窗口内"
        text = (
            f"【气象事实】{when} {meta['label']} {measure.value:g}{measure.unit or meta['unit']}，"
            f"分级：{LEVEL_NAMES[hazard_level]}（{grade}）。"
        )
        return AdvisoryItem(
            category=WEATHER_FACT,
            text=text,
            evidence=(
                Evidence(
                    kind="forecast",
                    aggregate_id=forecast.forecast_id,
                    version=forecast.version,
                    event_id=forecast.event_id,
                    note=f"{hazard}={measure.value:g}{measure.unit} @ {measure.at or 'window'}",
                ),
            ),
        )

    def _upcoming_health(
        self,
        forecast: ForecastVersion,
        hazard: str,
        at: datetime,
        person: FocusPerson | None,
    ) -> list[AdvisoryItem]:
        """对窗口内稍后出现的风险提前给健康措施（不抬级别、不提前交通处置）。"""

        upcoming = sorted(
            (m for m in forecast.measures if m.hazard == hazard and m.at is not None and m.at > at),
            key=lambda m: m.at,
        )
        if not upcoming:
            return []
        rules = self.catalog.rules_for(forecast.area)
        # 以窗口内即将到来的最高等级提前防护
        measure = max(upcoming, key=lambda m: level_for(hazard, m.value, rules))
        upcoming_level = level_for(hazard, measure.value, rules)
        if upcoming_level < 1:
            return []
        items = self._health_measures(hazard, upcoming_level, forecast, person)
        when = measure.at.strftime("%H:%M")
        return [
            AdvisoryItem(
                HEALTH_MEASURE,
                item.text.replace("【健康措施】", f"【健康措施·{when}前后预报】", 1),
                tuple(
                    Evidence(
                        kind=e.kind,
                        aggregate_id=e.aggregate_id,
                        version=e.version,
                        event_id=e.event_id,
                        note=(f"{e.note}:upcoming" if e.note else "upcoming"),
                    )
                    for e in item.evidence
                ),
            )
            for item in items
        ]

    def _health_measures(
        self,
        hazard: str,
        hazard_level: int,
        forecast: ForecastVersion,
        person: FocusPerson | None,
    ) -> list[AdvisoryItem]:
        if hazard_level < 1:
            return []
        evidence = Evidence(
            kind="forecast",
            aggregate_id=forecast.forecast_id,
            version=forecast.version,
            event_id=forecast.event_id,
            note=f"health:{hazard}:L{hazard_level}",
        )
        person_note: Evidence | None = None
        if person is not None:
            person_note = Evidence(
                kind="person",
                aggregate_id=person.person_id,
                version=person.version,
                event_id=person.event_id,
            )

        items: list[AdvisoryItem] = []

        def add(text: str) -> None:
            evidence_set = (evidence, person_note) if person_note else (evidence,)
            items.append(AdvisoryItem(HEALTH_MEASURE, text, evidence_set))

        groups = set(person.groups) if person else set()
        if hazard == "low_temp":
            add("【健康措施】及时添衣，注意头颈、手脚保暖，睡前可热水泡脚。")
            if "elderly" in groups and person is not None:
                add(
                    f"【健康措施】{person.name}为老年重点旅客，夜间请注意保暖，"
                    "备好毛毯与常用药，临睡前避免外出。"
                )
            if hazard_level >= 2:
                add("【健康措施】气温偏低，心脑血管疾病患者减少晨练，随身带好急救药物。")
        elif hazard == "rain":
            add("【健康措施】携带雨具，避开积水路段，谨防滑倒与漏电。")
            if hazard_level >= 2:
                add("【健康措施】强降雨期间减少户外活动，慢性病患者按时服药并留意身体反应。")
        elif hazard == "fog":
            add("【健康措施】雾天减少户外晨练，呼吸道敏感人群佩戴口罩。")
        return items

    def _traffic_measures(
        self,
        hazard: str,
        hazard_level: int,
        segment: Segment,
        forecast: ForecastVersion,
        at: datetime,
    ) -> list[AdvisoryItem]:
        if hazard_level < 1:
            return []
        evidence = Evidence(
            kind="forecast",
            aggregate_id=forecast.forecast_id,
            version=forecast.version,
            event_id=forecast.event_id,
            note=f"traffic:{hazard}:L{hazard_level}",
        )
        route = self._route_evidence(segment)
        items: list[AdvisoryItem] = []
        if hazard == "fog" and hazard_level >= 2:
            items.append(
                AdvisoryItem(
                    TRAFFIC_DISPOSAL,
                    "【交通处置】低能见度，开启雾灯、控制车速、保持车距，必要时就近等待散雾。",
                    (evidence, route),
                )
            )
        elif hazard == "fog":
            items.append(
                AdvisoryItem(
                    TRAFFIC_DISPOSAL,
                    "【交通处置】轻雾路段请开启示廓灯、谨慎驾驶。",
                    (evidence, route),
                )
            )
        if hazard == "rain" and segment.mountain and hazard_level >= 2:
            items.append(
                AdvisoryItem(
                    TRAFFIC_DISPOSAL,
                    f"【交通处置】山区区段 {segment.from_name}→{segment.to_name} 暴雨下路滑、"
                    "存在落石风险，建议绕行替代线路，勿强行通过。",
                    (evidence, route),
                )
            )
        elif hazard == "rain" and segment.mountain:
            items.append(
                AdvisoryItem(
                    TRAFFIC_DISPOSAL,
                    f"【交通处置】山区区段 {segment.from_name}→{segment.to_name} 路面湿滑，减速慢行。",
                    (evidence, route),
                )
            )
        return items

    def _closure_items(self, segment: Segment, forecast: ForecastVersion | None) -> list[AdvisoryItem]:
        if not segment.scenic_area_id:
            return []
        closure = self.catalog.closures.get(segment.scenic_area_id)
        if closure is None or not closure.active:
            return []
        evidence = Evidence(
            kind="scenic_closure",
            aggregate_id=closure.scenic_area_id,
            version=closure.version,
            event_id=closure.event_id,
            note=closure.reason,
        )
        return [
            AdvisoryItem(
                TRAFFIC_DISPOSAL,
                f"【交通处置】景区「{closure.name}」{closure.closed_from:%m-%d %H:%M}起封闭"
                f"（{closure.reason}），区段 {segment.from_name}→{segment.to_name} 终点不可抵达，"
                "请改走替代线路并调整行程。",
                (evidence,),
            )
        ]

    def _reroute_items(self, segment: Segment) -> list[AdvisoryItem]:
        for (route_id, segment_id), status in self.catalog.segment_status.items():
            if segment_id != segment.segment_id or not status.get("closed"):
                continue
            evidence = Evidence(
                kind="route",
                aggregate_id=route_id,
                version=self.catalog.routes[route_id].version if route_id in self.catalog.routes else 0,
                event_id=status["event_id"],
                note="segment_closed",
            )
            alternative = status.get("alternative")
            tail = f"请改走替代线路「{alternative}」。" if alternative else "请等待值班调度绕行安排。"
            return [
                AdvisoryItem(
                    TRAFFIC_DISPOSAL,
                    f"【交通处置】区段 {segment.from_name}→{segment.to_name} 已封闭，{tail}",
                    (evidence,),
                )
            ]
        return []

    def _medical_items(self, segment: Segment) -> list[AdvisoryItem]:
        items: list[AdvisoryItem] = []
        for point_id in segment.medical_point_ids:
            point = self.catalog.medical_points.get(point_id)
            if point is None or point.active:
                continue
            evidence = Evidence(
                kind="medical_point",
                aggregate_id=point.point_id,
                version=point.version,
                event_id=point.event_id,
                note="withdrawn",
            )
            if point.backup_point_id:
                backup = self.catalog.medical_points.get(point.backup_point_id)
                backup_name = backup.name if backup else point.backup_point_id
                items.append(
                    AdvisoryItem(
                        TRAFFIC_DISPOSAL,
                        f"【交通处置】区段沿线临时医疗点「{point.name}」已下线，"
                        f"急救请改往备案医疗点「{backup_name}」（{point.backup_point_id}），线路其余服务不变。",
                        (evidence,),
                    )
                )
            else:
                items.append(
                    AdvisoryItem(
                        TRAFFIC_DISPOSAL,
                        f"【交通处置】区段沿线临时医疗点「{point.name}」已下线，"
                        "请联系值班调度获取最近医疗点，本区域其他服务不受影响。",
                        (evidence,),
                    )
                )
        return items

    def _route_evidence(self, segment: Segment) -> Evidence:
        for route in self.catalog.routes.values():
            for candidate in route.segments:
                if candidate.segment_id == segment.segment_id:
                    return Evidence(
                        kind="route",
                        aggregate_id=route.route_id,
                        version=route.version,
                        event_id=route.event_id,
                    )
        return Evidence("route", "", 0, "")
