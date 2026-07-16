idf_component_register(
    SRCS
        "app_main.cpp"
        "button_driver.cpp"

    INCLUDE_DIRS
        "."

    PRIV_REQUIRES
        esp_driver_gpio
)
