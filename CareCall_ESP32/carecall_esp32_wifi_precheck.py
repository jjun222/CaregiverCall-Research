#!/usr/bin/env python3
"""CareCall ESP32 Wi-Fi 사전 점검 — Python 표준 라이브러리만 사용.

Windows에서 실행:
  python -I -B carecall_esp32_wifi_precheck.py --project C:\\Users\\dsc-nb02\\carecall_esp32

프로젝트/빌드 파일을 읽기만 합니다. 빌드, 플래시, COM 접속, 네트워크 접속,
설정 변경, NVS 삭제를 실행하지 않습니다. Wi-Fi 이름/비밀번호, MQTT 주소/
계정/비밀번호, 장치 ID는 출력하지 않습니다. 결과는 현재 PC 파일에 대한
점검이며, 장치에 실제 기록된 펌웨어나 NVS 내용과 같다는 보장은 아닙니다.
"""

import argparse
import hashlib
import json
import re
import struct
import sys
from pathlib import Path

VERSION = "20260930-esp32-wifi-precheck-2"
REFERENCE_COMMIT = "271bd0e8870aa3a0a26377a8d716394c17a24fba"
EXPECTED = {
    "CMakeLists.txt": "bf20ad5835a6628920a4e007e756a636b23736243fabea9c7320efaf90a62dbe",
    "main/CMakeLists.txt": "3cd7cea94556e63838045fa83101c03c911dc1eadd135c40f7b589316dc4d0f3",
    "main/Kconfig.projbuild": "7be1ba3e9197e9ae9d06de5ccbcf7f3c2d45fd6f6a60a4bf50cce5e49322ae99",
    "main/app_main.cpp": "a50e43c21b723211ea73d3fe6dc7c4a3cca594fbfe96ecde9ccbce5487ee32ff",
    "main/button_driver.cpp": "a06416fef111d7b8f3881871c1ed878906017442cb141f9e39fb07b8778a1dc1",
    "main/button_driver.h": "dfdcdb5861376ca3363457fab32b33407462968e3cdae1957c73eae391e8324d",
    "main/call_led.cpp": "3de19960ce59c4ad9a76f2452e9c8c853b64a8ce1653c4b7bb399a97465f4e55",
    "main/call_led.h": "c3347597ce374f2aa7d83825cebf721fe5f2c0e9697518a4a56b5d5aa8f6f797",
    "main/call_manager.cpp": "247c7ee520d2a99de6289d3738f4d615e5ede59ac38fb3bbb4f9c79118f5f8f8",
    "main/call_manager.h": "f817084e17f1d70fa3573798a5e5bfbd67a25f8d2546a29f01de58b51590a0a0",
    "main/call_outbox.cpp": "2fd3ec2702c146b6ac3c6f40c533992d581b52fa87e59368e30afe6add551d32",
    "main/call_outbox.h": "264d4d536b4c93ce46d7dd0ec4cce9626f300b4b9f996b9e215117c4ffded43b",
    "main/idf_component.yml": "3ded44a96723fce547a1d85d9e6cfaf1053b4ab73ba10ace85b782509f095ed2",
    "main/mqtt_manager.cpp": "2c7047f34cf7c1860836bba83816b889aa3ee0b4e96b05994616ebbf7790b692",
    "main/mqtt_manager.h": "86b4ab48536fa85b593c3f8b6b93f88b0b7344b227808aa10a81eb4ab1d9bccb",
    "main/wifi_manager.cpp": "496c2f87f3783f7ec64dee139250a3d72c1b611a340d6dbf913056aa29befb6f",
    "main/wifi_manager.h": "2c03ff84461cdb041badceea094d63e469e3ada7c873e6a7e84313da25d664a6",
}

