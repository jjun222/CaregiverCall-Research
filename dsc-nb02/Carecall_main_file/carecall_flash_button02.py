#!/usr/bin/env python3
"""Safely check or clean-flash the verified CareCall button02 build."""
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

VERSION = "20261008-button02-flash-1"
BUILD_VERSION = "20261008-button02-build-2"
PREPARE_VERSION = "20261008-button02-prepare-2"
DEFAULT_PROJECT = Path(
    r"C:\Users\dsc-nb02\Carecall_main_file\carecall_esp32_button02"
)
BUILD_NAME = "build_button02"
BUILD_MARKER = "carecall_button02_build_verified.json"
PROFILE_MARKER = "carecall_button02_profile.json"
FLASH_MARKER = "carecall_button02_flash_verified.json"
EXPECTED_MAC = "e8:3d:c1:82:30:a8"
EXPECTED_ESPTOOL = "4.12.0"
NVS_OFFSET = 0x9000
NVS_SIZE = 0x6000
FLASH_FILES = {
    "bootloader/bootloader.bin": 0x0,
    "partition_table/partition-table.bin": 0x8000,
    "carecall_esp32.bin": 0x10000,
}


class Stop(Exception):
    """Expected refusal with a non-secret error code."""


def require(condition, code):
    if not condition:
        raise Stop(code)


def no_link(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise Stop("REQUIRED_PATH_MISSING:" + str(path)) from None
    require(
        not stat.S_ISLNK(info.st_mode)
        and not (getattr(info, "st_file_attributes", 0) & 0x400),
        "LINK_OR_REPARSE_POINT_REFUSED:" + str(path),
    )
    require(
        stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode),
        "SPECIAL_FILE_REFUSED:" + str(path),
    )


def safe_path(path):
    path = Path(os.path.abspath(path))
    for ancestor in reversed((path, *path.parents)):
        no_link(ancestor)
    return path


def read_json(path, code):
    no_link(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError):
        raise Stop(code) from None
    require(isinstance(value, dict), code)
    return value


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def validate_artifacts(project):
    project = safe_path(project)
    require(project.is_dir(), "BUTTON02_PROJECT_MISSING")
    require(not os.path.lexists(project / FLASH_MARKER), "BUTTON02_ALREADY_FLASHED")

    profile = read_json(project / PROFILE_MARKER, "PROFILE_MARKER_INVALID")
    require(profile.get("version") == PREPARE_VERSION, "PROFILE_VERSION_MISMATCH")
    require(profile.get("device_id") == "button02", "PROFILE_DEVICE_MISMATCH")
    require(profile.get("mqtt_username") == "button02", "PROFILE_USERNAME_MISMATCH")
    require(
        profile.get("mqtt_client_id") == "carecall-button02",
        "PROFILE_CLIENT_ID_MISMATCH",
    )

    marker = read_json(project / BUILD_MARKER, "BUILD_MARKER_INVALID")
    require(marker.get("version") == BUILD_VERSION, "BUILD_VERSION_MISMATCH")
    require(marker.get("profile_version") == PREPARE_VERSION,
            "BUILD_PROFILE_VERSION_MISMATCH")
    require(marker.get("device_id") == "button02", "BUILD_DEVICE_MISMATCH")
    require(marker.get("mqtt_username") == "button02", "BUILD_USERNAME_MISMATCH")
    require(marker.get("mqtt_client_id") == "carecall-button02",
            "BUILD_CLIENT_ID_MISMATCH")
    require(marker.get("idf_version") == "5.4.1", "BUILD_IDF_VERSION_MISMATCH")
    require(marker.get("target") == "esp32c3", "BUILD_TARGET_MISMATCH")
    require(marker.get("flash_size") == "4MB", "BUILD_FLASH_SIZE_MISMATCH")
    require(marker.get("planned_flash_offsets") == ["0x0", "0x8000", "0x10000"],
            "BUILD_FLASH_OFFSETS_MISMATCH")
    require(marker.get("nvs_range") == {"offset": "0x9000", "size": "0x6000"},
            "BUILD_NVS_RANGE_MISMATCH")
    require(marker.get("nvs_write_planned") is False,
            "BUILD_MARKER_NVS_WRITE_UNEXPECTED")
    require(marker.get("flash_requested") is False,
            "BUILD_MARKER_FLASH_STATE_INVALID")

    files = marker.get("files")
    require(isinstance(files, dict) and set(files) == set(FLASH_FILES),
            "BUILD_FILE_SET_MISMATCH")
    build = project / BUILD_NAME
    no_link(build)
    require(build.is_dir(), "BUILD_DIRECTORY_MISSING")
    verified = {}
    for relative, offset in FLASH_FILES.items():
        metadata = files.get(relative)
        require(isinstance(metadata, dict), "BUILD_FILE_METADATA_INVALID:" + relative)
        require(metadata.get("offset") == offset,
                "BUILD_FILE_OFFSET_MISMATCH:" + relative)
        require(isinstance(metadata.get("size"), int) and metadata["size"] > 0,
                "BUILD_FILE_SIZE_INVALID:" + relative)
        require(re.fullmatch(r"[0-9a-f]{64}", str(metadata.get("sha256", ""))) is not None,
                "BUILD_FILE_HASH_INVALID:" + relative)
        path = build / relative
        no_link(path)
        require(path.is_file(), "BUILD_FILE_MISSING:" + relative)
        require(path.stat().st_size == metadata["size"],
                "BUILD_FILE_SIZE_CHANGED:" + relative)
        require(digest(path) == metadata["sha256"],
                "BUILD_FILE_HASH_CHANGED:" + relative)
        verified[relative] = path

    app = verified["carecall_esp32.bin"].read_bytes()
    require(b"button02" in app and b"carecall-button02" in app,
            "BUTTON02_IDENTITY_NOT_EMBEDDED")
    require(b"button01" not in app and b"carecall-button01" not in app,
            "BUTTON01_IDENTITY_FOUND_IN_BINARY")
    return project, marker, verified


