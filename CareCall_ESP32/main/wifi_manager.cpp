#include "wifi_manager.h"

#include <cstddef>
#include <cstring>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_netif_ip_addr.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "nvs_flash.h"
#include "sdkconfig.h"

namespace {

constexpr char TAG[] = "WIFI";

constexpr EventBits_t WIFI_CONNECTED_BIT = BIT0;

EventGroupHandle_t g_wifi_event_group = nullptr;

esp_event_handler_instance_t g_wifi_event_handler = nullptr;
esp_event_handler_instance_t g_ip_event_handler = nullptr;

bool g_initialized = false;
unsigned int g_reconnect_attempt = 0;

/**
 * @brief Wi-Fi 드라이버가 사용하는 NVS를 초기화합니다.
 */
esp_err_t initialize_nvs()
{
    esp_err_t result = nvs_flash_init();

    if (result == ESP_ERR_NVS_NO_FREE_PAGES ||
        result == ESP_ERR_NVS_NEW_VERSION_FOUND) {

        ESP_LOGW(
            TAG,
            "NVS requires erase before reinitialization: %s",
            esp_err_to_name(result)
        );

        result = nvs_flash_erase();
        if (result != ESP_OK) {
            ESP_LOGE(
                TAG,
                "Failed to erase NVS: %s",
                esp_err_to_name(result)
            );
            return result;
        }

        result = nvs_flash_init();
    }

    if (result != ESP_OK) {
        ESP_LOGE(
            TAG,
            "Failed to initialize NVS: %s",
            esp_err_to_name(result)
        );
    }

    return result;
}

/**
 * @brief menuconfig에 저장된 SSID와 비밀번호 길이를 검사합니다.
 */
esp_err_t validate_wifi_configuration()
{
    const std::size_t ssid_length =
        std::strlen(CONFIG_CARECALL_WIFI_SSID);

    const std::size_t password_length =
        std::strlen(CONFIG_CARECALL_WIFI_PASSWORD);

    if (ssid_length == 0) {
        ESP_LOGE(TAG, "Wi-Fi SSID is empty");
        return ESP_ERR_INVALID_ARG;
    }

    // ESP-IDF wifi_sta_config_t의 SSID 공간은 최대 32바이트입니다.
    if (ssid_length > 32) {
        ESP_LOGE(
            TAG,
            "Wi-Fi SSID is too long: %u bytes",
            static_cast<unsigned int>(ssid_length)
        );
        return ESP_ERR_INVALID_ARG;
    }

    /*
     * 이번 시험은 일반 WPA2/WPA3 Personal 비밀번호를 기준으로 합니다.
     * 비밀번호가 있으면 8~63바이트 범위로 제한합니다.
     * 비밀번호가 비어 있으면 개방형 네트워크로 처리합니다.
     */
    if (password_length != 0 &&
        (password_length < 8 || password_length > 63)) {

        ESP_LOGE(
            TAG,
            "Wi-Fi password must be empty or 8-63 bytes"
        );
        return ESP_ERR_INVALID_ARG;
    }

    return ESP_OK;
}

/**
 * @brief Wi-Fi 및 IP 이벤트 처리 함수입니다.
 */
void event_handler(
    void* argument,
    esp_event_base_t event_base,
    int32_t event_id,
    void* event_data
)
{
    (void)argument;

    if (event_base == WIFI_EVENT &&
        event_id == WIFI_EVENT_STA_START) {

        ESP_LOGI(
            TAG,
            "Wi-Fi station started; connecting to SSID=\"%s\"",
            CONFIG_CARECALL_WIFI_SSID
        );

        const esp_err_t result = esp_wifi_connect();

        if (result != ESP_OK) {
            ESP_LOGE(
                TAG,
                "esp_wifi_connect() failed: %s",
                esp_err_to_name(result)
            );
        }

        return;
    }

    if (event_base == WIFI_EVENT &&
        event_id == WIFI_EVENT_STA_DISCONNECTED) {

        xEventGroupClearBits(
            g_wifi_event_group,
            WIFI_CONNECTED_BIT
        );

        const auto* disconnected_event =
            static_cast<wifi_event_sta_disconnected_t*>(event_data);

        ++g_reconnect_attempt;

        ESP_LOGW(
            TAG,
            "Disconnected from access point: reason=%u, reconnect_attempt=%u",
            static_cast<unsigned int>(disconnected_event->reason),
            g_reconnect_attempt
        );

        const esp_err_t result = esp_wifi_connect();

        if (result != ESP_OK) {
            ESP_LOGE(
                TAG,
                "Wi-Fi reconnect request failed: %s",
                esp_err_to_name(result)
            );
        }

        return;
    }

    if (event_base == IP_EVENT &&
        event_id == IP_EVENT_STA_GOT_IP) {

        const auto* got_ip_event =
            static_cast<ip_event_got_ip_t*>(event_data);

        g_reconnect_attempt = 0;

        xEventGroupSetBits(
            g_wifi_event_group,
            WIFI_CONNECTED_BIT
        );

        ESP_LOGI(TAG, "Connected to Wi-Fi access point");

        ESP_LOGI(
            TAG,
            "IPv4 address: " IPSTR,
            IP2STR(&got_ip_event->ip_info.ip)
        );

        ESP_LOGI(
            TAG,
            "Gateway: " IPSTR,
            IP2STR(&got_ip_event->ip_info.gw)
        );

        ESP_LOGI(
            TAG,
            "Netmask: " IPSTR,
            IP2STR(&got_ip_event->ip_info.netmask)
        );
    }
}

}  // namespace