# Derived from the same pinned public source, never from private user settings.
EXPECTED_LAYOUT = {
    "CMakeLists.txt": [
        "fdfc43af45b09ab8c4823164126bab363f83403adb4b38c4f6ae080a2eb3c28a",
        "fdfc43af45b09ab8c4823164126bab363f83403adb4b38c4f6ae080a2eb3c28a"
    ],
    "main/CMakeLists.txt": [
        "76e1703c283f49db89d50c68950275d004123e71f2fb4932038a1699a649306d",
        "4e473db3878145dc3d781dd1b9f5c7d9bd7d9d36b7a36499ff219e611c01943d"
    ],
    "main/Kconfig.projbuild": [
        "5f48c32b37f399648f78b6f0fc9489a19783aa993182cf863a8141afbd09d5f4",
        "55663e353c9b157f7efc40814e30136c57589ee891e9de04be50c70cb50a8b1c"
    ],
    "main/app_main.cpp": [
        "6ebe82f655856ab9da9789a3a51b944d87c49484f0f13d9c142fae11f73a9f7f",
        "9346c79effb2321ab070af5277f72b840295987fdcba057795eb9a8c558dc1de"
    ],
    "main/button_driver.cpp": [
        "5353b64b5e7cbc7302a6f9e1d58c2bb58a47b1b52a61cce79003ff1bf6fb4ac2",
        "903a75ff6659b267bade56e9e971fc7921bf86f148566e061ca89019815e91b1"
    ],
    "main/button_driver.h": [
        "1b1554e1b39297dbdd4af2e25ce34e2fe04417a7f64ee3be6c7e41b736b1be41",
        "4779fd30565f6cbccc5676aaede425f2fd6dee29c860e261df941ec275340287"
    ],
    "main/call_led.cpp": [
        "38aa219e3d40ca2d0375b66f174b8052dcf794458fc3a6004d47e8fc93634623",
        "13303cd5166b53c02f9a1df5047f6f9a3294eb9a9becb68181bd36009b273faa"
    ],
    "main/call_led.h": [
        "51d1da85f82fb66b00ea6abca9945efe91eb603273c8a53b03159b529ca489e2",
        "51d1da85f82fb66b00ea6abca9945efe91eb603273c8a53b03159b529ca489e2"
    ],
    "main/call_manager.cpp": [
        "61160d74cb1265f1b076f4bbb8022664e0a940b840c5cc9b6935c40012d1b1cf",
        "6ca5717b7d139e8510d5247b865d903109b2af16f5ccc2dcea0e06a03cef2c88"
    ],
    "main/call_manager.h": [
        "71be188935800e828f59870fd738394837bc18dc500e4c6ed165aabb32ad75bf",
        "bd6946ef4cddc99c4f40d9fea65dda72d2735900106b1303310a1813017a763a"
    ],
    "main/call_outbox.cpp": [
        "6668085d1eea6a11e4ac3519cfc30d56e8727f72cce46993b3df281569ca4d76",
        "b85fd24a48d206b93aa6d85b73cc1b498c35d64fcef8623a4ae06f01ad6f52ce"
    ],
    "main/call_outbox.h": [
        "acc7649e0de07d1a892327c79a16beaa1065fc341dd04513164da9a8dc04bb92",
        "dd5fec353b18d5749b41aaec798be9cf57c79d7036512961fa4a0b8d32b7214a"
    ],
    "main/idf_component.yml": [
        "54437c432c7525cb09a0c53cf6adffe98040925f67260fe501e28f8e5bca5faa",
        "273a556a77d2082cb3aeef5ddd1084647a9af111a17a99e394a7121ed4a6c13a"
    ],
    "main/mqtt_manager.cpp": [
        "945373ad11b7cec98c947685e2c5191a4e3dc96033b1910963648b88d7140494",
        "11fb112e1a441ce8c06028de5136cc17e9702038998b0573430c01e4285957a7"
    ],
    "main/mqtt_manager.h": [
        "098b9b8993e96c8c90a00e917fa6e2ba0dbb1c326784a73ca87f45478b72dcf5",
        "929069b12a9c192ed7f87b8d1a80c4a5e567ebde05587592b05da7630953f745"
    ],
    "main/wifi_manager.cpp": [
        "b449ef5b0eb76b75a766686e61d6e9cd4808b945a19fbe1e186ed30543a546ee",
        "4df7017aedddaebb3829314eadae59e98f92867fd4403130ca393ef64beb8267"
    ],
    "main/wifi_manager.h": [
        "ed3c6c9dcb0199193aa2c15bea47e324987d286b19d8a5b7c0603e844f5a865a",
        "b8dfbb36eb8d6e856f2f849fb107312f67ba9c9074b3a33fa489270c914cafd2"
    ]
}



