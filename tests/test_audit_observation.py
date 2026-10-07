import unittest
from unittest.mock import Mock, patch

from modport.operations import MigrationOperations, SUBSCRIPTION


class AuditObservationTests(unittest.TestCase):
    def test_streams_single_event_reads_through_original_watermark(self):
        sdk = Mock()
        sdk.observe.return_value = {
            "snapshot": {"state": "running"}, "cursor": 3,
            "event_high_watermark": 6, "events": [{"sequence": 4, "payload": "large"}],
        }
        sdk.read_events.side_effect = lambda run_id, after, limit: [
            {"sequence": after + 1, "payload": "large"}]
        seen = []

        def persist(root, events):
            for event in events:
                seen.append(event["sequence"])

        with patch("modport.operations.record_sdk_events", side_effect=persist):
            result = MigrationOperations._audited_observation(sdk, {"run_id": "r", "run_dir": "."})
        self.assertEqual([4, 5, 6], seen)
        self.assertEqual(6, result["audit_advance_to"])
        self.assertNotIn("events", result)
        sdk.observe.assert_called_once_with("r", subscription=SUBSCRIPTION, limit=1)
        self.assertTrue(all(call.kwargs["limit"] == 1 for call in sdk.read_events.call_args_list))

    def test_failed_write_never_reads_later_events_or_acknowledges(self):
        sdk = Mock()
        sdk.observe.return_value = {
            "snapshot": {"state": "running"}, "cursor": 0,
            "event_high_watermark": 3, "events": [{"sequence": 1}],
        }

        def fail(root, events):
            next(events)
            raise OSError("audit disk full")

        with patch("modport.operations.record_sdk_events", side_effect=fail):
            with self.assertRaises(OSError):
                MigrationOperations._audited_observation(sdk, {"run_id": "r", "run_dir": "."})
        sdk.read_events.assert_not_called()
        sdk.acknowledge_events.assert_not_called()

    def test_terminal_drain_does_not_read_past_fixed_target(self):
        sdk = Mock()
        sdk.observe.return_value = {
            "snapshot": {"state": "failed"}, "cursor": 7,
            "event_high_watermark": 10, "events": [{"sequence": 8}],
        }
        with patch("modport.operations.record_sdk_events", side_effect=lambda root, events: list(events)):
            result = MigrationOperations._audited_observation(
                sdk, {"run_id": "r", "run_dir": "."}, through=8)
        self.assertEqual(8, result["audit_advance_to"])
        sdk.read_events.assert_not_called()


if __name__ == "__main__":
    unittest.main()
