idf_component_register(
    SRCS
        "app_main.cpp"
        "button_driver.cpp"
        "call_manager.cpp"
        "wifi_manager.cpp"
        "mqtt_manager.cpp"

    INCLUDE_DIRS
        "."

    PRIV_REQUIRES
        esp_driver_gpio
        esp_wifi
        esp_event
        esp_netif
        nvs_flash
        mqtt
        esp_timer
        esp_hw_support
        espressif__mdns
)