def normalized_port(port):
    port = port.strip().upper()
    require(re.fullmatch(r"COM[1-9][0-9]*", port) is not None,
            "COM_PORT_FORMAT_INVALID")
    return port


def esptool_command(port, after, operation):
    return [
        sys.executable,
        "-I",
        "-B",
        "-m",
        "esptool",
        "--chip",
        "esp32c3",
        "--port",
        port,
        "--baud",
        "115200",
        "--before",
        "default_reset",
        "--after",
        after,
        *operation,
    ]


def run_captured(port, after, operation, code):
    result = subprocess.run(
        esptool_command(port, after, operation),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = result.stdout + "\n" + result.stderr
    if result.returncode:
        if output.strip():
            print("ESPTOOL_DIAGNOSTIC_BEGIN")
            print("\n".join(output.splitlines()[-80:]))
            print("ESPTOOL_DIAGNOSTIC_END")
        raise Stop(code)
    return output


def probe_device(port):
    output = run_captured(port, "hard_reset", ["flash_id"], "DEVICE_PROBE_FAILED")
    version = re.search(r"(?im)^esptool(?:\.py)?\s+v([0-9.]+)\s*$", output)
    mac = re.search(
        r"(?im)^MAC:\s*([0-9a-f]{2}(?::[0-9a-f]{2}){5})\s*$",
        output,
    )
    require(version is not None and version.group(1) == EXPECTED_ESPTOOL,
            "ESPTOOL_VERSION_MISMATCH")
    require(re.search(r"(?im)^Chip is ESP32-C3\b", output) is not None,
            "CONNECTED_CHIP_IS_NOT_ESP32C3")
    require(re.search(r"(?im)^Detected flash size:\s*4MB\s*$", output) is not None,
            "CONNECTED_FLASH_SIZE_IS_NOT_4MB")
    require(mac is not None, "DEVICE_MAC_NOT_FOUND")
    actual_mac = mac.group(1).lower()
    require(actual_mac == EXPECTED_MAC, "WRONG_BUTTON02_DEVICE:" + actual_mac)
    return actual_mac


def check(project, port):
    project, marker, verified = validate_artifacts(project)
    mac = probe_device(port)
    print("BUTTON02_FLASH_CHECK=PASS")
    print("PROJECT=" + str(project))
    print("PORT=" + port)
    print("DEVICE_MAC=" + mac)
    print("DEVICE_IS_ESP32C3=YES")
    print("DEVICE_FLASH_SIZE=4MB")
    print("BUILD_VERSION=" + marker["version"])
    print("BUILD_FILES_HASH_VERIFIED=YES")
    print("BUTTON02_IDENTITY_VERIFIED=YES")
    print("PLANNED_FLASH_OFFSETS=0x0,0x8000,0x10000")
    print("PLANNED_NVS_RESET_RANGE=0x9000,0x6000")
    print("APP_BINARY_BYTES=" + str(verified["carecall_esp32.bin"].stat().st_size))


def run_visible(port, operation, code, after="hard_reset"):
    result = subprocess.run(esptool_command(port, after, operation))
    if result.returncode:
        raise Stop(code)


def clean_flash(project, port, confirmation):
    require(confirmation == "ERASE_BUTTON02_NVS",
            "EXPLICIT_NVS_RESET_CONFIRMATION_REQUIRED")
    project, marker, verified = validate_artifacts(project)
    mac = probe_device(port)
    print("BUTTON02_CLEAN_FLASH=STARTING", flush=True)
    print("NVS_RESET_RANGE=0x9000,0x6000", flush=True)
    run_visible(
        port,
        ["erase_region", hex(NVS_OFFSET), hex(NVS_SIZE)],
        "BUTTON02_NVS_RESET_FAILED",
        after="no_reset",
    )
    print("NVS_RESET=PASS", flush=True)
    operation = [
        "write_flash",
        "--flash_mode",
        "dio",
        "--flash_freq",
        "80m",
        "--flash_size",
        "4MB",
    ]
    for relative, offset in FLASH_FILES.items():
        operation.extend((hex(offset), str(verified[relative])))
    run_visible(port, operation, "BUTTON02_FLASH_WRITE_FAILED")

    record = {
        "version": VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "device_id": "button02",
        "device_mac": mac,
        "port_at_flash_time": port,
        "build_version": marker["version"],
        "build_created_utc": marker.get("created_utc"),
        "nvs_erased": True,
        "nvs_range": {"offset": "0x9000", "size": "0x6000"},
        "flash_offsets": ["0x0", "0x8000", "0x10000"],
        "esptool_write_verification": "pass",
        "pi_change_requested": False,
    }
    path = project / FLASH_MARKER
    with path.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, indent=2, ensure_ascii=True)
        stream.write("\n")
    print("BUTTON02_CLEAN_FLASH=PASS")
    print("DEVICE_MAC=" + mac)
    print("NVS_RESET=YES")
    print("ESPTOOL_WRITE_VERIFY=PASS")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--flash-clean", action="store_true")
    parser.add_argument("--port", required=True)
    parser.add_argument("--project", type=Path, default=DEFAULT_PROJECT)
    parser.add_argument("--confirm-nvs-reset")
    args = parser.parse_args()
    print("TOOL_VERSION=" + VERSION)
    print("EXPECTED_DEVICE_MAC=" + EXPECTED_MAC)
    print("ESPTOOL_REQUIRED_VERSION=" + EXPECTED_ESPTOOL)
    try:
        port = normalized_port(args.port)
        if args.check:
            require(args.confirm_nvs_reset is None,
                    "CONFIRMATION_NOT_ALLOWED_IN_CHECK_MODE")
            check(args.project, port)
        else:
            clean_flash(args.project, port, args.confirm_nvs_reset)
        return 0
    except (Exception, KeyboardInterrupt) as error:
        if isinstance(error, Stop):
            code = str(error)
        elif isinstance(error, FileNotFoundError):
            code = "REQUIRED_PATH_MISSING:" + str(error.filename or "UNKNOWN")
        else:
            code = type(error).__name__
        print("BUTTON02_FLASH_TOOL=FAILED")
        print("ERROR_CODE=" + code)
        print("DO_NOT_BYPASS_CHECKS=YES")
        return 1
    finally:
        print("PI_CHANGE_REQUESTED=NO")
        if "args" in locals() and args.flash_clean:
            print("FLASH_WRITE_REQUESTED=YES")
            print("NVS_ERASE_REQUESTED=YES")
        else:
            print("FLASH_WRITE_REQUESTED=NO")
            print("NVS_ERASE_REQUESTED=NO")


if __name__ == "__main__":
    raise SystemExit(main())
