from pathlib import Path
from types import SimpleNamespace
import logging
import sqlite3
import tempfile
import unittest

import paho.mqtt.client as mqtt

from event_database import EventDatabase
from mqtt_receiver import (
    MqttProtocolAckError,
    ReceiverRuntime,
    build_client,
    on_connect,
    process_message,
)
from receiver_config import (
    MQTT_PASSWORD_ENV_NAME,
    load_receiver_config,
)


TOPIC = "carecall/v1/devices/button01/call"

VALID_PAYLOAD = (
    b'{"schema_version":1,'
    b'"event_id":"button01-test-00000001",'
    b'"device_id":"button01",'
    b'"event_type":"care_call",'
    b'"sequence":1,'
    b'"uptime_ms":57510}'
)


class FakeClient:
    def __init__(
        self,
        ack_result=mqtt.MQTT_ERR_SUCCESS,
    ) -> None:
        self.ack_result = ack_result

        self.ack_calls: list[
            tuple[int, int]
        ] = []

        self.subscribe_calls: list[
            tuple[str, int]
        ] = []

    def ack(
        self,
        message_id: int,
        qos: int,
    ):
        self.ack_calls.append(
            (message_id, qos)
        )

        return self.ack_result

    def subscribe(
        self,
        topic: str,
        qos: int,
    ):
        self.subscribe_calls.append(
            (topic, qos)
        )

        return mqtt.MQTT_ERR_SUCCESS, 77


class FailingDatabase:
    def save_call(self, call):
        del call

        raise sqlite3.OperationalError(
            "simulated database failure"
        )


def make_message(
    payload: bytes = VALID_PAYLOAD,
    *,
    topic: str = TOPIC,
    qos: int = 1,
    retain: bool = False,
    duplicate: bool = False,
    message_id: int = 10,
):
    return SimpleNamespace(
        topic=topic,
        payload=payload,
        qos=qos,
        retain=retain,
        dup=duplicate,
        mid=message_id,
    )


class MqttReceiverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_logging_disable_level = (
            logging.root.manager.disable
        )
        logging.disable(logging.CRITICAL)

        self.temporary_directory = (
            tempfile.TemporaryDirectory()
        )

        self.project_path = Path(
            self.temporary_directory.name
        )

        self.config = load_receiver_config(
            environ={
                MQTT_PASSWORD_ENV_NAME:
                    "test-secret"
            },
            project_directory=self.project_path,
        )

        self.database = EventDatabase(
            self.config.database_path
        )

        self.runtime = ReceiverRuntime(
            config=self.config,
            database=self.database,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

        logging.disable(
            self.previous_logging_disable_level
        )

    def test_valid_message_is_committed_before_ack(
        self,
    ) -> None:
        client = FakeClient()
        message = make_message()

        process_message(
            client,
            self.runtime,
            message,
        )

        stored = self.database.get_event(
            "button01-test-00000001"
        )

        self.assertIsNotNone(stored)
        self.assertEqual(
            client.ack_calls,
            [(10, 1)],
        )

    def test_duplicate_message_increments_count_and_is_acked(
        self,
    ) -> None:
        client = FakeClient()

        first = make_message(
            message_id=10
        )
        second = make_message(
            duplicate=True,
            message_id=11,
        )

        process_message(
            client,
            self.runtime,
            first,
        )
        process_message(
            client,
            self.runtime,
            second,
        )

        stored = self.database.get_event(
            "button01-test-00000001"
        )

        self.assertIsNotNone(stored)

        assert stored is not None

        self.assertEqual(
            stored.delivery_count,
            2,
        )
        self.assertEqual(
            client.ack_calls,
            [
                (10, 1),
                (11, 1),
            ],
        )

    def test_invalid_json_is_not_stored_but_is_acked(
        self,
    ) -> None:
        client = FakeClient()

        process_message(
            client,
            self.runtime,
            make_message(payload=b"{"),
        )

        self.assertEqual(
            self.database.count_events(),
            0,
        )
        self.assertEqual(
            client.ack_calls,
            [(10, 1)],
        )

    def test_retained_message_is_not_stored_but_is_acked(
        self,
    ) -> None:
        client = FakeClient()

        process_message(
            client,
            self.runtime,
            make_message(retain=True),
        )

        self.assertEqual(
            self.database.count_events(),
            0,
        )
        self.assertEqual(
            client.ack_calls,
            [(10, 1)],
        )

    def test_qos_zero_message_is_not_stored_and_needs_no_ack(
        self,
    ) -> None:
        client = FakeClient()

        process_message(
            client,
            self.runtime,
            make_message(qos=0),
        )

        self.assertEqual(
            self.database.count_events(),
            0,
        )
        self.assertEqual(
            client.ack_calls,
            [],
        )

    def test_conflicting_payload_is_not_overwritten_but_is_acked(
        self,
    ) -> None:
        client = FakeClient()

        conflicting_payload = (
            VALID_PAYLOAD.replace(
                b'"sequence":1',
                b'"sequence":2',
            )
        )

        process_message(
            client,
            self.runtime,
            make_message(message_id=10),
        )

        process_message(
            client,
            self.runtime,
            make_message(
                payload=conflicting_payload,
                message_id=11,
            ),
        )

        stored = self.database.get_event(
            "button01-test-00000001"
        )

        self.assertIsNotNone(stored)

        assert stored is not None

        self.assertEqual(
            stored.sequence,
            1,
        )
        self.assertEqual(
            stored.delivery_count,
            1,
        )
        self.assertEqual(
            client.ack_calls,
            [
                (10, 1),
                (11, 1),
            ],
        )

    def test_database_failure_leaves_message_unacknowledged(
        self,
    ) -> None:
        client = FakeClient()

        runtime = ReceiverRuntime(
            config=self.config,
            database=FailingDatabase(),
        )

        with self.assertRaises(
            sqlite3.OperationalError
        ):
            process_message(
                client,
                runtime,
                make_message(),
            )

        self.assertEqual(
            client.ack_calls,
            [],
        )

    def test_ack_failure_is_raised_after_database_commit(
        self,
    ) -> None:
        client = FakeClient(
            ack_result=mqtt.MQTT_ERR_NO_CONN
        )

        with self.assertRaises(
            MqttProtocolAckError
        ):
            process_message(
                client,
                self.runtime,
                make_message(),
            )

        self.assertEqual(
            self.database.count_events(),
            1,
        )
        self.assertEqual(
            client.ack_calls,
            [(10, 1)],
        )

    def test_on_connect_subscribes_after_success(
        self,
    ) -> None:
        client = FakeClient()

        flags = SimpleNamespace(
            session_present=False
        )

        on_connect(
            client,
            self.runtime,
            flags,
            0,
            None,
        )

        self.assertEqual(
            client.subscribe_calls,
            [
                (
                    "carecall/v1/devices/+/call",
                    1,
                )
            ],
        )

    def test_on_connect_does_not_subscribe_after_rejection(
        self,
    ) -> None:
        client = FakeClient()

        flags = SimpleNamespace(
            session_present=False
        )

        on_connect(
            client,
            self.runtime,
            flags,
            5,
            None,
        )

        self.assertEqual(
            client.subscribe_calls,
            [],
        )

    def test_build_client_uses_callback_api_v2_and_userdata(
        self,
    ) -> None:
        client = build_client(
            self.config,
            self.database,
        )

        self.assertEqual(
            client.callback_api_version,
            mqtt.CallbackAPIVersion.VERSION2,
        )
        self.assertEqual(
            client.protocol,
            mqtt.MQTTv311,
        )
        self.assertEqual(
            client.username,
            "carecall_receiver",
        )
        self.assertIsInstance(
            client.user_data_get(),
            ReceiverRuntime,
        )
        self.assertIs(
            client.on_connect,
            on_connect,
        )


if __name__ == "__main__":
    unittest.main()
