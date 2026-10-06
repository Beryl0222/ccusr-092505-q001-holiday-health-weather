from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from holiday_health_weather import (
    EscalationLevel,
    ForecastVersion,
    HazardObservation,
    HazardType,
    LinkageService,
    ManualClock,
    MedicalPoint,
    NotFoundError,
    NoticeAuthorization,
    NoticeStatus,
    PartKind,
    PopulationGroup,
    RiskThreshold,
    RouteSegment,
    StateError,
    TravelRoute,
    validate_event,
)

TZ = timezone(timedelta(hours=8))
T0 = datetime(2026, 10, 1, 6, 0, tzinfo=TZ)
MOUNTAIN = "山区县"
PLAIN = "平原区"


def make_forecast(version: int, issued_at: datetime, detail: str = "局地暴雨") -> ForecastVersion:
    return ForecastVersion(
        forecast_id="F1",
        region=MOUNTAIN,
        version=version,
        issued_at=issued_at,
        effective_from=T0 - timedelta(hours=1),
        effective_to=T0 + timedelta(hours=24),
        hazards=(
            HazardObservation(HazardType.LOCAL_RAINSTORM, T0, detail),
            HazardObservation(HazardType.LOW_TEMPERATURE, T0, "夜间低温"),
        ),
    )


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock(T0)
        self.service = LinkageService(self.clock)
        svc = self.service
        svc.register_threshold(
            RiskThreshold("TH-RS", MOUNTAIN, HazardType.LOCAL_RAINSTORM,
                          timedelta(hours=0), timedelta(hours=2), timedelta(hours=6))
        )
        svc.register_threshold(
            RiskThreshold("TH-LT", MOUNTAIN, HazardType.LOW_TEMPERATURE,
                          timedelta(hours=1), timedelta(hours=4), timedelta(hours=8))
        )
        svc.register_threshold(
            RiskThreshold("TH-LF", MOUNTAIN, HazardType.LIGHT_FOG,
                          timedelta(hours=0), timedelta(hours=3), timedelta(hours=9))
        )
        svc.register_medical_point(MedicalPoint("P1", MOUNTAIN, "山顶临时医疗点", temporary=True))
        svc.register_medical_point(MedicalPoint("P2", MOUNTAIN, "县城卫生院"))
        svc.register_medical_point(MedicalPoint("P9", PLAIN, "平原医务室"))
        svc.register_route(TravelRoute("R1", MOUNTAIN, (
            RouteSegment("S1", "县城", "景区口"),
            RouteSegment("S2", "景区口", "山顶", mountain_road=True, medical_point_ids=("P1",)),
        )))
        svc.register_route(TravelRoute("R2", MOUNTAIN, (
            RouteSegment("S1", "县城", "景区口"),
            RouteSegment("S3", "景区口", "湖畔", medical_point_ids=("P2",)),
        )))
        svc.register_route(TravelRoute("R9", PLAIN, (
            RouteSegment("S9", "车站", "古镇", medical_point_ids=("P9",)),
        )))
        svc.register_population(PopulationGroup("G1", MOUNTAIN, "老人", night_warmth_required=True))
        svc.register_authorization(NoticeAuthorization("A1", MOUNTAIN, "张伟"))
        svc.register_forecast(make_forecast(1, T0 - timedelta(minutes=30)))
        svc.register_forecast(ForecastVersion(
            forecast_id="F9", region=PLAIN, version=1,
            issued_at=T0 - timedelta(minutes=30),
            effective_from=T0 - timedelta(hours=1),
            effective_to=T0 + timedelta(hours=24),
        ))

    def part(self, notice, kind, ref=None):
        for part in notice.parts:
            if part.kind != kind:
                continue
            if ref is None or ref in part.basis:
                return part
        return None


