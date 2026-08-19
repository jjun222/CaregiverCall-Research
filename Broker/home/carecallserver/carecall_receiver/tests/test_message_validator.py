import json
import unittest

from message_validator import (
    MessageValidationError,
    validate_call_message,
)


VALID_TOPIC = "carecall/v1/devices/button01/call"

VALID_DOCUMENT = {
    "schema_version": 1,
    "event_id": "button01-5700fd46b710e354-00000001",
    "device_id": "button01",
    "event_type": "care_call",
    "sequence": 1,
    "uptime_ms": 57510,
}


def encode_document(document: object) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
    ).encode("utf-8")


class ValidateCallMessageTests(unittest.TestCase):
    def assert_validation_code(
        self,
        expected_code: str,
        topic: str = VALID_TOPIC,
        payload: bytes | bytearray | None = None,
    ) -> None:
        actual_payload = (
            encode_document(VALID_DOCUMENT)
            if payload is None
            else payload
        )

        with self.assertRaises(
            MessageValidationError
        ) as context:
            validate_call_message(
                topic,
                actual_payload,
            )

        self.assertEqual(
            context.exception.code,
            expected_code,
        )

    def test_accepts_valid_message(self) -> None:
        result = validate_call_message(
            VALID_TOPIC,
            encode_document(VALID_DOCUMENT),
        )

        self.assertEqual(result.schema_version, 1)
        self.assertEqual(
            result.event_id,
            VALID_DOCUMENT["event_id"],
        )
        self.assertEqual(result.device_id, "button01")
        self.assertEqual(result.event_type, "care_call")
        self.assertEqual(result.sequence, 1)
        self.assertEqual(result.uptime_ms, 57510)
        self.assertEqual(result.topic, VALID_TOPIC)
        self.assertEqual(
            json.loads(result.payload_json),
            VALID_DOCUMENT,
        )

    def test_canonicalizes_equivalent_json(self) -> None:
        first = validate_call_message(
            VALID_TOPIC,
            encode_document(VALID_DOCUMENT),
        )

        reversed_document = dict(
            reversed(list(VALID_DOCUMENT.items()))
        )

        second = validate_call_message(
            VALID_TOPIC,
            encode_document(reversed_document),
        )

        self.assertEqual(
            first.payload_json,
            second.payload_json,
        )

    def test_rejects_invalid_topic(self) -> None:
        invalid_topics = (
            "carecall/v1/devices/button01/ack",
            "carecall/v1/devices/+/call",
            "carecall/v1/devices/button01/call/extra",
            "",
        )

        for topic in invalid_topics:
            with self.subTest(topic=topic):
                self.assert_validation_code(
                    "invalid_topic",
                    topic=topic,
                )

    def test_rejects_invalid_utf8(self) -> None:
        self.assert_validation_code(
            "invalid_utf8",
            payload=b"\xff\xfe",
        )

    def test_rejects_invalid_json(self) -> None:
        self.assert_validation_code(
            "invalid_json",
            payload=b'{"schema_version":1',
        )

    def test_rejects_non_object_json_root(self) -> None:
        self.assert_validation_code(
            "invalid_json_root",
            payload=b"[]",
        )

    def test_rejects_duplicate_json_key(self) -> None:
        payload = (
            b'{"schema_version":1,"schema_version":1,'
            b'"event_id":"event-1","device_id":"button01",'
            b'"event_type":"care_call","sequence":1,'
            b'"uptime_ms":0}'
        )

        self.assert_validation_code(
            "duplicate_json_key",
            payload=payload,
        )

    def test_rejects_missing_required_fields(self) -> None:
        for field in VALID_DOCUMENT:
            with self.subTest(field=field):
                document = VALID_DOCUMENT.copy()
                del document[field]

                self.assert_validation_code(
                    "missing_field",
                    payload=encode_document(document),
                )

    def test_rejects_boolean_for_integer_fields(self) -> None:
        integer_fields = (
            "schema_version",
            "sequence",
            "uptime_ms",
        )

        for field in integer_fields:
            with self.subTest(field=field):
                document = VALID_DOCUMENT.copy()
                document[field] = True

                self.assert_validation_code(
                    "invalid_field_type",
                    payload=encode_document(document),
                )

    def test_rejects_wrong_integer_field_types(self) -> None:
        integer_fields = (
            "schema_version",
            "sequence",
            "uptime_ms",
        )

        for field in integer_fields:
            with self.subTest(field=field):
                document = VALID_DOCUMENT.copy()
                document[field] = "1"

                self.assert_validation_code(
                    "invalid_field_type",
                    payload=encode_document(document),
                )

    def test_rejects_empty_string_fields(self) -> None:
        string_fields = (
            "event_id",
            "device_id",
            "event_type",
        )

        for field in string_fields:
            with self.subTest(field=field):
                document = VALID_DOCUMENT.copy()
                document[field] = "   "

                self.assert_validation_code(
                    "invalid_field_value",
                    payload=encode_document(document),
                )

    def test_rejects_unsupported_schema_version(self) -> None:
        document = VALID_DOCUMENT.copy()
        document["schema_version"] = 2

        self.assert_validation_code(
            "unsupported_schema_version",
            payload=encode_document(document),
        )

    def test_rejects_invalid_event_type(self) -> None:
        document = VALID_DOCUMENT.copy()
        document["event_type"] = "other"

        self.assert_validation_code(
            "invalid_event_type",
            payload=encode_document(document),
        )

    def test_rejects_sequence_below_one(self) -> None:
        document = VALID_DOCUMENT.copy()
        document["sequence"] = 0

        self.assert_validation_code(
            "invalid_field_value",
            payload=encode_document(document),
        )

    def test_rejects_negative_uptime(self) -> None:
        document = VALID_DOCUMENT.copy()
        document["uptime_ms"] = -1

        self.assert_validation_code(
            "invalid_field_value",
            payload=encode_document(document),
        )

    def test_rejects_topic_payload_device_mismatch(self) -> None:
        document = VALID_DOCUMENT.copy()
        document["device_id"] = "button02"

        self.assert_validation_code(
            "device_id_mismatch",
            payload=encode_document(document),
        )

    def test_rejects_unregistered_device(self) -> None:
        document = VALID_DOCUMENT.copy()
        document["device_id"] = "button02"

        self.assert_validation_code(
            "unregistered_device",
            topic="carecall/v1/devices/button02/call",
            payload=encode_document(document),
        )

    def test_rejects_non_finite_json_number(self) -> None:
        payload = (
            b'{"schema_version":1,"event_id":"event-1",'
            b'"device_id":"button01",'
            b'"event_type":"care_call",'
            b'"sequence":1,"uptime_ms":NaN}'
        )

        self.assert_validation_code(
            "invalid_json",
            payload=payload,
        )


if __name__ == "__main__":
    unittest.main()
