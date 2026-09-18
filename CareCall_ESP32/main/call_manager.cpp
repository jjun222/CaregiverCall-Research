#include "call_manager.h"

#include <cinttypes>
#include <cstdio>
#include <cstring>

#include "call_outbox.h"
#include "cJSON.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "mqtt_manager.h"
#include "nvs_flash.h"
#include "sdkconfig.h"

namespace {

constexpr char TAG[] = "CALL";
constexpr std::int64_t RETRY_US = 10 * 1000 * 1000;
constexpr std::size_t MAX_ACK_SIZE = 512;

struct DatabaseAck {
    char event_id[CallOutbox::EVENT_ID_SIZE];
};

CallOutbox g_outbox;
SemaphoreHandle_t g_mutex = nullptr;
QueueHandle_t g_acks = nullptr;
TaskHandle_t g_task = nullptr;

void delivery_task(void*)
{
    char last_attempt_id[CallOutbox::EVENT_ID_SIZE]{};
    std::int64_t retry_at = 0;

    while (true) {
        DatabaseAck ack{};

        while (xQueueReceive(g_acks, &ack, 0) == pdTRUE) {
            xSemaphoreTake(g_mutex, portMAX_DELAY);
            const esp_err_t result =
                g_outbox.acknowledge_stored(ack.event_id);
            xSemaphoreGive(g_mutex);

            if (result == ESP_OK) {
                ESP_LOGI(
                    TAG,
                    "Pi database storage confirmed: event_id=%s",
                    ack.event_id
                );
            } else if (result != ESP_ERR_NOT_FOUND) {
                ESP_LOGE(
                    TAG,
                    "Failed to persist database ACK: %s",
                    esp_err_to_name(result)
                );
            }
        }

        StoredCall call{};
        char event_id[CallOutbox::EVENT_ID_SIZE]{};

        xSemaphoreTake(g_mutex, portMAX_DELAY);
        const bool pending = g_outbox.front(&call);

        if (pending) {
            g_outbox.event_id(call, event_id);
        }

        xSemaphoreGive(g_mutex);

        TickType_t wait = portMAX_DELAY;

        if (pending && mqtt_manager_is_ready_to_publish()) {
            if (std::strcmp(last_attempt_id, event_id) != 0) {
                retry_at = 0;
            }

            const std::int64_t now = esp_timer_get_time();

            if (now >= retry_at) {
                char payload[320]{};

                const int length = std::snprintf(
                    payload,
                    sizeof(payload),
                    "{\"schema_version\":1,\"event_id\":\"%s\","
                    "\"device_id\":\"%s\",\"event_type\":\"care_call\","
                    "\"sequence\":%" PRIu32 ",\"uptime_ms\":%" PRIu64 "}",
                    event_id,
                    CONFIG_CARECALL_DEVICE_ID,
                    call.sequence,
                    call.uptime_ms
                );

                int message_id = -1;

                if (length > 0 &&
                    static_cast<std::size_t>(length) < sizeof(payload)) {
                    const esp_err_t result = mqtt_manager_publish_call(
                        payload,
                        static_cast<std::size_t>(length),
                        &message_id
                    );

                    if (result != ESP_OK) {
                        ESP_LOGW(
                            TAG,
                            "Stored call will be retried: %s",
                            esp_err_to_name(result)
                        );
                    }
                }

                std::strcpy(last_attempt_id, event_id);
                retry_at = now + RETRY_US;
            }

            const auto remaining_ms =
                (retry_at - esp_timer_get_time()) / 1000;

            wait = pdMS_TO_TICKS(
                remaining_ms > 0 ? remaining_ms + 1 : 1
            );

            if (wait == 0) {
                wait = 1;
            }
        } else {
            // 오프라인에서는 이 작업이 주기적으로 깨어나지 않습니다.
            last_attempt_id[0] = '\0';
            retry_at = 0;
        }

        ulTaskNotifyTake(pdTRUE, wait);
    }
}

}  // namespace

