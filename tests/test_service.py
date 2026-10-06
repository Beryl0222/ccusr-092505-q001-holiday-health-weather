"""假日气象健康联动服务的端到端业务规则测试。"""

from __future__ import annotations

import unittest

from holiday_health_weather import (
    HEALTH_MEASURE,
    TRAFFIC_DISPOSAL,
    WEATHER_FACT,
    Event,
    EventConflictError,
    WorkflowError,
)

from support import AREA, OTHER_AREA, build_service, cst, fog, low_temp, rain, register_trip

S1 = "seg-center-lingfeng"
S2 = "seg-lingfeng-dalongqiu"
N1 = f"notice-trip-wang-{S1}"
N2 = f"notice-trip-wang-{S2}"


def categories(notice) -> set[str]:  # type: ignore[no-untyped-def]
    return {item.category for item in notice.items}


def texts(notice) -> list[str]:  # type: ignore[no-untyped-def]
    return [item.text for item in notice.items]


class RegistrationAndAdvisoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_service()
        self.svc.register_forecast(
            "fc-1002",
            AREA,
            cst(2026, 10, 2, 0),
            cst(2026, 10, 3, 0),
            [rain(12, cst(2026, 10, 2, 8)), low_temp(3, cst(2026, 10, 2, 22))],
        )
        register_trip(self.svc)
        self.svc.draft_for_trip("trip-wang")
        self.cat = self.svc.catalog

    def test_six_registrations_are_replayed(self) -> None:
        self.assertEqual({"雁荡山区"}, set(self.cat.thresholds))
        self.assertIn("r-lingfeng", self.cat.routes)
        self.assertIn("p-wang", self.cat.persons)
        self.assertTrue(self.cat.medical_points["mp-lingfeng"].active)
        self.assertTrue(self.cat.authorizations[AREA].granted)
        self.assertEqual(2, len(self.cat.forecasts["fc-1002"][0].measures))

    def test_advisory_splits_into_three_traceable_categories(self) -> None:
        notice = self.cat.notices[N1]
        self.assertEqual({WEATHER_FACT, HEALTH_MEASURE, TRAFFIC_DISPOSAL}, categories(notice))

    def test_every_item_carries_evidence(self) -> None:
        notice = self.cat.notices[N1]
        for item in notice.items:
            self.assertTrue(item.evidence, msg=item.text)
            for evidence in item.evidence:
                self.assertTrue(evidence.event_id)
                self.assertGreaterEqual(evidence.version, 1)

    def test_elderly_gets_night_warmth_measure(self) -> None:
        all_text = "\n".join(texts(self.cat.notices[N1]))
        self.assertIn("王老伯", all_text)
        self.assertIn("夜间请注意保暖", all_text)

    def test_effective_forecast_chosen_by_area_and_validity(self) -> None:
        picked = self.cat.effective_forecast(AREA, cst(2026, 10, 2, 12))
        self.assertIsNotNone(picked)
        self.assertIsNone(self.cat.effective_forecast(AREA, cst(2026, 10, 4)))
        self.assertIsNone(self.cat.effective_forecast(OTHER_AREA, cst(2026, 10, 2, 12)))


