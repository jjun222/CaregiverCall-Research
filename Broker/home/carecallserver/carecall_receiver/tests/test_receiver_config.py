from pathlib import Path
import tempfile
import unittest

from receiver_config import (
    ConfigurationError,
    MQTT_PASSWORD_ENV_NAME,
    load_receiver_config,
)


class ReceiverConfigTests(unittest.TestCase):
    def test_rejects_missing_password_environment_variable(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            ConfigurationError,
            MQTT_PASSWORD_ENV_NAME,
        ):
            load_receiver_config(environ={})

    def test_rejects_empty_password_environment_variable(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            ConfigurationError,
            MQTT_PASSWORD_ENV_NAME,
        ):
            load_receiver_config(
                environ={
                    MQTT_PASSWORD_ENV_NAME: ""
                }
            )

    def test_loads_fixed_mqtt_settings_and_database_path(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = load_receiver_config(
                environ={
                    MQTT_PASSWORD_ENV_NAME:
                        "test-secret"
                },
                project_directory=temporary_directory,
            )

            self.assertEqual(
                config.mqtt_broker_host,
                "127.0.0.1",
            )
            self.assertEqual(
                config.mqtt_broker_port,
                1883,
            )
            self.assertEqual(
                config.mqtt_username,
                "carecall_receiver",
            )
            self.assertEqual(
                config.mqtt_password,
                "test-secret",
            )
            self.assertEqual(
                config.mqtt_client_id,
                "carecall-python-receiver",
            )
            self.assertEqual(
                config.mqtt_topic_filter,
                "carecall/v1/devices/+/call",
            )
            self.assertEqual(
                config.mqtt_qos,
                1,
            )
            self.assertEqual(
                config.mqtt_keepalive_seconds,
                60,
            )
            self.assertEqual(
                config.database_path,
                (
                    Path(
                        temporary_directory
                    ).resolve()
                    / "data"
                    / "carecall_events.db"
                ),
            )

    def test_repr_does_not_expose_password(
        self,
    ) -> None:
        config = load_receiver_config(
            environ={
                MQTT_PASSWORD_ENV_NAME:
                    "do-not-print-this"
            }
        )

        rendered = repr(config)

        self.assertNotIn(
            "do-not-print-this",
            rendered,
        )
        self.assertNotIn(
            "mqtt_password",
            rendered,
        )


if __name__ == "__main__":
    unittest.main()
