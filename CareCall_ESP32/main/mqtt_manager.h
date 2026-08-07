#pragma once

#include "esp_err.h"

/**
 * @brief MQTT 클라이언트를 초기화하고 Broker 연결을 시작합니다.
 *
 * Wi-Fi에서 IPv4 주소를 받은 뒤 호출해야 합니다.
 */
esp_err_t mqtt_manager_init();

/**
 * @brief MQTT Broker와 현재 연결되어 있는지 반환합니다.
 */
bool mqtt_manager_is_connected();