class GradedEscalationTests(unittest.TestCase):
    def test_levels_escalate_as_clock_advances_through_night(self) -> None:
        svc = build_service(cst(2026, 10, 2, 8))
        # 白天仅小到中雨（注意），入夜降温，深夜局地暴雨+低温
        svc.register_forecast(
            "fc-1002", AREA, cst(2026, 10, 2, 0), cst(2026, 10, 3, 0),
            [rain(12, cst(2026, 10, 2, 8)), rain(55, cst(2026, 10, 2, 23)),
             low_temp(6, cst(2026, 10, 2, 18)), low_temp(-1, cst(2026, 10, 2, 23))],
        )
        # 单段行程，聚焦升级事件数量
        svc.register_trip("trip-wang", "p-wang", "r-lingfeng", "灵峰", [S1])
        svc.draft_for_trip("trip-wang")
        self.assertEqual(1, svc.catalog.notices[N1].level)

        # 推进到 18 点：低温注意，雨仍为白天注意，级别不变不产生事件
        self.assertEqual([], svc.advance_and_evaluate(hours=10))
        self.assertEqual(1, svc.catalog.notices[N1].level)

        # 推进到 23 点：暴雨 L3、低温 L3，一次升级
        events = svc.advance_and_evaluate(hours=5)
        self.assertEqual(1, len(events))
        self.assertEqual("RISK_ESCALATED", events[0].event_type)
        self.assertEqual(3, events[0].payload["to_level"])
        self.assertEqual(3, svc.catalog.notices[N1].level)

    def test_both_pending_notices_escalate_together(self) -> None:
        svc = build_service(cst(2026, 10, 2, 8))
        svc.register_forecast(
            "fc-1002", AREA, cst(2026, 10, 2, 0), cst(2026, 10, 3, 0),
            [rain(55, cst(2026, 10, 2, 9))],
        )
        register_trip(svc)
        svc.draft_for_trip("trip-wang")
        events = svc.advance_and_evaluate(hours=1)
        self.assertEqual({N1, N2}, {e.aggregate_id for e in events})

    def test_light_fog_starts_at_notice_level(self) -> None:
        svc = build_service(cst(2026, 10, 2, 6))
        svc.register_forecast(
            "fc-fog", AREA, cst(2026, 10, 2, 0), cst(2026, 10, 3, 0),
            [fog(800, cst(2026, 10, 2, 6))],
        )
        register_trip(svc)
        svc.draft_for_trip("trip-wang")
        notice = svc.catalog.notices[N1]
        self.assertEqual(1, notice.level)
        self.assertTrue(any("示廓灯" in t for t in texts(notice)))

    def test_no_escalation_event_when_level_unchanged_or_lower(self) -> None:
        svc = build_service(cst(2026, 10, 2, 23))
        svc.register_forecast(
            "fc-cold", AREA, cst(2026, 10, 2, 0), cst(2026, 10, 3, 0),
            [low_temp(-1, cst(2026, 10, 2, 23))],
        )
        register_trip(svc)
        svc.draft_for_trip("trip-wang")
        self.assertEqual(3, svc.catalog.notices[N1].level)
        svc.clock.advance(hours=10)  # 白天气温回升，但未登记新值
        self.assertEqual([], svc.evaluate_due())


class RevisionFreezeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_service(cst(2026, 10, 2, 8))
        self.svc.register_forecast(
            "fc-1002", AREA, cst(2026, 10, 2, 0), cst(2026, 10, 3, 0),
            [rain(8, cst(2026, 10, 2, 8))],
        )
        register_trip(self.svc)
        self.svc.draft_for_trip("trip-wang")
        self.svc.approve_notice(N1, "值班员小李")
        self.svc.send_notice(N1)
        # 两区段：N1 已送达，N2 未送达

    def _revise(self) -> None:
        self.svc.register_forecast(
            "fc-1002", AREA, cst(2026, 10, 2, 0), cst(2026, 10, 3, 0),
            [rain(40, cst(2026, 10, 2, 8))],
        )

    def test_revision_only_affects_not_yet_sent_notices(self) -> None:
        self._revise()
        cat = self.svc.catalog
        sent = cat.notices[N1]
        pending = cat.notices[N2]
        self.assertEqual("sent", sent.status)
        self.assertEqual(1, sent.basis.forecast_version)
        self.assertEqual(0, sent.level)
        self.assertEqual(2, pending.basis.forecast_version)
        self.assertEqual(2, pending.level)

    def test_sent_notice_keeps_snapshot_and_basis(self) -> None:
        self._revise()
        sent = self.svc.catalog.notices[N1]
        self.assertEqual(1, len(sent.history))
        self.assertEqual(1, sent.history[0].basis.forecast_version)
        # 冻结跳过留痕可查
        self.assertTrue(any(row["notice_id"] == N1 for row in self.svc.audit))

    def test_revision_recreates_content_against_new_forecast(self) -> None:
        self._revise()
        pending = self.svc.catalog.notices[N2]
        weather = [item for item in pending.items if item.category == WEATHER_FACT]
        self.assertEqual("fc-1002", weather[0].evidence[0].aggregate_id)
        self.assertEqual(2, weather[0].evidence[0].version)
        self.assertEqual(2, len(pending.history))

    def test_sent_notice_never_escalates_on_clock_advance(self) -> None:
        self.svc.clock.advance(hours=1)
        self.assertEqual([], self.svc.evaluate_due())


class ReceiptIdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_service()
        self.svc.register_forecast(
            "fc-1002", AREA, cst(2026, 10, 2, 0), cst(2026, 10, 3, 0),
            [rain(6, cst(2026, 10, 2, 8))],
        )
        register_trip(self.svc)
        self.svc.draft_for_trip("trip-wang")
        self.svc.approve_notice(N1, "值班员小李")
        self.svc.send_notice(N1)

    def test_cross_device_acknowledgement_is_idempotent(self) -> None:
        first, dup1 = self.svc.acknowledge(N1, "p-wang", "phone-a")
        second, dup2 = self.svc.acknowledge(N1, "p-wang", "watch-b")
        third, dup3 = self.svc.acknowledge(N1, "p-wang", "phone-a")
        self.assertFalse(dup1)
        self.assertTrue(dup2)
        self.assertTrue(dup3)
        receipts = self.svc.catalog.notices[N1].receipts
        self.assertEqual(1, len(receipts))
        self.assertEqual(first.event_id, second.event_id)
        # 重放结果一致：存储里只有一条确认事件
        self.assertEqual(1, len(self.svc.catalog.notices[N1].receipts))

    def test_replay_is_idempotent_even_if_duplicates_reach_the_stream(self) -> None:
        """第二道防线：即便重复回执绕过指纹直接进入事件流，重放仍只生效一次。"""

        from holiday_health_weather import Catalog, Event as Ev

        base = build_service()
        base.register_forecast(
            "fc-1002", AREA, cst(2026, 10, 2, 0), cst(2026, 10, 3, 0),
            [rain(6, cst(2026, 10, 2, 8))],
        )
        register_trip(base)
        base.draft_for_trip("trip-wang")
        events = list(base.store.all_events())
        # 外部库异常导致两条同旅客同阶段、不同 event_id 的确认事件都入了流
        events.append(
            Ev("ext-ack-1", "RECEIPT_ACKNOWLEDGED", "health_notice", N1,
               cst(2026, 10, 2, 9), 2, "手机确认",
               {"person_id": "p-wang", "device": "phone-a", "devices": ["phone-a"],
                "at": cst(2026, 10, 2, 9).isoformat()})
        )
        events.append(
            Ev("ext-ack-2", "RECEIPT_ACKNOWLEDGED", "health_notice", N1,
               cst(2026, 10, 2, 9, 5), 3, "手表重复确认",
               {"person_id": "p-wang", "device": "watch-b", "devices": ["watch-b"],
                "at": cst(2026, 10, 2, 9, 5).isoformat()})
        )
        catalog = Catalog.replay(sorted(events, key=lambda e: (e.occurred_at, e.event_id)))
        self.assertEqual(1, len(catalog.notices[N1].receipts))
        self.assertTrue(any(i["code"] == "duplicate_receipt" for i in catalog.ignored))

    def test_duplicate_ack_does_not_trigger_new_events(self) -> None:
        before = len(self.svc.store.all_events())
        self.svc.acknowledge(N1, "p-wang", "phone-a")
        self.svc.acknowledge(N1, "p-wang", "watch-b")
        self.assertEqual(before + 1, len(self.svc.store.all_events()))

    def test_acknowledge_and_completion_are_distinct_stages(self) -> None:
        self.svc.acknowledge(N1, "p-wang", "phone-a")
        done, dup = self.svc.complete_receipt(N1, "p-wang", "watch-b")
        self.assertFalse(dup)
        stages = {receipt.stage for receipt in self.svc.catalog.notices[N1].receipts.values()}
        self.assertEqual({"acknowledged", "completed"}, stages)


class DestinationChangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_service()
        self.svc.register_forecast(
            "fc-1002", AREA, cst(2026, 10, 2, 0), cst(2026, 10, 3, 0),
            [rain(6, cst(2026, 10, 2, 8))],
        )
        register_trip(self.svc)
        self.svc.draft_for_trip("trip-wang")

    def test_only_affected_segments_are_recalculated(self) -> None:
        # 目的地改为灵峰：S2 被移出，S1 保留
        events = self.svc.change_destination("trip-wang", "灵峰", [S1])
        recalculated = {e.aggregate_id for e in events if e.event_type == "ADVISORY_RECALCULATED"}
        self.assertEqual({N2}, recalculated)
        cat = self.svc.catalog
        self.assertEqual("drafted", cat.notices[N1].status)
        self.assertEqual("superseded", cat.notices[N2].status)
        trip = cat.trips["trip-wang"]
        self.assertEqual(("seg-center-lingfeng",), trip.segment_ids)

    def test_kept_segment_preserves_its_approval(self) -> None:
        self.svc.approve_notice(N1, "值班员小李")
        self.svc.change_destination("trip-wang", "灵峰", [S1])
        self.assertEqual("approved", self.svc.catalog.notices[N1].status)

    def test_new_segment_is_drafted_automatically(self) -> None:
        # 改走白溪绕行线：S1 保留，S2 取消，新增 seg-baixi
        self.svc.change_destination(
            "trip-wang", "大龙湫后门", [S1, "seg-baixi"]
        )
        notices = self.svc.catalog.notices_for_trip("trip-wang")
        new_ids = {n.segment_id for n in notices if n.status != "superseded"}
        self.assertEqual({S1, "seg-baixi"}, new_ids)

    def test_sent_superseded_notice_cannot_be_sent(self) -> None:
        self.svc.change_destination("trip-wang", "灵峰", [S1])
        with self.assertRaises(WorkflowError):
            self.svc.send_notice(N2)


class MedicalPointAndScenicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_service()
        self.svc.register_forecast(
            "fc-1002", AREA, cst(2026, 10, 2, 0), cst(2026, 10, 3, 0),
            [rain(6, cst(2026, 10, 2, 8))],
        )
        register_trip(self.svc)
        self.svc.draft_for_trip("trip-wang")

    def test_point_withdrawal_only_replaces_related_segment(self) -> None:
        events = self.svc.withdraw_medical_point("mp-lingfeng")
        recalculated = {e.aggregate_id for e in events if e.event_type == "ADVISORY_RECALCULATED"}
        self.assertEqual({N1}, recalculated)
        cat = self.svc.catalog
        s1_text = "\n".join(texts(cat.notices[N1]))
        self.assertIn("灵峰临时点", s1_text)
        self.assertIn("雁荡镇卫生院", s1_text)
        # S2 不含下线点，不产生医疗点处置
        s2_text = "\n".join(texts(cat.notices[N2]))
        self.assertNotIn("灵峰临时点", s2_text)

    def test_other_area_keeps_serving(self) -> None:
        self.svc.withdraw_medical_point("mp-lingfeng")
        self.assertTrue(self.svc.catalog.medical_points["mp-lake"].active)
        # 未引用下线点的区域不产生任何重算事件（N2 同区但不引用也不应重算）
        # （mp-lake 属楠溪湖区，无行程受影响）
        trip_notices = [n for n in self.svc.catalog.notices_for_trip("trip-wang")]
        # N2 历史应只有草稿一版
        self.assertEqual(1, next(n for n in trip_notices if n.notice_id == N2).history.__len__())

    def test_scenic_closure_produces_traffic_disposal(self) -> None:
        self.svc.notify_scenic_closure(
            "scenic-dalongqiu", "大龙湫景区", AREA,
            cst(2026, 10, 2, 12), cst(2026, 10, 3, 12), "上游道路塌方",
        )
        cat = self.svc.catalog
        s2 = cat.notices[N2]
        traffic = [t for t in texts(s2) if "大龙湫景区" in t]
        self.assertEqual(1, len(traffic))
        evidence_kinds = {e.kind for item in s2.items for e in item.evidence}
        self.assertIn("scenic_closure", evidence_kinds)
        # S1 不经过封闭景区，不受影响
        self.assertFalse(any("封闭" in t for t in texts(cat.notices[N1])))

    def test_reroute_segment_only_affects_trips_using_it(self) -> None:
        self.svc.reroute_segment("r-lingfeng", S1, alternative="白溪绕行线")
        s1 = self.svc.catalog.notices[N1]
        self.assertTrue(any("封闭" in t for t in texts(s1)))
        self.assertFalse(any("封闭" in t for t in texts(self.svc.catalog.notices[N2])))


