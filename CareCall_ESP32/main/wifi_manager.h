#pragma once

#include "esp_err.h"

/**
 * @brief Wi-Fi Station 모드를 초기화하고 AP 연결을 시작합니다.
 *
 * 실제 SSID와 비밀번호는 menuconfig의
 * "CareCall Wi-Fi configuration" 메뉴에서 설정합니다.
 *
 * 연결은 비동기로 진행되며, IP를 획득하면 로그가 출력됩니다.
 * 연결이 끊어지면 자동 재연결을 시도합니다.
 */
esp_err_t wifi_manager_init();

/**
 * @brief DHCP를 통해 유효한 IPv4 주소를 획득했는지 반환합니다.
 */
bool wifi_manager_is_connected();
