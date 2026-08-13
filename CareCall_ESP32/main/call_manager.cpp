#include "call_manager.h"

#include <atomic>
#include <cinttypes>
#include <cstddef>
#include <cstdint>
#include <cstdio>

#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "mqtt_manager.h"
#include "sdkconfig.h"

namespace {

constexpr char TAG[] = "CALL";

constexpr std::size_t EVENT_ID_BUFFER_SIZE = 96;
constexpr std::size_t JSON_BUFFER_SIZE = 320;

std::atomic_uint32_t g_sequence{0};
uint64_t g_boot_identifier = 0;

uint32_t next_sequence()
{
    uint32_t sequence =
        g_sequence.fetch_add(1) + 1;

    // 32비트 순번이 한 바퀴 돌아 0이 된 경우 0을 건너뜁니다.
    if (sequence == 0) {
        sequence = g_sequence.fetch_add(1) + 1;
    }

    return sequence;
}

uint64_t get_boot_identifier()
{
    if (g_boot_identifier == 0) {
        do {
            esp_fill_random(
                &g_boot_identifier,
                sizeof(g_boot_identifier)
            );
        } while (g_boot_identifier == 0);
    }

    return g_boot_identifier;
}

bool format_was_successful(
    const int written,
    const std::size_t buffer_size
)
{
    return written >= 0 &&
           static_cast<std::size_t>(written) < buffer_size;
}

}  // namespace

esp_err_t call_manager_request_call()
{
    if (!mqtt_manager_is_connected()) {
        ESP_LOGW(
            TAG,
            "Call request rejected: MQTT is not connected"
        );
        return ESP_ERR_INVALID_STATE;
    }

    const uint32_t sequence = next_sequence();
    const uint64_t boot_identifier = get_boot_identifier();

    const uint64_t uptime_ms =
        static_cast<uint64_t>(esp_timer_get_time()) / 1000ULL;

    char event_id[EVENT_ID_BUFFER_SIZE]{};

    const int event_id_length = std::snprintf(
        event_id,
        sizeof(event_id),
        "%s-%016" PRIx64 "-%08" PRIu32,
        CONFIG_CARECALL_DEVICE_ID,
        boot_identifier,
        sequence
    );

    if (!format_was_successful(
            event_id_length,
            sizeof(event_id))) {

        ESP_LOGE(TAG, "Failed to create event_id");
        return ESP_ERR_INVALID_SIZE;
    }

    char json_payload[JSON_BUFFER_SIZE]{};

    const int payload_length = std::snprintf(
        json_payload,
        sizeof(json_payload),
        "{\"schema_version\":1,"
        "\"event_id\":\"%s\","
        "\"device_id\":\"%s\","
        "\"event_type\":\"care_call\","
        "\"sequence\":%" PRIu32 ","
        "\"uptime_ms\":%" PRIu64 "}",
        event_id,
        CONFIG_CARECALL_DEVICE_ID,
        sequence,
        uptime_ms
    );

    if (!format_was_successful(
            payload_length,
            sizeof(json_payload))) {

        ESP_LOGE(TAG, "Failed to create call JSON");
        return ESP_ERR_INVALID_SIZE;
    }

    int mqtt_message_id = -1;

    const esp_err_t publish_result =
        mqtt_manager_publish_call(
            json_payload,
            static_cast<std::size_t>(payload_length),
            &mqtt_message_id
        );

    if (publish_result != ESP_OK) {
        ESP_LOGE(
            TAG,
            "Call publish request failed: event_id=%s, error=%s",
            event_id,
            esp_err_to_name(publish_result)
        );
        return publish_result;
    }

    ESP_LOGI(
        TAG,
        "Call request queued: event_id=%s, sequence=%" PRIu32
        ", uptime_ms=%" PRIu64 ", mqtt_message_id=%d",
        event_id,
        sequence,
        uptime_ms,
        mqtt_message_id
    );

    return ESP_OK;
}
