idf_component_register(
    SRCS
        "app_main.cpp"
        "button_driver.cpp"
        "wifi_manager.cpp"

    INCLUDE_DIRS
        "."

    PRIV_REQUIRES
        esp_driver_gpio
        esp_wifi
        esp_event
        esp_netif
        nvs_flash
)
