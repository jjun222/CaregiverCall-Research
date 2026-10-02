    if (getsockname(httpd_req_to_sockfd(req), reinterpret_cast<sockaddr*>(&local), &length) != 0)
        return "LOCAL_SOCKET";
    std::uint32_t ipv4 = 0;
    if (local.ss_family == AF_INET) {
        if (length < sizeof(sockaddr_in)) return "LOCAL_LENGTH";
        ipv4 = reinterpret_cast<const sockaddr_in*>(&local)->sin_addr.s_addr;
    }
#if CONFIG_LWIP_IPV6
    else if (local.ss_family == AF_INET6) {
        if (length < sizeof(sockaddr_in6)) return "LOCAL_LENGTH";
        const auto* addr = reinterpret_cast<const sockaddr_in6*>(&local);
        const auto* bytes = reinterpret_cast<const unsigned char*>(&addr->sin6_addr);
        constexpr unsigned char mapped_prefix[12] = {0,0,0,0,0,0,0,0,0,0,0xff,0xff};
        if (std::memcmp(bytes, mapped_prefix, sizeof(mapped_prefix)) != 0)
            return "LOCAL_NOT_MAPPED_IPV4";
        std::memcpy(&ipv4, bytes + 12, sizeof(ipv4));
    }
#endif
    else return "LOCAL_FAMILY";
    if (ipv4 != inet_addr(CARECALL_WIFI_SETUP_IP)) return "LOCAL_ADDRESS";
    CarecallWifiSetupStatus state{}; carecall_wifi_setup_status(state);
    if (!state.ap_active) return "AP_INACTIVE";
    if (!header_equals(req, "Host", CARECALL_WIFI_SETUP_HOST)) return "HOST_HEADER";
    return nullptr;
}

const char* post_rejection(httpd_req_t* req)
{
    if (const char* reason = setup_rejection(req)) return reason;
    if (!header_equals(req, "Origin", "http://192.168.78.1:8080")) return "ORIGIN_HEADER";
    char supplied[33]{};
    if (httpd_req_get_hdr_value_len(req, "X-CareCall-Token") != 32 ||
        httpd_req_get_hdr_value_str(req, "X-CareCall-Token", supplied, sizeof(supplied)) != ESP_OK)
        return "SESSION_TOKEN";
    unsigned different = 0;
    for (unsigned i = 0; i < 32; ++i) different |= static_cast<unsigned char>(supplied[i] ^ session_token[i]);
    return different == 0 ? nullptr : "SESSION_TOKEN";
}

esp_err_t handle_index(httpd_req_t* req)
{
    if (const char* reason = setup_rejection(req)) return error(req, "403 Forbidden", reason);
    headers(req); httpd_resp_set_type(req, "text/html; charset=utf-8");
    return httpd_resp_send(req, PAGE, sizeof(PAGE) - 1);
}

esp_err_t state(httpd_req_t* req)
{
    if (const char* reason = setup_rejection(req)) return error(req, "403 Forbidden", reason);
    CarecallWifiSetupStatus s{}; carecall_wifi_setup_status(s);
    const bool idle = s.phase == CarecallWifiPhase::Setup || s.phase == CarecallWifiPhase::TestOk;
    const bool can_save = s.phase == CarecallWifiPhase::TestOk && s.station_ready && s.storage_ready;
    char output[512]{};
    const int size = std::snprintf(output, sizeof(output),
        "{\"version\":\"20260930-esp32-wifi-1\",\"portal_version\":\"20260930-httpaddr-2\",\"token\":\"%s\",\"notice\":\"%s\","
        "\"station_ready\":%s,\"mqtt_ready\":%s,\"profile_saved\":%s,\"storage_ready\":%s,"
        "\"can_test\":%s,\"can_save\":%s,\"can_resume\":%s}", session_token, s.notice,
        s.station_ready ? "true" : "false", mqtt_manager_is_ready_to_publish() ? "true" : "false",
        s.profile_saved ? "true" : "false", s.storage_ready ? "true" : "false",
        idle ? "true" : "false", can_save ? "true" : "false", idle ? "true" : "false");
    if (size < 0 || size >= static_cast<int>(sizeof(output))) return error(req, "500 Internal Server Error");
    headers(req); httpd_resp_set_type(req, "application/json");
    return httpd_resp_send(req, output, size);
}