esp_err_t call_manager_init()
{
    if (g_task != nullptr) {
        return ESP_OK;
    }

    esp_err_t result = nvs_flash_init();

    if (result != ESP_OK) {
        ESP_LOGE(
            TAG,
            "NVS initialization failed; saved calls were not erased: %s",
            esp_err_to_name(result)
        );
        return result;
    }

    std::uint64_t seed = 0;
    esp_fill_random(&seed, sizeof(seed));

    result = g_outbox.open(CONFIG_CARECALL_DEVICE_ID, seed);

    if (result != ESP_OK) {
        return result;
    }

    g_mutex = xSemaphoreCreateMutex();
    g_acks = xQueueCreate(8, sizeof(DatabaseAck));

    if (g_mutex == nullptr || g_acks == nullptr) {
        return ESP_ERR_NO_MEM;
    }

    if (xTaskCreate(
            delivery_task,
            "call_delivery",
            6144,
            nullptr,
            4,
            &g_task
        ) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }

    ESP_LOGI(
        TAG,
        "Persistent call outbox ready: restored=%u, capacity=%u",
        static_cast<unsigned>(g_outbox.size()),
        static_cast<unsigned>(CallOutbox::CAPACITY)
    );

    return ESP_OK;
}

esp_err_t call_manager_request_call()
{
    if (g_task == nullptr) {
        return ESP_ERR_INVALID_STATE;
    }

    StoredCall accepted{};
    char event_id[CallOutbox::EVENT_ID_SIZE]{};

    xSemaphoreTake(g_mutex, portMAX_DELAY);

    const esp_err_t result = g_outbox.append(
        static_cast<std::uint64_t>(esp_timer_get_time()) / 1000,
        &accepted
    );

    if (result == ESP_OK) {
        g_outbox.event_id(accepted, event_id);
    }

    xSemaphoreGive(g_mutex);

    if (result == ESP_OK) {
        // 추후 LED 점등은 영구 저장에 성공한 이 지점과 연결합니다.
        // MQTT PUBACK이나 Pi 저장 ACK로 LED를 끄면 안 됩니다.
        ESP_LOGI(
            TAG,
            "Call durably accepted: event_id=%s",
            event_id
        );

        xTaskNotifyGive(g_task);
    } else {
        ESP_LOGE(
            TAG,
            "Call not accepted; outbox full or storage unavailable: %s",
            esp_err_to_name(result)
        );
    }

    return result;
}

void call_manager_notify_transport_changed()
{
    if (g_task != nullptr) {
        xTaskNotifyGive(g_task);
    }
}

void call_manager_receive_database_ack(
    const char* payload,
    std::size_t length
)
{
    if (g_acks == nullptr ||
        payload == nullptr ||
        length == 0 ||
        length > MAX_ACK_SIZE) {
        return;
    }

    char text[MAX_ACK_SIZE + 1]{};
    std::memcpy(text, payload, length);

    if (std::memchr(text, '\0', length) != nullptr) {
        return;
    }

    cJSON* root = cJSON_ParseWithLengthOpts(
        text,
        length + 1,
        nullptr,
        true
    );

    if (root == nullptr) {
        return;
    }

    const cJSON* id =
        cJSON_GetObjectItemCaseSensitive(root, "event_id");
    const cJSON* device =
        cJSON_GetObjectItemCaseSensitive(root, "device_id");
    const cJSON* status =
        cJSON_GetObjectItemCaseSensitive(root, "status");

    if (cJSON_IsObject(root) &&
        cJSON_GetArraySize(root) == 3 &&
        cJSON_IsString(id) &&
        cJSON_IsString(device) &&
        cJSON_IsString(status) &&
        std::strlen(id->valuestring) > 0 &&
        std::strlen(id->valuestring) < CallOutbox::EVENT_ID_SIZE &&
        std::strcmp(device->valuestring, CONFIG_CARECALL_DEVICE_ID) == 0 &&
        std::strcmp(status->valuestring, "stored") == 0) {
        DatabaseAck ack{};
        std::strcpy(ack.event_id, id->valuestring);

        if (xQueueSend(g_acks, &ack, 0) == pdTRUE) {
            call_manager_notify_transport_changed();
        }

        // ACK 처리 큐가 가득 차도 호출은 삭제하지 않습니다.
        // 같은 호출 재시도 후 다시 ACK를 받을 수 있습니다.
    }

    cJSON_Delete(root);
}