class ComposeTests(ServiceTestBase):
    def test_advice_splits_into_three_traceable_kinds(self) -> None:
        notice = self.service.compose_notice("甲", "R1", ("G1",))
        self.assertEqual(
            {PartKind.WEATHER_FACT, PartKind.HEALTH_MEASURE, PartKind.TRAFFIC_HANDLING},
            {part.kind for part in notice.parts},
        )
        weather = self.part(notice, PartKind.WEATHER_FACT)
        self.assertEqual(("forecast:F1@v1",), weather.basis)
        self.assertIn("LOCAL_RAINSTORM", weather.content)
        health = self.part(notice, PartKind.HEALTH_MEASURE)
        self.assertIn("population:G1", health.basis)
        self.assertIn("夜间注意保暖", health.content)
        mountain = self.part(notice, PartKind.TRAFFIC_HANDLING, "segment:S2")
        self.assertIn("point:P1", mountain.basis)
        self.assertIn("forecast:F1@v1", mountain.basis)
        self.assertIn("山区道路", mountain.content)

    def test_compose_without_effective_forecast_fails(self) -> None:
        self.service.register_route(TravelRoute("R0", "无人区", (RouteSegment("S0", "甲地", "乙地"),)))
        with self.assertRaises(NotFoundError):
            self.service.compose_notice("甲", "R0")


class ForecastRevisionTests(ServiceTestBase):
    def test_revision_rebases_only_not_yet_sent_notices(self) -> None:
        sent = self.service.compose_notice("甲", "R1")
        self.service.approve_notice(sent.notice_id, "A1")
        self.service.send_notice(sent.notice_id)
        approved = self.service.compose_notice("乙", "R1")
        self.service.approve_notice(approved.notice_id, "A1")
        draft = self.service.compose_notice("丙", "R1")

        self.service.register_forecast(make_forecast(2, T0 + timedelta(minutes=10), "暴雨增强"))

        self.assertEqual(1, sent.forecast_version)
        self.assertIn("forecast:F1@v1", self.part(sent, PartKind.WEATHER_FACT).basis)
        for notice in (approved, draft):
            self.assertEqual(2, notice.forecast_version)
            self.assertIn("forecast:F1@v2", self.part(notice, PartKind.WEATHER_FACT).basis)
            self.assertIn("暴雨增强", self.part(notice, PartKind.WEATHER_FACT).content)
        self.assertNotIn("暴雨增强", self.part(sent, PartKind.WEATHER_FACT).content)


class ReceiptTests(ServiceTestBase):
    def test_cross_device_confirmation_does_not_retrigger_escalation(self) -> None:
        notice = self.service.compose_notice("甲", "R1")
        self.service.approve_notice(notice.notice_id, "A1")
        self.service.send_notice(notice.notice_id)

        receipt1, triggered1 = self.service.confirm_receipt(notice.notice_id, "甲", "手机")
        receipt2, triggered2 = self.service.confirm_receipt(notice.notice_id, "甲", "平板")

        self.assertTrue(triggered1)
        self.assertFalse(triggered2)
        self.assertEqual(receipt1, receipt2)
        self.assertEqual(1, len(notice.receipts))
        receipt_records = [
            record for record in self.service.escalations
            if record.trigger == "RECEIPT" and record.notice_id == notice.notice_id
        ]
        self.assertEqual(1, len(receipt_records))

    def test_receipt_requires_sent_notice(self) -> None:
        notice = self.service.compose_notice("甲", "R1")
        with self.assertRaises(StateError):
            self.service.confirm_receipt(notice.notice_id, "甲", "手机")


class DestinationChangeTests(ServiceTestBase):
    def test_only_affected_segments_are_recomputed(self) -> None:
        notice = self.service.compose_notice("甲", "R1", ("G1",))
        weather_before = self.part(notice, PartKind.WEATHER_FACT)
        health_before = self.part(notice, PartKind.HEALTH_MEASURE)
        s1_before = self.part(notice, PartKind.TRAFFIC_HANDLING, "segment:S1")

        self.service.change_destination(notice.notice_id, "R2")

        self.assertIs(weather_before, self.part(notice, PartKind.WEATHER_FACT))
        self.assertIs(health_before, self.part(notice, PartKind.HEALTH_MEASURE))
        self.assertIs(s1_before, self.part(notice, PartKind.TRAFFIC_HANDLING, "segment:S1"))
        self.assertIsNone(self.part(notice, PartKind.TRAFFIC_HANDLING, "segment:S2"))
        s3 = self.part(notice, PartKind.TRAFFIC_HANDLING, "segment:S3")
        self.assertIn("point:P2", s3.basis)
        self.assertIn("湖畔", s3.content)

    def test_sent_notice_keeps_original_destination(self) -> None:
        notice = self.service.compose_notice("甲", "R1")
        self.service.approve_notice(notice.notice_id, "A1")
        self.service.send_notice(notice.notice_id)
        with self.assertRaises(StateError):
            self.service.change_destination(notice.notice_id, "R2")