def emit(key, value):
    print(key + "=" + json.dumps(value, ensure_ascii=True, sort_keys=True))


def read_text(path):
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("FILE_SIZE_LIMIT")
    return path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")


def outer_blank_lines_removed(text):
    """Remove only wholly blank lines at file boundaries, not inside code."""
    lines = text.split("\n")
    start, end = 0, len(lines)
    while start < end and not lines[start].strip(" \t"):
        start += 1
    while end > start and not lines[end - 1].strip(" \t"):
        end -= 1
    return "\n".join(lines[start:end])


def line_edge_whitespace_removed(text):
    # Diagnostic only: this can change raw-string contents or preprocessor
    # continuations. A match here must NEVER authorize an automatic patch.
    return "\n".join(line.strip(" \t") for line in outer_blank_lines_removed(text).split("\n"))


def classify_source(name, text):
    digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
    if digest(text) == EXPECTED[name]:
        return "MATCH"
    outer_hash, line_edge_hash = EXPECTED_LAYOUT[name]
    if digest(outer_blank_lines_removed(text)) == outer_hash:
        return "OUTER_BLANK_LINES_ONLY"
    if digest(line_edge_whitespace_removed(text)) == line_edge_hash:
        return "LINE_EDGE_WHITESPACE_MATCH_REVIEW_REQUIRED"
    return "CONTENT_DIFFERENCE_REQUIRES_REVIEW"


def read_installed_idf_version(idf_root):
    header = idf_root / "components" / "esp_common" / "include" / "esp_idf_version.h"
    if not header.is_file():
        return "HEADER_NOT_FOUND"
    text = read_text(header)
    values = []
    for part in ("MAJOR", "MINOR", "PATCH"):
        match = re.search(r"^\s*#\s*define\s+ESP_IDF_VERSION_" + part + r"\s+(\d{1,3})\b", text, re.M)
        if not match:
            return "NOT_IDENTIFIED"
        values.append(str(int(match.group(1))))
    return ".".join(values)


def parse_sdk(text):
    result = {}
    for line in text.splitlines():
        match = re.fullmatch(r"(CONFIG_[A-Z0-9_]+)=(.*)", line)
        if match:
            result[match[1]] = match[2]
        match = re.fullmatch(r"# (CONFIG_[A-Z0-9_]+) is not set", line)
        if match:
            result[match[1]] = "n"
    return result


def known_string(values, key, choices):
    raw = values.get(key)
    if raw is None:
        return "UNSET"
    for choice in choices:
        if raw == json.dumps(choice):
            return choice
    return "OTHER"


def number(values, key):
    raw = values.get(key, "")
    if not re.fullmatch(r"(?:0[xX][0-9a-fA-F]{1,8}|[0-9]{1,8})", raw):
        return None
    return int(raw, 16 if raw.lower().startswith("0x") else 10)


def version_only(raw):
    if not isinstance(raw, str):
        return None
    match = re.search(r"\bv?(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)\b", raw)
    return match.group(1) if match else None


