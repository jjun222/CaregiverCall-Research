#include "mqtt_manager.h"

#include <atomic>
#include <cstring>

#include "esp_event.h"
#include "esp_log.h"
#include "mqtt_client.h"
#include "sdkconfig.h"

namespace {

constexpr char TAG[] = "MQTT";

constexpr char ACK_TOPIC[] =
    "carecall/v1/devices/button01/ack";

constexpr int ACK_QOS = 1;

esp_mqtt_client_handle_t g_mqtt_client = nullptr;

std::atomic_bool g_connected{false};

bool g_initialized = false;

esp_err_t validate_mqtt_configuration()
{
    if (std::strlen(CONFIG_CARECALL_MQTT_BROKER_HOST) == 0) {
        ESP_LOGE(TAG, "MQTT Broker address is empty");
        return ESP_ERR_INVALID_ARG;
    }

    if (CONFIG_CARECALL_MQTT_BROKER_PORT < 1 ||
        CONFIG_CARECALL_MQTT_BROKER_PORT > 65535) {

        ESP_LOGE(TAG, "MQTT Broker port is invalid");
        return ESP_ERR_INVALID_ARG;
    }

    if (std::strlen(CONFIG_CARECALL_MQTT_USERNAME) == 0) {
        ESP_LOGE(TAG, "MQTT username is empty");
        return ESP_ERR_INVALID_ARG;
    }

    if (std::strlen(CONFIG_CARECALL_MQTT_PASSWORD) == 0) {
        ESP_LOGE(TAG, "MQTT password is empty");
        return ESP_ERR_INVALID_ARG;
    }

    if (std::strlen(CONFIG_CARECALL_MQTT_CLIENT_ID) == 0) {
        ESP_LOGE(TAG, "MQTT Client ID is empty");
        return ESP_ERR_INVALID_ARG;
    }

    return ESP_OK;
}

bool is_ack_topic(const esp_mqtt_event_handle_t event)
{
    if (event == nullptr || event->topic == nullptr) {
        return false;
    }

    const int expected_length =
        static_cast<int>(std::strlen(ACK_TOPIC));

    return event->topic_len == expected_length &&
           std::memcmp(
               event->topic,
               ACK_TOPIC,
               expected_length
           ) == 0;
}

void mqtt_event_handler(
    void* handler_argument,
    esp_event_base_t event_base,
    int32_t event_id,
    void* event_data
)
{
    (void)handler_argument;
    (void)event_base;

    const auto event =
        static_cast<esp_mqtt_event_handle_t>(event_data);

    if (event == nullptr) {
        ESP_LOGE(TAG, "MQTT event data is null");
        return;
    }

    switch (static_cast<esp_mqtt_event_id_t>(event_id)) {

        case MQTT_EVENT_CONNECTED: {
            g_connected.store(true);

            ESP_LOGI(
                TAG,
                "Connected to MQTT Broker"
            );

            const int message_id =
                esp_mqtt_client_subscribe(
                    event->client,
                    ACK_TOPIC,
                    ACK_QOS
                );

            if (message_id < 0) {
                ESP_LOGE(
                    TAG,
                    "ACK subscription request failed: result=%d",
                    message_id
                );
            } else {
                ESP_LOGI(
                    TAG,
                    "ACK subscription requested: "
                    "topic=%s, qos=%d, message_id=%d",
                    ACK_TOPIC,
                    ACK_QOS,
                    message_id
                );
            }

            break;
        }

        case MQTT_EVENT_DISCONNECTED:
            g_connected.store(false);

            ESP_LOGW(
                TAG,
                "Disconnected from MQTT Broker; "
                "automatic reconnect will be attempted"
            );
            break;

        case MQTT_EVENT_SUBSCRIBED:
            ESP_LOGI(
                TAG,
                "MQTT SUBACK received: message_id=%d",
                event->msg_id
            );
            break;

        case MQTT_EVENT_DATA:
            if (is_ack_topic(event)) {
                ESP_LOGI(
                    TAG,
                    "Application ACK received: "
                    "topic=%.*s, payload=%.*s",
                    event->topic_len,
                    event->topic,
                    event->data_len,
                    event->data != nullptr ? event->data : ""
                );
            } else {
                ESP_LOGW(
                    TAG,
                    "Message received from unexpected topic: %.*s",
                    event->topic_len,
                    event->topic != nullptr ? event->topic : ""
                );
            }
            break;

        case MQTT_EVENT_ERROR:
            g_connected.store(false);

            if (event->error_handle == nullptr) {
                ESP_LOGE(TAG, "MQTT error occurred");
                break;
            }

            if (event->error_handle->error_type ==
                MQTT_ERROR_TYPE_CONNECTION_REFUSED) {

                ESP_LOGE(
                    TAG,
                    "Broker refused MQTT connection: "
                    "return_code=%d",
                    static_cast<int>(
                        event->error_handle->connect_return_code
                    )
                );
            } else if (
                event->error_handle->error_type ==
                MQTT_ERROR_TYPE_TCP_TRANSPORT) {

                ESP_LOGE(
                    TAG,
                    "MQTT TCP transport error: "
                    "esp_error=0x%x, socket_errno=%d",
                    static_cast<unsigned int>(
                        event->error_handle->esp_tls_last_esp_err
                    ),
                    event->error_handle->esp_transport_sock_errno
                );
            } else {
                ESP_LOGE(
                    TAG,
                    "MQTT error occurred: error_type=%d",
                    static_cast<int>(
                        event->error_handle->error_type
                    )
                );
            }

            break;

        default:
            break;
    }
}

}  // namespace