class EscalationTests(ServiceTestBase):
    def test_levels_advance_with_injected_clock(self) -> None:
        svc = self.service
        first = svc.refresh_escalations()
        self.assertEqual([EscalationLevel.ATTENTION], [r.level for r in first])
        self.assertEqual(EscalationLevel.ATTENTION,
                         svc.escalation_level(MOUNTAIN, HazardType.LOCAL_RAINSTORM))
        self.assertEqual(EscalationLevel.NONE,
                         svc.escalation_level(MOUNTAIN, HazardType.LOW_TEMPERATURE))

        self.clock.advance(timedelta(hours=3))
        second = svc.refresh_escalations()
        self.assertEqual(
            {(HazardType.LOCAL_RAINSTORM, EscalationLevel.WARNING),
             (HazardType.LOW_TEMPERATURE, EscalationLevel.ATTENTION)},
            {(r.hazard, r.level) for r in second},
        )

        self.clock.advance(timedelta(hours=4))
        third = svc.refresh_escalations()
        self.assertEqual(
            {(HazardType.LOCAL_RAINSTORM, EscalationLevel.SEVERE),
             (HazardType.LOW_TEMPERATURE, EscalationLevel.WARNING)},
            {(r.hazard, r.level) for r in third},
        )
        self.assertEqual([], svc.refresh_escalations())

    def test_other_region_stays_quiet(self) -> None:
        self.service.refresh_escalations()
        self.assertEqual(EscalationLevel.NONE,
                         self.service.escalation_level(PLAIN, HazardType.LOCAL_RAINSTORM))


class MedicalPointWithdrawalTests(ServiceTestBase):
    def test_withdrawal_replaces_related_routes_only(self) -> None:
        sent = self.service.compose_notice("甲", "R1")
        self.service.approve_notice(sent.notice_id, "A1")
        self.service.send_notice(sent.notice_id)
        draft = self.service.compose_notice("乙", "R1")

        self.service.withdraw_medical_point("P1")

        route = self.service._routes["R1"]
        self.assertEqual(("P2",), route.segments[1].medical_point_ids)
        moved = self.part(draft, PartKind.TRAFFIC_HANDLING, "segment:S2")
        self.assertIn("point:P2", moved.basis)
        self.assertNotIn("point:P1", moved.basis)
        frozen = self.part(sent, PartKind.TRAFFIC_HANDLING, "segment:S2")
        self.assertIn("point:P1", frozen.basis)

        plain_route = self.service._routes["R9"]
        self.assertEqual(("P9",), plain_route.segments[0].medical_point_ids)
        plain_notice = self.service.compose_notice("丙", "R9")
        self.assertEqual(NoticeStatus.DRAFT, plain_notice.status)
        self.assertIn("point:P9", self.part(plain_notice, PartKind.TRAFFIC_HANDLING).basis)

    def test_withdrawal_is_idempotent(self) -> None:
        self.service.withdraw_medical_point("P1")
        point = self.service.withdraw_medical_point("P1")
        self.assertFalse(point.available)
        self.assertEqual(
            1, sum(1 for e in self.service.event_log if e["event_type"] == "POINT_WITHDRAWN")
        )