class AuthorizationWorkflowTests(unittest.TestCase):
    def test_send_requires_authorization_and_approval(self) -> None:
        svc = build_service()
        svc.revoke_authorization("auth-yd", AREA)
        svc.register_forecast(
            "fc-1", AREA, cst(2026, 10, 2, 0), cst(2026, 10, 3, 0),
            [rain(6, cst(2026, 10, 2, 8))],
        )
        register_trip(svc)
        svc.draft_for_trip("trip-wang")
        from holiday_health_weather import AuthorizationError

        with self.assertRaises(AuthorizationError):
            svc.approve_notice(N1, "值班员小李")
        with self.assertRaises(WorkflowError):
            svc.send_notice(N1)

    def test_recalculation_after_approval_requires_reapproval(self) -> None:
        svc = build_service()
        svc.register_forecast(
            "fc-1", AREA, cst(2026, 10, 2, 0), cst(2026, 10, 3, 0),
            [rain(6, cst(2026, 10, 2, 8))],
        )
        register_trip(svc)
        svc.draft_for_trip("trip-wang")
        svc.approve_notice(N1, "小李")
        svc.register_forecast(
            "fc-1", AREA, cst(2026, 10, 2, 0), cst(2026, 10, 3, 0),
            [rain(40, cst(2026, 10, 2, 8))],
        )
        self.assertEqual("drafted", svc.catalog.notices[N1].status)
        # 第一次批准仍然留档
        self.assertEqual("小李", svc.trace(N1)["approved_by"][0]["by"])


