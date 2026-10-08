#!/usr/bin/env python3
"""Build and verify CareCall button02. Never access a serial port or Pi."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

VERSION = "20261008-button02-build-2"
PREPARE_VERSION = "20261008-button02-prepare-2"
DEFAULT_PROJECT = Path(r"C:\Users\dsc-nb02\Carecall_main_file\carecall_esp32_button02")
EXPECTED_IDF = Path(r"C:\esp541\.espressif\v5.4.1\esp-idf")
PROFILE_MARKER = "carecall_button02_profile.json"
VERIFY_MARKER = "carecall_button02_build_verified.json"
BUILD_NAME = "build_button02"
PRIVATE_KEYS = (
    "CARECALL_WIFI_SSID",
    "CARECALL_WIFI_PASSWORD",
    "CARECALL_DEVICE_ID",
    "CARECALL_MQTT_BROKER_MDNS_HOST",
    "CARECALL_MQTT_BROKER_PORT",
    "CARECALL_MQTT_USERNAME",
    "CARECALL_MQTT_PASSWORD",
    "CARECALL_MQTT_CLIENT_ID",
    "CARECALL_LED_GPIO",
    "CARECALL_LED_BRIGHTNESS",
    "CARECALL_SETUP_AP_PASSWORD",
)
IDENTITY = {
    "CARECALL_DEVICE_ID": "button02",
    "CARECALL_MQTT_USERNAME": "button02",
    "CARECALL_MQTT_CLIENT_ID": "carecall-button02",
}


class Stop(Exception):
    """Expected refusal with a credential-free error code."""


def require(condition, code):
    if not condition:
        raise Stop(code)


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def no_link(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise Stop("REQUIRED_PATH_MISSING:" + str(path)) from None
    require(not stat.S_ISLNK(info.st_mode) and not (
        getattr(info, "st_file_attributes", 0) & 0x400
    ), "LINK_OR_REPARSE_POINT_REFUSED")
    require(stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode),
            "SPECIAL_FILE_REFUSED")


def safe_path(path):
    path = Path(os.path.abspath(path))
    for ancestor in reversed((path, *path.parents)):
        no_link(ancestor)
    return path


def tree_files(directory):
    require(directory.is_dir(), "REQUIRED_DIRECTORY_MISSING")
    no_link(directory)
    result = []
    for entry in sorted(directory.iterdir()):
        no_link(entry)
        if entry.is_dir():
            result.extend(tree_files(entry))
        else:
            result.append(entry)
    return result


def read_json(path, code):
    no_link(path)
    try:
        result = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError):
        raise Stop(code) from None
    require(isinstance(result, dict), code)
    return result


def parse_sdk(path):
    no_link(path)
    values = {}
    for line in path.read_text(encoding="utf-8-sig", errors="strict").splitlines():
        match = re.fullmatch(r"(CONFIG_[A-Z0-9_]+)=(.*)", line)
        if match:
            key, value = match.groups()
            require(key not in values, "DUPLICATE_CONFIG_KEY:" + key)
            values[key] = value
    return values


def sdk_value(values, name):
    key = "CONFIG_" + name
    require(key in values, "CONFIG_MISSING:" + key)
    try:
        return json.loads(values[key])
    except (ValueError, TypeError):
        raw = values[key]
        if raw == "y":
            return True
        if re.fullmatch(r"-?[0-9]+", raw):
            return int(raw)
        raise Stop("CONFIG_FORMAT_ERROR:" + key) from None


def expected_project_files(record):
    hashes = record.get("source_hashes")
    require(isinstance(hashes, dict) and hashes, "PROFILE_SOURCE_HASHES_INVALID")
    excluded = {"sdkconfig", "carecall_wifi_stage.json"}
    return {name: value for name, value in hashes.items() if name not in excluded}


def validate_inputs(project):
    project = safe_path(project)
    require(project.is_dir(), "BUTTON02_PROJECT_MISSING")
    record = read_json(project / PROFILE_MARKER, "PROFILE_MARKER_INVALID")
    require(record.get("version") == PREPARE_VERSION, "PROFILE_VERSION_MISMATCH")
    require(record.get("phase") == "profile_prepared_not_built", "PROFILE_PHASE_MISMATCH")
    require(record.get("device_id") == "button02", "PROFILE_DEVICE_MISMATCH")
    require(record.get("mqtt_username") == "button02", "PROFILE_USERNAME_MISMATCH")
    require(record.get("mqtt_client_id") == "carecall-button02", "PROFILE_CLIENT_ID_MISMATCH")
    require(record.get("password_reuse_allowed") is True, "PROFILE_PASSWORD_POLICY_MISMATCH")
    require(record.get("build_verified") is False and record.get("flash_requested") is False,
            "PROFILE_STATE_MISMATCH")
    require(record.get("pi_registered") is False, "PI_ALREADY_REGISTERED_UNEXPECTED")
    require(not os.path.lexists(project / VERIFY_MARKER), "BUILD_VERIFY_MARKER_ALREADY_EXISTS")
    for name in ("components", "sdkconfig.defaults", "sdkconfig.defaults.esp32c3"):
        require(not os.path.lexists(project / name), "UNREVIEWED_BUILD_INPUT:" + name)

    expected = expected_project_files(record)
    expected_main = {name for name in expected if name.startswith("main/")}
    actual_main = {
        path.relative_to(project).as_posix()
        for path in tree_files(project / "main")
    }
    require(actual_main == expected_main, "BUTTON02_MAIN_FILE_LIST_CHANGED")
    expected_managed = {name for name in expected if name.startswith("managed_components/")}
    actual_managed = {
        path.relative_to(project).as_posix()
        for path in tree_files(project / "managed_components")
    }
    require(actual_managed == expected_managed, "BUTTON02_MANAGED_FILE_LIST_CHANGED")
    for name, wanted in expected.items():
        path = project / name
        no_link(path)
        require(path.is_file() and digest(path) == wanted, "BUTTON02_SOURCE_DIFF:" + name)

    config = project / "sdkconfig"
    no_link(config)
    require(digest(config) == record.get("profile_sdkconfig_sha256"),
            "BUTTON02_SDKCONFIG_CHANGED")
    values = parse_sdk(config)
    for key, wanted in IDENTITY.items():
        require(sdk_value(values, key) == wanted, "BUTTON02_IDENTITY_MISMATCH:" + key)
    for key in ("CARECALL_MQTT_PASSWORD", "CARECALL_WIFI_SSID", "CARECALL_WIFI_PASSWORD"):
        value = sdk_value(values, key)
        require(isinstance(value, str) and bool(value), "EMPTY_PRIVATE_SETTING:" + key)
    ap_password = sdk_value(values, "CARECALL_SETUP_AP_PASSWORD")
    require(isinstance(ap_password, str) and
            re.fullmatch(r"[A-Za-z0-9]{16,63}", ap_password) is not None,
            "SETUP_AP_PASSWORD_INVALID")
    mqtt_password = sdk_value(values, "CARECALL_MQTT_PASSWORD")
    require(isinstance(mqtt_password, str) and
            re.fullmatch(r"[A-Za-z0-9]{16,63}", mqtt_password) is not None,
            "MQTT_PASSWORD_INVALID")
    required = {
        "CONFIG_IDF_TARGET": '"esp32c3"',
        "CONFIG_ESPTOOLPY_FLASHSIZE": '"4MB"',
        "CONFIG_PARTITION_TABLE_CUSTOM": "y",
        "CONFIG_PARTITION_TABLE_CUSTOM_FILENAME": '"partitions_carecall_wifi.csv"',
        "CONFIG_PARTITION_TABLE_OFFSET": "0x8000",
        "CONFIG_ESP_WIFI_SOFTAP_SUPPORT": "y",
        "CONFIG_LWIP_DHCPS": "y",
    }
    for key, wanted in required.items():
        require(values.get(key) == wanted, "BUILD_SETTING_MISMATCH:" + key)
    for key in ("CONFIG_SECURE_BOOT", "CONFIG_FLASH_ENCRYPTION_ENABLED", "CONFIG_NVS_ENCRYPTION"):
        require(values.get(key) != "y", "SECURITY_LAYOUT_CHANGED:" + key)

    source = safe_path(Path(record.get("source_project", "")))
    require(source.name == "carecall_esp32_wifi_setup" and source.parent == project.parent,
            "SOURCE_PROJECT_LOCATION_MISMATCH")
    source_hashes = record["source_hashes"]
    for name, wanted in source_hashes.items():
        path = source / name
        no_link(path)
        require(path.is_file() and digest(path) == wanted, "BUTTON01_SOURCE_CHANGED:" + name)

    idf = safe_path(EXPECTED_IDF)
    require(str(record.get("idf_root_hint", "")).lower() == str(EXPECTED_IDF).lower(),
            "IDF_ROOT_HINT_MISMATCH")
    require((idf / "tools/idf.py").is_file() and (idf / "tools/idf_tools.py").is_file(),
            "ESP_IDF_TOOLS_MISSING")
    return project, record, expected, values, source, idf


def tools_root():
    executable = Path(sys.executable)
    require(len(executable.parents) >= 6, "PYTHON_LOCATION_UNEXPECTED")
    result = safe_path(executable.parents[5])
    expected_tail = ("tools", "python", "v5.4.1", "venv", "scripts")
    actual_tail = tuple(part.lower() for part in executable.parent.parts[-5:])
    require(actual_tail == expected_tail, "PYTHON_LOCATION_UNEXPECTED")
    require((result / "tools").is_dir(), "IDF_TOOLS_ROOT_MISSING")
    return result


def exported_environment(project, idf, secrets):
    environment = os.environ.copy()
    environment.update(
        IDF_PATH=str(idf),
        IDF_TOOLS_PATH=str(tools_root()),
        IDF_PYTHON_ENV_PATH=sys.prefix,
        IDF_TARGET="esp32c3",
        PYTHONUTF8="1",
    )
    result = subprocess.run(
        [sys.executable, str(idf / "tools/idf_tools.py"), "--idf-path", str(idf),
         "export", "--format", "key-value"],
        cwd=project, env=environment, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode:
        print_sanitized_tail(result.stderr, secrets)
        raise Stop("EXISTING_IDF_TOOLS_ENVIRONMENT_NOT_READY")
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        key, separator, value = line.partition("=")
        require(bool(separator) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key),
                "IDF_ENVIRONMENT_EXPORT_FORMAT")
        if key.upper() == "PATH":
            previous = next((value for name, value in environment.items()
                             if name.upper() == "PATH"), "")
            value = value.replace("%PATH%", previous).replace("$PATH", previous)
            for old_key in list(environment):
                if old_key.upper() == "PATH":
                    del environment[old_key]
            key = "PATH"
        environment[key] = value
    return environment


def verify_idf_version(project, idf, environment, secrets):
    result = subprocess.run(
        [sys.executable, str(idf / "tools/idf.py"), "--version"],
        cwd=project, env=environment, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    output = result.stdout + "\n" + result.stderr
    if result.returncode:
        print_sanitized_tail(output, secrets)
        raise Stop("ESP_IDF_VERSION_COMMAND_FAILED")
    match = re.search(r"(?:ESP-IDF\s+)?v?(5\.4\.1)(?:[-+\s]|$)", output)
    require(match is not None, "ESP_IDF_5_4_1_REQUIRED")


def print_sanitized_tail(output, secrets):
    redacted = output
    for value in sorted((item for item in secrets if isinstance(item, str) and item),
                        key=len, reverse=True):
        redacted = redacted.replace(value, "[REDACTED]")
    lines = redacted.splitlines()[-120:]
    if lines:
        print("BUILD_DIAGNOSTIC_TAIL_BEGIN")
        print("\n".join(lines))
        print("BUILD_DIAGNOSTIC_TAIL_END")


def decode_partition_table(path):
    data = path.read_bytes()
    require(len(data) == 0xC00, "PARTITION_TABLE_BINARY_SIZE_MISMATCH")
    entries = []
    cursor = 0
    while cursor + 32 <= len(data):
        block = data[cursor:cursor + 32]
        if block[:2] == b"\xaa\x50":
            label = block[12:28].split(b"\0", 1)[0].decode("ascii", errors="strict")
            entries.append((block[2], block[3], int.from_bytes(block[4:8], "little"),
                            int.from_bytes(block[8:12], "little"), label,
                            int.from_bytes(block[28:32], "little")))
            cursor += 32
            continue
        require(block[:2] == b"\xeb\xeb" and block[2:16] == b"\xff" * 14,
                "PARTITION_TABLE_CHECKSUM_ENTRY_MISSING")
        require(block[16:32] == hashlib.md5(data[:cursor]).digest(),
                "PARTITION_TABLE_CHECKSUM_MISMATCH")
        break
    expected = [
        (1, 2, 0x9000, 0x6000, "nvs", 0),
        (1, 1, 0xF000, 0x1000, "phy_init", 0),
        (0, 0, 0x10000, 0x200000, "factory", 0),
    ]
    require(entries == expected, "PARTITION_LAYOUT_MISMATCH")
    return entries


def verify_build(project, record, expected, values, source):
    build = project / BUILD_NAME
    description = read_json(build / "project_description.json", "PROJECT_DESCRIPTION_INVALID")
    require(description.get("git_revision") == "v5.4.1", "BUILD_IDF_VERSION_MISMATCH")
    require(description.get("target") == "esp32c3", "BUILD_TARGET_MISMATCH")
    require(Path(description.get("project_path", "")).resolve() == project.resolve(),
            "BUILD_PROJECT_PATH_MISMATCH")
    require(Path(description.get("build_dir", "")).resolve() == build.resolve(),
            "BUILD_DIRECTORY_MISMATCH")
    require(Path(description.get("config_file", "")).resolve() == (project / "sdkconfig").resolve(),
            "BUILD_CONFIG_PATH_MISMATCH")

    built = read_json(build / "config/sdkconfig.json", "BUILT_CONFIG_INVALID")
    for name in PRIVATE_KEYS:
        require(built.get(name) == sdk_value(values, name), "BUILT_PRIVATE_SETTING_MISMATCH:" + name)
    require(built.get("IDF_TARGET") == "esp32c3", "BUILT_TARGET_MISMATCH")
    require(built.get("ESPTOOLPY_FLASHSIZE") == "4MB", "BUILT_FLASH_SIZE_MISMATCH")
    require(built.get("PARTITION_TABLE_CUSTOM") is True, "BUILT_PARTITION_SETTING_MISSING")
    require(built.get("PARTITION_TABLE_CUSTOM_FILENAME") == "partitions_carecall_wifi.csv",
            "BUILT_PARTITION_FILENAME_MISMATCH")
    require(not any(built.get(key, False) for key in
                    ("SECURE_BOOT", "FLASH_ENCRYPTION_ENABLED", "NVS_ENCRYPTION")),
            "BUILT_SECURITY_LAYOUT_CHANGED")

    partition = build / "partition_table/partition-table.bin"
    decode_partition_table(partition)
    flash = read_json(build / "flasher_args.json", "FLASH_ARGUMENTS_INVALID")
    require(flash.get("flash_settings") == {
        "flash_mode": "dio", "flash_size": "4MB", "flash_freq": "80m"
    }, "FLASH_SETTINGS_MISMATCH")
    planned = {int(offset, 0): name for offset, name in flash.get("flash_files", {}).items()}
    expected_files = {
        0: "bootloader/bootloader.bin",
        0x8000: "partition_table/partition-table.bin",
        0x10000: "carecall_esp32.bin",
    }
    require(planned == expected_files, "UNEXPECTED_FLASH_PLAN")
    limits = {0: 0x8000, 0x8000: 0x9000, 0x10000: 0x210000}
    verified = {}
    for offset, relative in expected_files.items():
        path = (build / planned[offset]).resolve()
        require(path == (build / relative).resolve() and path.is_relative_to(build.resolve()),
                "UNEXPECTED_FLASH_FILE")
        no_link(path)
        size = path.stat().st_size
        require(size > 0 and offset + ((size + 4095) // 4096 * 4096) <= limits[offset],
                "FLASH_FILE_SIZE_INVALID")
        require(not (offset < 0xF000 and offset + ((size + 4095) // 4096 * 4096) > 0x9000),
                "FLASH_PLAN_OVERLAPS_NVS")
        verified[relative] = {"offset": offset, "size": size, "sha256": digest(path)}

    app = (build / "carecall_esp32.bin").read_bytes()
    require(app.count(b"button02") >= 1 and app.count(b"carecall-button02") >= 1,
            "BUTTON02_IDENTITY_NOT_EMBEDDED")
    require(b"button01" not in app and b"carecall-button01" not in app,
            "BUTTON01_IDENTITY_FOUND_IN_BUTTON02_BINARY")

    for name, wanted in expected.items():
        require(digest(project / name) == wanted, "BUTTON02_SOURCE_CHANGED_DURING_BUILD:" + name)
    for name, wanted in record["source_hashes"].items():
        require(digest(source / name) == wanted, "BUTTON01_SOURCE_CHANGED_DURING_BUILD:" + name)
    require(digest(project / "sdkconfig") == record["profile_sdkconfig_sha256"],
            "BUTTON02_SDKCONFIG_CHANGED_DURING_BUILD")

    result = {
        "version": VERSION,
        "profile_version": PREPARE_VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "project": str(project),
        "device_id": "button02",
        "mqtt_username": "button02",
        "mqtt_client_id": "carecall-button02",
        "idf_version": "5.4.1",
        "target": "esp32c3",
        "flash_size": "4MB",
        "files": verified,
        "planned_flash_offsets": ["0x0", "0x8000", "0x10000"],
        "nvs_range": {"offset": "0x9000", "size": "0x6000"},
        "nvs_write_planned": False,
        "flash_requested": False,
        "pi_change_requested": False,
    }
    path = project / VERIFY_MARKER
    with path.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, ensure_ascii=True)
        handle.write("\n")
    print("BUTTON02_BUILD_VERIFY=PASS")
    print("BUILD_IDF_VERSION=5.4.1")
    print("BUILD_TARGET=esp32c3")
    print("BUILD_FLASH_SIZE=4MB")
    print("BUTTON02_IDENTITY_VERIFIED=YES")
    print("BUTTON01_INPUTS_UNCHANGED=YES")
    print("PARTITION_LAYOUT_VERIFIED=YES")
    print("PLANNED_FLASH_WRITES_EXCLUDE_NVS=YES")
    print("APP_PARTITION_BYTES=2097152")
    print("APP_BINARY_BYTES=" + str(verified["carecall_esp32.bin"]["size"]))
    print("APP_REMAINING_BYTES=" + str(0x200000 - verified["carecall_esp32.bin"]["size"]))
    print("PASSWORD_VALUES_PRINTED=NO")


def build(project):
    project, record, expected, values, source, idf = validate_inputs(project)
    build_dir = project / BUILD_NAME
    owner = build_dir / ".carecall_button02_build_owner"
    if build_dir.exists():
        no_link(build_dir)
        require(owner.is_file() and owner.read_text(encoding="utf-8") == str(project.resolve()),
                "UNOWNED_BUILD_DIRECTORY_REFUSED")
    else:
        build_dir.mkdir()
        owner.write_text(str(project.resolve()), encoding="utf-8")
    secrets = [sdk_value(values, key) for key in PRIVATE_KEYS]
    environment = exported_environment(project, idf, secrets)
    verify_idf_version(project, idf, environment, secrets)
    print("BUTTON02_BUILD=STARTING", flush=True)
    process = subprocess.run(
        [sys.executable, str(idf / "tools/idf.py"), "-C", str(project),
         "-B", str(build_dir), "build"],
        cwd=project, env=environment, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if process.returncode:
        print_sanitized_tail(process.stdout + "\n" + process.stderr, secrets)
        raise Stop("ESP_IDF_BUILD_FAILED")
    verify_build(project, record, expected, values, source)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=DEFAULT_PROJECT)
    args = parser.parse_args()
    print("TOOL_VERSION=" + VERSION)
    print("PROJECT=" + str(args.project))
    try:
        build(args.project)
        return 0
    except (Exception, KeyboardInterrupt) as error:
        if isinstance(error, Stop):
            code = str(error)
        elif isinstance(error, FileNotFoundError):
            code = "REQUIRED_PATH_MISSING:" + str(error.filename or "UNKNOWN")
        else:
            code = type(error).__name__
        print("BUTTON02_BUILD_VERIFY=FAILED")
        print("ERROR_CODE=" + code)
        print("DO_NOT_FLASH_UNVERIFIED_BUILD=YES")
        return 1
    finally:
        print("FLASH_WRITE_REQUESTED=NO")
        print("NVS_ERASE_REQUESTED=NO")
        print("PI_CHANGE_REQUESTED=NO")


if __name__ == "__main__":
    raise SystemExit(main())