def sdk_summary(text):
    cfg = parse_sdk(text)
    # Standard generated header is '(ESP-IDF) 5.4.1 Project Configuration'.
    header = re.search(r"\bESP-IDF\)?\s+(v?\d+\.\d+(?:\.\d+)?)", text)
    schemes = [name for name in ("SINGLE_APP", "SINGLE_APP_LARGE", "TWO_OTA", "TWO_OTA_LARGE", "CUSTOM")
               if cfg.get("CONFIG_PARTITION_TABLE_" + name) == "y"]
    presence = {}
    for label, suffix in (("wifi_ssid", "WIFI_SSID"), ("wifi_password", "WIFI_PASSWORD"),
                          ("mqtt_mdns_host", "MQTT_BROKER_MDNS_HOST"),
                          ("mqtt_username", "MQTT_USERNAME"), ("mqtt_password", "MQTT_PASSWORD"),
                          ("mqtt_client_id", "MQTT_CLIENT_ID"), ("device_id", "DEVICE_ID")):
        presence[label] = cfg.get("CONFIG_CARECALL_" + suffix, '""') not in ('""', "")
    summary = {
        "header_idf_version": version_only(header.group(1)) if header else None,
        "target": known_string(cfg, "CONFIG_IDF_TARGET", ("esp32c3", "esp32", "esp32s3", "esp32c6", "esp32s2")),
        "flash_size": known_string(cfg, "CONFIG_ESPTOOLPY_FLASHSIZE", ("1MB", "2MB", "4MB", "8MB", "16MB")),
        "partition_schemes": schemes,
        "partition_table_offset": number(cfg, "CONFIG_PARTITION_TABLE_OFFSET"),
        "led_gpio": number(cfg, "CONFIG_CARECALL_LED_GPIO"),
        "led_brightness": number(cfg, "CONFIG_CARECALL_LED_BRIGHTNESS"),
        "mqtt_port": number(cfg, "CONFIG_CARECALL_MQTT_BROKER_PORT"),
        "nvs_encryption": cfg.get("CONFIG_NVS_ENCRYPTION", "n") == "y",
        "secure_boot": cfg.get("CONFIG_SECURE_BOOT", "n") == "y",
        "flash_encryption": cfg.get("CONFIG_FLASH_ENCRYPTION_ENABLED", "n") == "y",
        "private_settings_present": presence,
    }
    return summary, cfg


def decode_partition_table(data):
    if not data or len(data) > 4096 or len(data) % 32:
        raise ValueError("PARTITION_BINARY_LENGTH")
    entries = []
    checked_md5 = False
    for pos in range(0, len(data), 32):
        block = data[pos:pos + 32]
        if block == b"\xff" * 32:
            break
        if block[:2] == b"\xeb\xeb":
            if block[2:16] != b"\xff" * 14 or hashlib.md5(data[:pos]).digest() != block[16:]:
                raise ValueError("PARTITION_MD5_MISMATCH")
            checked_md5 = True
            if any(x != 255 for x in data[pos + 32:]):
                raise ValueError("PARTITION_TRAILING_DATA")
            break
        magic, typ, subtype, offset, size, label, flags = struct.unpack("<HBBII16sI", block)
        if magic != 0x50AA or size == 0:
            raise ValueError("PARTITION_ENTRY_INVALID")
        # Labels are deliberately not output: only numeric layout and known roles.
        role = {(1, 2): "nvs", (1, 1): "phy_init", (1, 0): "otadata", (0, 0): "factory"}.get((typ, subtype), "other")
        if typ == 0 and 0x10 <= subtype <= 0x1F:
            role = "ota_" + str(subtype - 0x10)
        entries.append({"role": role, "type": typ, "subtype": subtype,
                        "offset": offset, "size": size, "flags": flags})
    if not entries:
        raise ValueError("PARTITION_TABLE_EMPTY")
    ordered = sorted(entries, key=lambda item: item["offset"])
    if any(a["offset"] + a["size"] > b["offset"] for a, b in zip(ordered, ordered[1:])):
        raise ValueError("PARTITION_OVERLAP")
    return entries, checked_md5