esp_err_t mqtt_manager_init()
{
    if (g_initialized) {
        ESP_LOGW(TAG, "MQTT manager is already initialized");
        return ESP_OK;
    }

    const esp_err_t validation_result =
        validate_mqtt_configuration();

    if (validation_result != ESP_OK) {
        return validation_result;
    }

    esp_mqtt_client_config_t mqtt_config{};

    mqtt_config.broker.address.hostname =
        CONFIG_CARECALL_MQTT_BROKER_HOST;

    mqtt_config.broker.address.port =
        CONFIG_CARECALL_MQTT_BROKER_PORT;

    mqtt_config.broker.address.transport =
        MQTT_TRANSPORT_OVER_TCP;

    mqtt_config.credentials.username =
        CONFIG_CARECALL_MQTT_USERNAME;

    mqtt_config.credentials.client_id =
        CONFIG_CARECALL_MQTT_CLIENT_ID;

    mqtt_config.credentials.authentication.password =
        CONFIG_CARECALL_MQTT_PASSWORD;

    mqtt_config.session.protocol_ver =
        MQTT_PROTOCOL_V_3_1_1;

    mqtt_config.session.keepalive = 60;

    mqtt_config.network.reconnect_timeout_ms = 5000;

    mqtt_config.network.disable_auto_reconnect = false;

    g_mqtt_client =
        esp_mqtt_client_init(&mqtt_config);

    if (g_mqtt_client == nullptr) {
        ESP_LOGE(TAG, "Failed to create MQTT client");
        return ESP_ERR_NO_MEM;
    }

    esp_err_t result =
        esp_mqtt_client_register_event(
            g_mqtt_client,
            MQTT_EVENT_ANY,
            mqtt_event_handler,
            nullptr
        );

    if (result != ESP_OK) {
        ESP_LOGE(
            TAG,
            "MQTT event registration failed: %s",
            esp_err_to_name(result)
        );

        esp_mqtt_client_destroy(g_mqtt_client);
        g_mqtt_client = nullptr;

        return result;
    }

    result = esp_mqtt_client_start(g_mqtt_client);

    if (result != ESP_OK) {
        ESP_LOGE(
            TAG,
            "MQTT client start failed: %s",
            esp_err_to_name(result)
        );

        esp_mqtt_client_destroy(g_mqtt_client);
        g_mqtt_client = nullptr;

        return result;
    }

    g_initialized = true;

    ESP_LOGI(
        TAG,
        "MQTT client started: broker=%s, port=%d, "
        "client_id=%s",
        CONFIG_CARECALL_MQTT_BROKER_HOST,
        CONFIG_CARECALL_MQTT_BROKER_PORT,
        CONFIG_CARECALL_MQTT_CLIENT_ID
    );

    return ESP_OK;
}

bool mqtt_manager_is_connected()
{
    return g_connected.load();
}
