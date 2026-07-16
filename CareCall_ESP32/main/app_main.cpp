#include "button_driver.h"
#include "wifi_manager.h"

#include "esp_err.h"
#include "esp_log.h"

namespace {

constexpr char TAG[] = "CARECALL";

void on_call_button_pressed()
{
    ESP_LOGW(TAG, "CALL REQUEST detected");

    /*
     * 현재 단계에서는 호출 버튼이 정상적으로 감지되는지만 확인합니다.
     *
     * 이후 연결:
     * call_manager_request_call()
     * -> event_id 생성
     * -> MQTT QoS 1 발행
     * -> Raspberry Pi application ACK 대기
     */
}

}  // namespace

extern "C" void app_main(void)
{
    ESP_LOGI(
        TAG,
        "CareCall ESP32-C3 firmware started"
    );

    ESP_LOGI(
        TAG,
        "Hardware profile: GPIO4 call button and Wi-Fi Station"
    );

    /*
     * 버튼을 먼저 시작합니다.
     * Wi-Fi 연결을 시도하는 동안에도 버튼 감시는 별도 Task에서 계속됩니다.
     */
    ESP_ERROR_CHECK(
        button_driver_init(on_call_button_pressed)
    );

    ESP_LOGI(
        TAG,
        "Call button system is ready"
    );

    /*
     * Wi-Fi 초기화는 연결 요청을 시작한 뒤 반환합니다.
     * 실제 연결 완료 여부는 WIFI 로그의 IPv4 address로 확인합니다.
     */
    ESP_ERROR_CHECK(
        wifi_manager_init()
    );

    ESP_LOGI(
        TAG,
        "Wi-Fi Station initialization is ready"
    );
}
