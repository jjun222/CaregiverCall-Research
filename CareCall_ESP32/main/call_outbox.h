#pragma once

#include <cstddef>
#include <cstdint>

#include "esp_err.h"
#include "nvs.h"

// Pi DB 저장을 기다리는 호출입니다.
// 보호자 확인을 기다리는 상태와는 별개입니다.
struct StoredCall {
    std::uint64_t boot_id;
    std::uint64_t uptime_ms;
    std::uint32_t sequence;
    std::uint32_t reserved;
};

class CallOutbox {
public:
    static constexpr std::size_t CAPACITY = 32;
    static constexpr std::size_t EVENT_ID_SIZE = 64;

    ~CallOutbox();
    CallOutbox() = default;
    CallOutbox(const CallOutbox&) = delete;
    CallOutbox& operator=(const CallOutbox&) = delete;

    esp_err_t open(const char* device_id, std::uint64_t initial_boot_id);
    esp_err_t append(std::uint64_t uptime_ms, StoredCall* accepted);
    bool front(StoredCall* call) const;
    bool latest(StoredCall* call) const;
    esp_err_t acknowledge_stored(const char* event_id);

    std::size_t size() const { return state_.count; }
    void event_id(const StoredCall& call, char (&output)[EVENT_ID_SIZE]) const;
    const char* device_id() const { return state_.device_id; }

private:
    struct Snapshot {
        std::uint32_t version;
        std::uint32_t count;
        std::uint64_t boot_id;
        char device_id[33];
        std::uint8_t reserved[7];
        StoredCall latest;
        StoredCall calls[CAPACITY];
    };

    static_assert(sizeof(StoredCall) == 24, "StoredCall format changed");
    static_assert(sizeof(Snapshot) == 848, "Snapshot format changed");

    esp_err_t persist(const Snapshot& next);

    nvs_handle_t handle_ = 0;
    bool ready_ = false;
    std::uint32_t sequence_ = 0;
    Snapshot state_{};
};
