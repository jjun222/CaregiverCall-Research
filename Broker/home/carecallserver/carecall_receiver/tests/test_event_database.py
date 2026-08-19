from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from event_database import (
    EventConflictError,
    EventDatabase,
    SaveOutcome,
)
from message_validator import validate_call_message


TOPIC = "carecall/v1/devices/button01/call"


def make_call(
    event_id: str = "button01-test-00000001",
    sequence: int = 1,
):
    payload = (
        "{"
        '"schema_version":1,'
        f'"event_id":"{event_id}",'
        '"device_id":"button01",'
        '"event_type":"care_call",'
        f'"sequence":{sequence},'
        '"uptime_ms":57510'
        "}"
    ).encode("utf-8")

    return validate_call_message(
        TOPIC,
        payload,
    )


class EventDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = (
            tempfile.TemporaryDirectory()
        )

        self.database_path = (
            Path(self.temporary_directory.name)
            / "carecall_events.db"
        )

        self.database = EventDatabase(
            self.database_path
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_initializes_database_file_and_schema(
        self,
    ) -> None:
        self.assertTrue(
            self.database_path.is_file()
        )

        with sqlite3.connect(
            self.database_path
        ) as connection:
            columns = connection.execute(
                "PRAGMA table_info(call_events)"
            ).fetchall()

            journal_mode = connection.execute(
                "PRAGMA journal_mode"
            ).fetchone()

        self.assertEqual(
            [column[1] for column in columns],
            [
                "event_id",
                "schema_version",
                "device_id",
                "event_type",
                "sequence",
                "uptime_ms",
                "topic",
                "payload_json",
                "received_at",
                "last_received_at",
                "delivery_count",
            ],
        )

        self.assertEqual(
            journal_mode[0].lower(),
            "wal",
        )

    def test_saves_new_event(self) -> None:
        call = make_call()

        received_at = datetime(
            2026,
            8,
            19,
            5,
            0,
            tzinfo=timezone.utc,
        )

        result = self.database.save_call(
            call,
            received_at=received_at,
        )

        stored = self.database.get_event(
            call.event_id
        )

        self.assertEqual(
            result.outcome,
            SaveOutcome.STORED,
        )
        self.assertEqual(
            result.delivery_count,
            1,
        )
        self.assertIsNotNone(stored)

        assert stored is not None

        self.assertEqual(
            stored.event_id,
            call.event_id,
        )
        self.assertEqual(
            stored.schema_version,
            1,
        )
        self.assertEqual(
            stored.device_id,
            "button01",
        )
        self.assertEqual(
            stored.event_type,
            "care_call",
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
            stored.topic,
            TOPIC,
        )
        self.assertEqual(
            stored.payload_json,
            call.payload_json,
        )
        self.assertEqual(
            stored.received_at,
            "2026-08-19T05:00:00.000+00:00",
        )
        self.assertEqual(
            stored.last_received_at,
            "2026-08-19T05:00:00.000+00:00",
        )
        self.assertEqual(
            stored.delivery_count,
            1,
        )
        self.assertEqual(
            self.database.count_events(),
            1,
        )

    def test_same_payload_updates_duplicate_metadata(
        self,
    ) -> None:
        call = make_call()

        first_time = datetime(
            2026,
            8,
            19,
            5,
            0,
            tzinfo=timezone.utc,
        )
        second_time = datetime(
            2026,
            8,
            19,
            5,
            1,
            tzinfo=timezone.utc,
        )

        self.database.save_call(
            call,
            received_at=first_time,
        )

        result = self.database.save_call(
            call,
            received_at=second_time,
        )

        stored = self.database.get_event(
            call.event_id
        )

        self.assertEqual(
            result.outcome,
            SaveOutcome.DUPLICATE,
        )
        self.assertEqual(
            result.delivery_count,
            2,
        )
        self.assertIsNotNone(stored)

        assert stored is not None

        self.assertEqual(
            stored.received_at,
            "2026-08-19T05:00:00.000+00:00",
        )
        self.assertEqual(
            stored.last_received_at,
            "2026-08-19T05:01:00.000+00:00",
        )
        self.assertEqual(
            stored.delivery_count,
            2,
        )
        self.assertEqual(
            self.database.count_events(),
            1,
        )

    def test_conflicting_payload_is_rejected_without_overwrite(
        self,
    ) -> None:
        original = make_call(sequence=1)
        conflicting = make_call(sequence=2)

        received_at = datetime(
            2026,
            8,
            19,
            5,
            0,
            tzinfo=timezone.utc,
        )

        self.database.save_call(
            original,
            received_at=received_at,
        )

        with self.assertRaises(
            EventConflictError
        ):
            self.database.save_call(
                conflicting,
                received_at=(
                    received_at
                    + timedelta(minutes=1)
                ),
            )

        stored = self.database.get_event(
            original.event_id
        )

        self.assertIsNotNone(stored)

        assert stored is not None

        self.assertEqual(
            stored.sequence,
            1,
        )
        self.assertEqual(
            stored.payload_json,
            original.payload_json,
        )
        self.assertEqual(
            stored.delivery_count,
            1,
        )
        self.assertEqual(
            stored.last_received_at,
            "2026-08-19T05:00:00.000+00:00",
        )
        self.assertEqual(
            self.database.count_events(),
            1,
        )

    def test_duplicate_is_detected_after_database_reopen(
        self,
    ) -> None:
        call = make_call()

        first_time = datetime(
            2026,
            8,
            19,
            5,
            0,
            tzinfo=timezone.utc,
        )
        second_time = datetime(
            2026,
            8,
            19,
            5,
            1,
            tzinfo=timezone.utc,
        )

        self.database.save_call(
            call,
            received_at=first_time,
        )

        reopened_database = EventDatabase(
            self.database_path
        )

        result = reopened_database.save_call(
            call,
            received_at=second_time,
        )

        stored = reopened_database.get_event(
            call.event_id
        )

        self.assertEqual(
            result.outcome,
            SaveOutcome.DUPLICATE,
        )
        self.assertIsNotNone(stored)

        assert stored is not None

        self.assertEqual(
            stored.delivery_count,
            2,
        )
        self.assertEqual(
            reopened_database.count_events(),
            1,
        )

    def test_different_event_ids_create_separate_rows(
        self,
    ) -> None:
        first = make_call(
            event_id="button01-test-00000001",
            sequence=1,
        )
        second = make_call(
            event_id="button01-test-00000002",
            sequence=2,
        )

        self.database.save_call(first)
        self.database.save_call(second)

        self.assertEqual(
            self.database.count_events(),
            2,
        )

    def test_get_event_returns_none_for_unknown_event(
        self,
    ) -> None:
        self.assertIsNone(
            self.database.get_event(
                "unknown-event"
            )
        )

    def test_rejects_naive_received_at(self) -> None:
        call = make_call()

        with self.assertRaisesRegex(
            ValueError,
            "timezone",
        ):
            self.database.save_call(
                call,
                received_at=datetime(
                    2026,
                    8,
                    19,
                    5,
                    0,
                ),
            )

        self.assertEqual(
            self.database.count_events(),
            0,
        )

    def test_converts_received_at_to_utc(
        self,
    ) -> None:
        call = make_call()

        korea_timezone = timezone(
            timedelta(hours=9)
        )

        korea_time = datetime(
            2026,
            8,
            19,
            14,
            0,
            tzinfo=korea_timezone,
        )

        self.database.save_call(
            call,
            received_at=korea_time,
        )

        stored = self.database.get_event(
            call.event_id
        )

        self.assertIsNotNone(stored)

        assert stored is not None

        self.assertEqual(
            stored.received_at,
            "2026-08-19T05:00:00.000+00:00",
        )


if __name__ == "__main__":
    unittest.main()