def run(project, build_name, idf_root=None):
    problems = []
    notes = []
    print("CARECALL_ESP32_WIFI_PRECHECK_BEGIN")
    emit("VERSION", VERSION)
    emit("REFERENCE_COMMIT", REFERENCE_COMMIT)
    emit("PYTHON_VERSION", "%d.%d.%d" % sys.version_info[:3])
    stage = "PROJECT"
    try:
        if not project.is_dir() or not (project / "main").is_dir():
            raise ValueError("PROJECT_LAYOUT")
        stage = "SOURCE"
        statuses = {}
        layout_details = {}
        for name, expected in EXPECTED.items():
            path = project / name
            if not path.is_file():
                statuses[name] = "MISSING"
                continue
            text = read_text(path)
            statuses[name] = classify_source(name, text)
            if statuses[name] != "MATCH":
                lines = text.split("\n")
                layout_details[name] = {
                    "line_count": len(lines),
                    "ends_with_newline": text.endswith("\n"),
                    "file_boundary_blank_line_count": len(lines) - len(outer_blank_lines_removed(text).split("\n")),
                }
        emit("SOURCE_FILES", statuses)
        emit("SOURCE_LAYOUT_DETAILS", layout_details)
        source_matches = all(value in ("MATCH", "OUTER_BLANK_LINES_ONLY") for value in statuses.values())
        exact = all(value == "MATCH" for value in statuses.values())
        emit("SOURCE_BASELINE", "MATCH" if exact else (
            "MATCH_EXCEPT_OUTER_BLANK_LINES" if source_matches else "REVIEW_REQUIRED"))
        if not source_matches:
            problems.append("SOURCE_BASELINE_DIFFERS_OR_FILE_MISSING")
        # Count other files without revealing filenames or content.
        extras = [p for p in (project / "main").rglob("*") if p.is_file()
                  and (p.suffix.lower() in (".c", ".cpp", ".h", ".hpp") or p.name == "CMakeLists.txt")
                  and p.relative_to(project).as_posix() not in EXPECTED]
        emit("ADDITIONAL_SOURCE_FILE_COUNT", len(extras))
        if extras:
            notes.append("ADDITIONAL_SOURCE_FILES_NEED_REVIEW")
        stage = "SDKCONFIG"
        sdk_text = read_text(project / "sdkconfig")
        summary, cfg = sdk_summary(sdk_text)
        emit("SDKCONFIG", summary)
        if summary["target"] != "esp32c3":
            problems.append("TARGET_NOT_ESP32C3")
        if summary["header_idf_version"] is None:
            notes.append("SDKCONFIG_HEADER_VERSION_NOT_IDENTIFIED")
        elif summary["header_idf_version"] != "5.4.1":
            notes.append("SDKCONFIG_HEADER_VERSION_DIFFERS_FROM_5_4_1")
        if summary["flash_size"] != "4MB":
            notes.append("CONFIGURED_FLASH_SIZE_NEEDS_REVIEW")
        if idf_root is not None:
            stage = "INSTALLED_IDF_VERSION"
            installed = read_installed_idf_version(idf_root)
            emit("INSTALLED_IDF_HEADER_VERSION", installed)
            if installed != "5.4.1":
                notes.append("INSTALLED_IDF_VERSION_NEEDS_REVIEW")
        stage = "BUILD"
        build = project / build_name
        emit("BUILD_DIRECTORY_PRESENT", build.is_dir())
        description = build / "project_description.json"
        if description.is_file():
            desc = json.loads(read_text(description))
            metadata_version = version_only(desc.get("idf_ver")) or version_only(desc.get("idf_version"))
            emit("BUILD_METADATA_IDF_VERSION", metadata_version or "NOT_RECORDED")
            # Missing metadata is not evidence of a broken SDK installation.
        else:
            notes.append("BUILD_DESCRIPTION_MISSING")
        built_sdk = build / "config" / "sdkconfig.json"
        if built_sdk.is_file():
            built = json.loads(read_text(built_sdk))
            checks = {}
            for label, key in (("wifi_ssid", "CONFIG_CARECALL_WIFI_SSID"),
                               ("wifi_password", "CONFIG_CARECALL_WIFI_PASSWORD"),
                               ("mqtt_mdns_host", "CONFIG_CARECALL_MQTT_BROKER_MDNS_HOST"),
                               ("mqtt_username", "CONFIG_CARECALL_MQTT_USERNAME"),
                               ("mqtt_password", "CONFIG_CARECALL_MQTT_PASSWORD"),
                               ("device_id", "CONFIG_CARECALL_DEVICE_ID")):
                try:
                    current = json.loads(cfg[key])
                    checks[label] = "MATCH" if current == built[key.removeprefix("CONFIG_")] else "DIFFERENT"
                except (KeyError, ValueError):
                    checks[label] = "UNKNOWN"
            emit("BUILD_PRIVATE_SETTINGS_VS_SDKCONFIG", checks)
            if any(value == "DIFFERENT" for value in checks.values()):
                notes.append("BUILD_SETTINGS_DIFFER_FROM_CURRENT_SDKCONFIG")
        else:
            notes.append("BUILD_SDKCONFIG_JSON_MISSING")
        part_path = build / "partition_table" / "partition-table.bin"
        entries = []
        if part_path.is_file():
            stage = "PARTITION_TABLE"
            if part_path.stat().st_size > 4096:
                raise ValueError("PARTITION_FILE_SIZE")
            entries, md5_checked = decode_partition_table(part_path.read_bytes())
            emit("BUILD_PARTITIONS", entries)
            emit("BUILD_PARTITION_MD5_VERIFIED", md5_checked)
        else:
            notes.append("BUILT_PARTITION_TABLE_MISSING")
        stage = "APP_SIZE"
        app = build / "carecall_esp32.bin"
        if app.is_file():
            app_size = app.stat().st_size
            emit("BUILD_APP_BYTES", app_size)
            apps = [entry for entry in entries if entry["type"] == 0]
            if len(apps) == 1:
                remaining = apps[0]["size"] - app_size
                emit("BUILD_APP_PARTITION_BYTES", apps[0]["size"])
                emit("BUILD_APP_REMAINING_BYTES", remaining)
                if remaining < 0:
                    problems.append("APP_EXCEEDS_PARTITION")
                elif remaining < 128 * 1024:
                    notes.append("APP_SPACE_SMALL_REVIEW_BEFORE_ADDING_WEB_UI")
            else:
                notes.append("APP_PARTITION_SELECTION_NEEDS_REVIEW")
        else:
            notes.append("BUILT_APP_BINARY_MISSING")
        emit("PROBLEMS", problems)
        emit("NOTES", notes)
        emit("CHECK_RESULT", "REVIEW_REQUIRED" if problems or notes else "COMPLETE")
        return 0
    except Exception as exc:
        # No exception message or traceback: it could contain private input.
        emit("CHECK_FAILURE", {"stage": stage, "exception_type": type(exc).__name__})
        emit("CHECK_RESULT", "FAILED")
        return 1
    finally:
        emit("PROJECT_MUTATIONS_REQUESTED", False)
        emit("BUILD_OR_FLASH_REQUESTED", False)
        emit("DEVICE_FLASH_READ", False)
        emit("PRIVATE_VALUES_PRINTED", False)
        print("CARECALL_ESP32_WIFI_PRECHECK_END")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--idf-root", type=Path,
                        help="Optional installed ESP-IDF root; only its version header is read.")
    parser.add_argument("--build-dir", default="build_guardian_confirm",
                        choices=("build_guardian_confirm", "build_offline_calls", "build"))
    args = parser.parse_args()
    return run(args.project, args.build_dir, args.idf_root)


if __name__ == "__main__":
    raise SystemExit(main())