class MergeTests(ServiceTestBase):
    def make_peer(self) -> LinkageService:
        peer = LinkageService(ManualClock(T0))
        peer.register_medical_point(MedicalPoint("P1", MOUNTAIN, "山顶临时医疗点", temporary=True))
        peer.register_medical_point(MedicalPoint("P2", MOUNTAIN, "县城卫生院"))
        return peer

    def forecast_events(self):
        return [
            e for e in self.service.event_log
            if e["event_type"] == "FORECAST_ISSUED" and e["aggregate_id"] == "F1"
        ]

    def test_duplicate_reports_are_merged(self) -> None:
        peer = self.make_peer()
        event = self.forecast_events()[0]
        self.assertEqual("APPLIED", peer.ingest(event))
        self.assertEqual("DUPLICATE", peer.ingest(event))
        self.assertEqual(1, len(peer._forecasts[MOUNTAIN]))

    def test_late_events_are_stale_and_do_not_downgrade(self) -> None:
        self.service.register_forecast(make_forecast(2, T0 + timedelta(minutes=10)))
        v1, v2 = self.forecast_events()[:2]
        peer = self.make_peer()
        self.assertEqual("APPLIED", peer.ingest(v2))
        self.assertEqual("STALE", peer.ingest(v1))
        versions = [item.version for item in peer._forecasts[MOUNTAIN]]
        self.assertEqual([2], versions)

    def test_withdrawal_event_merges_into_peer(self) -> None:
        self.service.withdraw_medical_point("P1")
        event = [e for e in self.service.event_log if e["event_type"] == "POINT_WITHDRAWN"][0]
        peer = self.make_peer()
        self.assertEqual("APPLIED", peer.ingest(event))
        self.assertFalse(peer._points["P1"].available)
        self.assertEqual("DUPLICATE", peer.ingest(event))

    def test_emitted_events_satisfy_exchange_contract(self) -> None:
        schema = json.loads((ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8"))
        self.service.register_forecast(make_forecast(2, T0 + timedelta(minutes=10)))
        notice = self.service.compose_notice("甲", "R1")
        self.service.approve_notice(notice.notice_id, "A1")
        self.service.send_notice(notice.notice_id)
        self.service.refresh_escalations()
        self.service.withdraw_medical_point("P1")
        self.assertGreaterEqual(len(self.service.event_log), 5)
        for event in self.service.event_log:
            self.assertEqual([], validate_event(event, schema), event["event_id"])


class ReviewTests(ServiceTestBase):
    def test_review_traces_basis_approval_recipients_and_receipts(self) -> None:
        notice = self.service.compose_notice("甲", "R1", ("G1",))
        self.service.approve_notice(notice.notice_id, "A1")
        self.service.send_notice(notice.notice_id)
        self.service.confirm_receipt(notice.notice_id, "甲", "手机")

        review = self.service.review_notice(notice.notice_id)
        self.assertEqual("forecast:F1@v1", review.forecast_ref)
        self.assertEqual("张伟", review.approved_by)
        self.assertEqual(("甲",), review.recipients)
        self.assertTrue(review.receipt_complete)
        self.assertEqual(1, len(review.receipts))
        self.assertEqual(T0, review.sent_at)
        self.assertEqual(
            {PartKind.WEATHER_FACT, PartKind.HEALTH_MEASURE, PartKind.TRAFFIC_HANDLING},
            {part.kind for part in review.parts},
        )

    def test_review_marks_missing_receipt(self) -> None:
        notice = self.service.compose_notice("乙", "R1")
        self.service.approve_notice(notice.notice_id, "A1")
        self.service.send_notice(notice.notice_id)
        review = self.service.review_notice(notice.notice_id)
        self.assertFalse(review.receipt_complete)
        self.assertEqual((), review.receipts)

    def test_approval_requires_matching_authorization(self) -> None:
        notice = self.service.compose_notice("甲", "R1")
        with self.assertRaises(NotFoundError):
            self.service.approve_notice(notice.notice_id, "A-X")
        self.service.register_authorization(NoticeAuthorization("A9", PLAIN, "李雷"))
        with self.assertRaises(StateError):
            self.service.approve_notice(notice.notice_id, "A9")
        with self.assertRaises(StateError):
            self.service.send_notice(notice.notice_id)


if __name__ == "__main__":
    unittest.main()
