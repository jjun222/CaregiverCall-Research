from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from event_database import (
    EventDatabase,
    SaveOutcome,
)
from message_validator import validate_call_message


class ValidationDatabaseIntegrationTests(
    unittest.TestCase
):
    def test_real_esp32_payload_is_validated_and_deduplicated(
        self,
    ) -> None:
        topic = (
            "carecall/v1/devices/button01/call"
        )

        payload = (
            b'{"schema_version":1,'
            b'"event_id":'
            b'"button01-5700fd46b710e354-00000001",'
            b'"device_id":"button01",'
            b'"event_type":"care_call",'
            b'"sequence":1,'
            b'"uptime_ms":57510}'
        )

        call = validate_call_message(
            topic,
            payload,
        )

        first_time = datetime(
            2026,
            8,
            19,
            5,
            0,
            tzinfo=timezone.utc,
        )
        second_time = (
            first_time + timedelta(seconds=1)
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = (
                Path(temporary_directory)
                / "carecall_events.db"
            )

            database = EventDatabase(
                database_path
            )

            first_result = database.save_call(
                call,
                received_at=first_time,
            )
            second_result = database.save_call(
                call,
                received_at=second_time,
            )
            stored = database.get_event(
                call.event_id
            )

        self.assertEqual(
            first_result.outcome,
            SaveOutcome.STORED,
        )
        self.assertEqual(
            second_result.outcome,
            SaveOutcome.DUPLICATE,
        )
        self.assertEqual(
            second_result.delivery_count,
            2,
        )
        self.assertIsNotNone(stored)

        assert stored is not None

        self.assertEqual(
            stored.event_id,
            call.event_id,
        )
        self.assertEqual(
            stored.device_id,
            "button01",
        )
        self.assertEqual(
            stored.sequence,
            1,
        )
        self.assertEqual(
            stored.uptime_ms,
            57510,
        )
        self.assertEqual(
            stored.delivery_count,
            2,
        )
        self.assertEqual(
            stored.received_at,
            "2026-08-19T05:00:00.000+00:00",
        )
        self.assertEqual(
            stored.last_received_at,
            "2026-08-19T05:00:01.000+00:00",
        )


if __name__ == "__main__":
    unittest.main()