class OfflineSyncTests(unittest.TestCase):
    def test_duplicate_submissions_merge_once(self) -> None:
        svc = build_service()
        svc.register_forecast(
            "fc-1", AREA, cst(2026, 10, 2, 0), cst(2026, 10, 3, 0),
            [rain(6, cst(2026, 10, 2, 8))],
        )
        register_trip(svc)
        svc.draft_for_trip("trip-wang")
        svc.approve_notice(N1, "小李")
        svc.send_notice(N1)

        # 离线期间旅客用两台设备各点了一次确认，网络恢复后批量补传
        e1 = Event(
            event_id="offline-ack-1",
            event_type="RECEIPT_ACKNOWLEDGED",
            aggregate_type="health_notice",
            aggregate_id=N1,
            occurred_at=cst(2026, 10, 2, 9),
            version=svc.store.current_version(N1) + 1,
            summary="手机确认",
            payload={"person_id": "p-wang", "device": "phone-a", "devices": ["phone-a"],
                     "at": cst(2026, 10, 2, 9).isoformat()},
        )
        e2 = Event(
            event_id="offline-ack-2",
            event_type="RECEIPT_ACKNOWLEDGED",
            aggregate_type="health_notice",
            aggregate_id=N1,
            occurred_at=cst(2026, 10, 2, 9, 5),
            version=svc.store.current_version(N1) + 2,
            summary="手表确认",
            payload={"person_id": "p-wang", "device": "watch-b", "devices": ["watch-b"],
                     "at": cst(2026, 10, 2, 9, 5).isoformat()},
        )
        results = svc.sync([e2, e1])  # 故意乱序补传
        self.assertEqual(2, len(results))
        self.assertFalse(results[0][1])  # 晚到的 e1 先按时间落位
        self.assertTrue(results[1][1])   # e2 与 e1 业务同指，被合并
        self.assertEqual(1, len(svc.catalog.notices[N1].receipts))

    def test_late_events_replay_by_occurrence_time(self) -> None:
        svc = build_service()
        early = Event(
            event_id="late-1", event_type="THRESHOLD_REGISTERED",
            aggregate_type="risk_threshold", aggregate_id="th-late",
            occurred_at=cst(2026, 10, 1, 6), version=1,
            summary="晚到的阈值",
            payload={"area": "南部山区", "rules": {"rain": [{"level": 1, "cutoff": 5, "name": "注意"}]}},
        )
        late = Event(
            event_id="late-2", event_type="PERSON_REGISTERED",
            aggregate_type="focus_person", aggregate_id="p-late",
            occurred_at=cst(2026, 10, 1, 9), version=1,
            summary="晚到的人员",
            payload={"name": "迟到登记", "groups": [], "devices": []},
        )
        svc.sync([late, early])
        ordered = svc.store.all_events()
        self.assertLess(ordered[0].occurred_at, ordered[1].occurred_at)
        self.assertEqual("th-late", ordered[0].aggregate_id)

    def test_late_forecast_revision_triggers_recalculation_after_sync(self) -> None:
        svc = build_service(cst(2026, 10, 2, 8))
        svc.register_forecast(
            "fc-1", AREA, cst(2026, 10, 2, 0), cst(2026, 10, 3, 0),
            [rain(6, cst(2026, 10, 2, 8))],
        )
        register_trip(svc)
        svc.draft_for_trip("trip-wang")
        self.assertEqual(0, svc.catalog.notices[N1].level)

        # 离线时气象台发布了修订，恢复网络后晚到补传
        revision = Event(
            event_id="offline-rev", event_type="FORECAST_REVISED",
            aggregate_type="forecast", aggregate_id="fc-1",
            occurred_at=cst(2026, 10, 2, 8, 30), version=2,
            summary="局地暴雨修订",
            payload={
                "area": AREA,
                "valid_from": cst(2026, 10, 2, 0).isoformat(),
                "valid_to": cst(2026, 10, 3, 0).isoformat(),
                "measures": [rain(45, cst(2026, 10, 2, 8))],
            },
        )
        svc.sync([revision])
        notice = svc.catalog.notices[N1]
        self.assertEqual(2, notice.basis.forecast_version)
        self.assertEqual(2, notice.level)

    def test_same_event_id_resubmitted_is_idempotent(self) -> None:
        from holiday_health_weather import EventStore

        store = EventStore()
        event = Event(
            event_id="fixed-id", event_type="PERSON_REGISTERED",
            aggregate_type="focus_person", aggregate_id="p-x",
            occurred_at=cst(2026, 10, 2, 8), version=1,
            summary="重复上报同一事件",
            payload={"name": "张三", "groups": [], "devices": []},
        )
        first, dup1 = store.append(event)
        second, dup2 = store.append(event)
        self.assertFalse(dup1)
        self.assertTrue(dup2)
        self.assertIs(first, second)
        self.assertEqual(1, len(store.all_events()))

    def test_same_id_with_different_content_is_rejected(self) -> None:
        svc = build_service()
        event = Event(
            event_id="fixed-id", event_type="PERSON_REGISTERED",
            aggregate_type="focus_person", aggregate_id="p-x",
            occurred_at=cst(2026, 10, 2, 8), version=1,
            summary="第一次",
            payload={"name": "张三", "groups": [], "devices": []},
        )
        tampered = Event(
            event_id="fixed-id", event_type="PERSON_REGISTERED",
            aggregate_type="focus_person", aggregate_id="p-x",
            occurred_at=cst(2026, 10, 2, 8), version=1,
            summary="内容被改",
            payload={"name": "李四", "groups": [], "devices": []},
        )
        svc.store.append(event)
        with self.assertRaises(EventConflictError):
            svc.store.append(tampered)
        self.assertTrue(any(r["code"] == "id_conflict" for r in svc.store.rejections))

    def test_optimistic_version_conflict_is_rejected_and_logged(self) -> None:
        svc = build_service()
        e1 = Event(
            event_id="v-1", event_type="PERSON_REGISTERED",
            aggregate_type="focus_person", aggregate_id="p-y",
            occurred_at=cst(2026, 10, 2, 8), version=1, summary="a",
            payload={"name": "A", "groups": [], "devices": []},
        )
        e3 = Event(
            event_id="v-3", event_type="PERSON_REGISTERED",
            aggregate_type="focus_person", aggregate_id="p-y",
            occurred_at=cst(2026, 10, 2, 9), version=3, summary="跳号",
            payload={"name": "B", "groups": [], "devices": []},
        )
        svc.store.append(e1)
        with self.assertRaises(EventConflictError):
            svc.store.append(e3)
        self.assertTrue(any(r["code"] == "version_gap" for r in svc.store.rejections))