esp_err_t post(httpd_req_t* req)
{
    if (const char* reason = post_rejection(req)) return error(req, "403 Forbidden", reason);
    const bool testing = std::strcmp(req->uri, "/test") == 0;
    esp_err_t result = ESP_ERR_INVALID_ARG;
    if (testing) {
        if (!req->content_len || req->content_len > 512) return error(req, "413 Content Too Large");
        char type[80]{};
        constexpr char expected[] = "application/x-www-form-urlencoded";
        if (httpd_req_get_hdr_value_str(req, "Content-Type", type, sizeof(type)) != ESP_OK ||
            std::strncmp(type, expected, sizeof(expected) - 1) != 0 ||
            (type[sizeof(expected) - 1] != 0 && type[sizeof(expected) - 1] != ';'))
            return error(req, "415 Unsupported Media Type");
        char body[513]{}; std::size_t read = 0;
        const auto started = esp_timer_get_time();
        while (read < req->content_len) {
            const int count = httpd_req_recv(req, body + read, req->content_len - read);
            if (count <= 0 || esp_timer_get_time() - started > 5000000) {
                carecall_wifi_clear(body, sizeof(body)); return error(req, "408 Request Timeout");
            }
            read += count;
        }
        CarecallWifiCredentials credentials{};
        if (carecall_wifi_form_decode(body, read, credentials)) result = carecall_wifi_request_test(credentials);
        carecall_wifi_clear(body, sizeof(body)); carecall_wifi_clear(&credentials, sizeof(credentials));
    } else {
        if (req->content_len != 0) return error(req, "400 Bad Request");
        if (std::strcmp(req->uri, "/save") == 0) result = carecall_wifi_request_save();
        if (std::strcmp(req->uri, "/resume") == 0) result = carecall_wifi_request_resume();
    }
    if (result != ESP_OK) return error(req, result == ESP_ERR_INVALID_ARG ? "400 Bad Request" : "409 Conflict");
    headers(req); httpd_resp_set_status(req, "202 Accepted"); httpd_resp_set_type(req, "application/json");
    return httpd_resp_send(req, "{\"accepted\":true}", HTTPD_RESP_USE_STRLEN);
}
}

esp_err_t carecall_wifi_portal_start()
{
    if (server) return ESP_OK;
    ESP_LOGI("WIFI_PORTAL", "PORTAL_VERSION=%s", PORTAL_VERSION);
    unsigned char random[16]{}; esp_fill_random(random, sizeof(random));
    for (unsigned i = 0; i < 16; ++i) std::snprintf(session_token + i * 2, 3, "%02x", random[i]);
    carecall_wifi_clear(random, sizeof(random));
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.server_port = 8080;
    config.stack_size = 6144;
    config.max_open_sockets = 3;
    config.max_uri_handlers = 5;
    config.lru_purge_enable = true;
    config.recv_wait_timeout = 2;
    config.send_wait_timeout = 2;
    esp_err_t result = httpd_start(&server, &config);
    if (result != ESP_OK) { server = nullptr; return result; }
    const char* paths[] = {"/", "/status", "/test", "/save", "/resume"};
    for (unsigned i = 0; i < 5; ++i) {
        httpd_uri_t uri{};
        uri.uri = paths[i]; uri.method = i < 2 ? HTTP_GET : HTTP_POST;
        uri.handler = i == 0 ? handle_index : (i == 1 ? state : post);
        result = httpd_register_uri_handler(server, &uri);
        if (result != ESP_OK) { carecall_wifi_portal_stop(); return result; }
    }
    return ESP_OK;
}
