#include "button_driver.h"

#include "esp_err.h"
#include "esp_log.h"

namespace {

constexpr char TAG[] = "CARECALL";

void on_call_button_pressed()
{
    ESP_LOGW(TAG, "CALL REQUEST detected");

    /*
     * 현재 단계에서는 호출이 정상적으로 감지됐는지만 확인합니다.
     *
     * 이후 연결:
     * call_manager_request_call()
     *     -> event_id 생성
     *     -> MQTT QoS 1 발행
     *     -> Raspberry Pi 처리 ACK 대기
     */
}

}  // namespace

extern "C" void app_main(void)
{
    ESP_LOGI(TAG, "CareCall ESP32-C3 firmware started");
    ESP_LOGI(TAG, "Hardware profile: GPIO4 call button only");

    ESP_ERROR_CHECK(
        button_driver_init(on_call_button_pressed)
    );

    ESP_LOGI(TAG, "Call button system is ready");
}
