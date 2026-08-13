#pragma once

#include "esp_err.h"

/**
 * @brief 호출 버튼 한 번에 대응하는 호출 JSON을 생성하고 MQTT 발행을 요청합니다.
 *
 * MQTT에 연결되지 않은 경우에는 호출을 생성하거나 발행하지 않습니다.
 */
esp_err_t call_manager_request_call();