esp_err_t wifi_manager_init()
{
    if (g_initialized) {
        ESP_LOGW(TAG, "Wi-Fi manager is already initialized");
        return ESP_OK;
    }

    esp_err_t result = validate_wifi_configuration();
    if (result != ESP_OK) {
        return result;
    }

    result = initialize_nvs();
    if (result != ESP_OK) {
        return result;
    }

    g_wifi_event_group = xEventGroupCreate();

    if (g_wifi_event_group == nullptr) {
        ESP_LOGE(TAG, "Failed to create Wi-Fi event group");
        return ESP_ERR_NO_MEM;
    }

    result = esp_netif_init();
    if (result != ESP_OK) {
        ESP_LOGE(
            TAG,
            "esp_netif_init() failed: %s",
            esp_err_to_name(result)
        );
        return result;
    }

    result = esp_event_loop_create_default();

    /*
     * 다른 기능이 먼저 기본 이벤트 루프를 생성한 경우에는
     * ESP_ERR_INVALID_STATE가 반환될 수 있습니다.
     */
    if (result != ESP_OK &&
        result != ESP_ERR_INVALID_STATE) {

        ESP_LOGE(
            TAG,
            "Default event loop creation failed: %s",
            esp_err_to_name(result)
        );
        return result;
    }

    esp_netif_t* station_netif =
        esp_netif_create_default_wifi_sta();

    if (station_netif == nullptr) {
        ESP_LOGE(
            TAG,
            "Failed to create default Wi-Fi station interface"
        );
        return ESP_FAIL;
    }

    wifi_init_config_t wifi_init_config =
        WIFI_INIT_CONFIG_DEFAULT();

    result = esp_wifi_init(&wifi_init_config);
    if (result != ESP_OK) {
        ESP_LOGE(
            TAG,
            "esp_wifi_init() failed: %s",
            esp_err_to_name(result)
        );
        return result;
    }

    result = esp_event_handler_instance_register(
        WIFI_EVENT,
        ESP_EVENT_ANY_ID,
        &event_handler,
        nullptr,
        &g_wifi_event_handler
    );

    if (result != ESP_OK) {
        ESP_LOGE(
            TAG,
            "Wi-Fi event handler registration failed: %s",
            esp_err_to_name(result)
        );
        return result;
    }

    result = esp_event_handler_instance_register(
        IP_EVENT,
        IP_EVENT_STA_GOT_IP,
        &event_handler,
        nullptr,
        &g_ip_event_handler
    );

    if (result != ESP_OK) {
        ESP_LOGE(
            TAG,
            "IP event handler registration failed: %s",
            esp_err_to_name(result)
        );
        return result;
    }

    wifi_config_t wifi_config{};

    const std::size_t ssid_length =
        std::strlen(CONFIG_CARECALL_WIFI_SSID);

    const std::size_t password_length =
        std::strlen(CONFIG_CARECALL_WIFI_PASSWORD);

    std::memcpy(
        wifi_config.sta.ssid,
        CONFIG_CARECALL_WIFI_SSID,
        ssid_length
    );

    if (password_length > 0) {
        std::memcpy(
            wifi_config.sta.password,
            CONFIG_CARECALL_WIFI_PASSWORD,
            password_length
        );

        /*
         * WPA2 이상의 Personal 네트워크만 허용합니다.
         * WPA2/WPA3 혼합 모드도 이 기준으로 연결할 수 있습니다.
         */
        wifi_config.sta.threshold.authmode =
            WIFI_AUTH_WPA2_PSK;
    } else {
        wifi_config.sta.threshold.authmode =
            WIFI_AUTH_OPEN;
    }

    /*
     * Protected Management Frames를 지원하되,
     * 이번 시험에서는 필수로 강제하지 않습니다.
     */
    wifi_config.sta.pmf_cfg.capable = true;
    wifi_config.sta.pmf_cfg.required = false;

    result = esp_wifi_set_mode(WIFI_MODE_STA);
    if (result != ESP_OK) {
        ESP_LOGE(
            TAG,
            "Failed to set Wi-Fi Station mode: %s",
            esp_err_to_name(result)
        );
        return result;
    }

    result = esp_wifi_set_config(
        WIFI_IF_STA,
        &wifi_config
    );

    if (result != ESP_OK) {
        ESP_LOGE(
            TAG,
            "Failed to apply Wi-Fi configuration: %s",
            esp_err_to_name(result)
        );
        return result;
    }

    ESP_LOGI(
        TAG,
        "Wi-Fi configuration applied: mode=STA, SSID=\"%s\"",
        CONFIG_CARECALL_WIFI_SSID
    );

    result = esp_wifi_start();
    if (result != ESP_OK) {
        ESP_LOGE(
            TAG,
            "esp_wifi_start() failed: %s",
            esp_err_to_name(result)
        );
        return result;
    }

    g_initialized = true;

    ESP_LOGI(
        TAG,
        "Wi-Fi manager initialized; waiting for IPv4 address"
    );

    return ESP_OK;
}

bool wifi_manager_is_connected()
{
    if (g_wifi_event_group == nullptr) {
        return false;
    }

    const EventBits_t bits =
        xEventGroupGetBits(g_wifi_event_group);

    return (bits & WIFI_CONNECTED_BIT) != 0;
}