class TraceTests(unittest.TestCase):
    def test_trace_reconstructs_full_provenance(self) -> None:
        svc = build_service(cst(2026, 10, 2, 8))
        svc.register_forecast(
            "fc-1002", AREA, cst(2026, 10, 2, 0), cst(2026, 10, 3, 0),
            [rain(6, cst(2026, 10, 2, 8))],
        )
        register_trip(svc)
        svc.draft_for_trip("trip-wang")
        svc.approve_notice(N1, "值班员小李")
        svc.send_notice(N1)
        svc.acknowledge(N1, "p-wang", "phone-a")
        svc.complete_receipt(N1, "p-wang", "phone-a")

        trace = svc.trace(N1)
        self.assertEqual("sent", trace["status"])
        self.assertEqual("fc-1002", trace["current_basis"]["forecast_id"])
        self.assertEqual(1, trace["current_basis"]["forecast_version"])
        self.assertEqual("值班员小李", trace["approved_by"][0]["by"])
        self.assertEqual(2, len(trace["delivered"]["recipients"]))
        stages = {r["stage"] for r in trace["receipts"]}
        self.assertEqual({"acknowledged", "completed"}, stages)
        event_types = [e["event_type"] for e in trace["events"]]
        self.assertEqual(
            ["ADVISORY_DRAFTED", "ADVISORY_APPROVED", "ADVISORY_SENT",
             "RECEIPT_ACKNOWLEDGED", "RECEIPT_COMPLETED"],
            event_types,
        )
        # 每版快照的每条内容都可回溯到事件
        for snapshot in trace["history"]:
            for item in snapshot["items"]:
                for evidence in item["evidence"]:
                    self.assertTrue(evidence["event_id"])

    def test_trace_follows_forecast_revision_chain(self) -> None:
        svc = build_service(cst(2026, 10, 2, 8))
        svc.register_forecast(
            "fc-1002", AREA, cst(2026, 10, 2, 0), cst(2026, 10, 3, 0),
            [rain(6, cst(2026, 10, 2, 8))],
        )
        register_trip(svc)
        svc.draft_for_trip("trip-wang")
        svc.register_forecast(
            "fc-1002", AREA, cst(2026, 10, 2, 0), cst(2026, 10, 3, 0),
            [rain(40, cst(2026, 10, 2, 8))],
        )
        trace = svc.trace(N1)
        versions = [(h["snapshot"], h["basis"]["forecast_version"], h["reason"]) for h in trace["history"]]
        self.assertEqual([(1, 1, "draft"), (2, 2, "forecast_revised")], versions)
        # 重算事件的因果链指向预报修订事件
        recalc = next(e for e in trace["events"] if e["event_type"] == "ADVISORY_RECALCULATED")
        self.assertIsNotNone(recalc["causation_id"])
        self.assertTrue(recalc["causation_id"].startswith("000"))


if __name__ == "__main__":
    unittest.main()
