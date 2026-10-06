from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from holiday_health_weather.contracts import validate_event


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads((ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8"))
        cls.sample = json.loads((ROOT / "data" / "sample.json").read_text(encoding="utf-8"))

    def test_sample_is_valid(self) -> None:
        self.assertEqual([], validate_event(self.sample, self.schema))

    def test_missing_fields_are_reported_in_stable_order(self) -> None:
        issues = validate_event({}, self.schema)
        self.assertEqual(sorted(issue.field for issue in issues), [issue.field for issue in issues])
        self.assertIn("event_id", {issue.field for issue in issues})

    def test_naive_time_and_zero_version_are_rejected(self) -> None:
        payload = dict(self.sample, occurred_at="2026-09-24T12:00:00", version=0)
        codes = {(issue.field, issue.code) for issue in validate_event(payload, self.schema)}
        self.assertIn(("occurred_at", "timezone_required"), codes)
        self.assertIn(("version", "positive_integer"), codes)

    def test_unknown_event_type_is_rejected(self) -> None:
        payload = dict(self.sample, event_type="UNKNOWN")
        issues = validate_event(payload, self.schema)
        self.assertEqual([("event_type", "unsupported_value")], [(item.field, item.code) for item in issues])

    def test_schema_enums_match_python_registry(self) -> None:
        from holiday_health_weather import AGGREGATE_TYPES, EVENT_TYPES

        schema_events = set(self.schema["properties"]["event_type"]["enum"])
        schema_aggregates = set(self.schema["properties"]["aggregate_type"]["enum"])
        self.assertEqual(EVENT_TYPES, schema_events)
        self.assertEqual(AGGREGATE_TYPES, schema_aggregates)

    def test_emitted_service_events_satisfy_contract(self) -> None:
        import json

        from support import AREA, build_service, cst, rain, register_trip

        svc = build_service(cst(2026, 10, 2, 8))
        svc.register_forecast(
            "fc-1", AREA, cst(2026, 10, 2, 0), cst(2026, 10, 3, 0),
            [rain(12, cst(2026, 10, 2, 8))],
        )
        register_trip(svc)
        svc.draft_for_trip("trip-wang")
        svc.approve_notice("notice-trip-wang-seg-center-lingfeng", "小李")
        for event in svc.store.all_events():
            envelope = json.loads(json.dumps(event.to_dict(), ensure_ascii=False))
            self.assertEqual([], validate_event(envelope, self.schema))


if __name__ == "__main__":
    unittest.main()
