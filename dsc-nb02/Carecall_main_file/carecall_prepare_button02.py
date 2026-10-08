#!/usr/bin/env python3
"""Prepare a separate button02 source profile. Never build, flash, or contact Pi.

Run without --prepare to check the source only. Run from a real console for
hidden password entry. Requires only Python's standard library.
Generated sdkconfig contains plaintext credentials; keep it private.
Password reuse is allowed; matching confirmation and format checks remain.
"""
import argparse
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import warnings

VERSION = "20261008-button02-prepare-2"
DEFAULT_SOURCE = Path(r"C:\Users\dsc-nb02\Carecall_main_file\carecall_esp32_wifi_setup")
MARKER = "carecall_button02_profile.json"
EXPECTED = {
    "CMakeLists.txt": "7448296750e3e9ef0d849cc2e58dec4f025444129c231445d2f75bc91298e55c",
    "partitions_carecall_wifi.csv": "aa73a8ee9266d60e04568d333b48915a0300439a02567d5a7930a1f642f7e50c",
    "dependencies.lock": "7bc636e769920d919c62ff0f9174f01e350a7cfac30bf033fd9551cf7e7b91d5",
    "main/CMakeLists.txt": "a95d3cad4059109a4bed7ab9c7590f5a0dde000165a7f9a41f92dcb8566b8f48",
    "main/Kconfig.projbuild": "091474fa8292874d168d62eae7ae10e8ae4f4469f60ed6979da960c40fb8bded",
    "main/app_main.cpp": "a6a07e7263711a01f9fac980cf5733ec9e073f7618d5a5f73a5924e165e5b9d9",
    "main/button_driver.cpp": "4a8c58abf8d5e0625903c091d9d11b432c33f183316203add6fc6bd92f7d0b08",
    "main/button_driver.h": "bd5ffbaff78d80dd8d82adc24e01c074de70914e56067853b5274b10b39be529",
    "main/call_led.cpp": "9003859c7fffd2eb72f199b3284921b32de7b9b5f1477faf9c23831aa0ffe0f2",
    "main/call_led.h": "d5d141f58baa95e7854c7486d624584117795331373b8945fed0e4a888dcf256",
    "main/call_manager.cpp": "c36f553c7cbb8d05cf207ad3eddf51219982585f527d32a0eaa92d97c9a7f2d8",
    "main/call_manager.h": "1e0d247171360e43ef640b4a8f1f67cebefcd2dd3a1e88f2c192b30fcdf35785",
    "main/call_outbox.cpp": "3a83aaedbfb5dc30faba56bc2696a0734ad4f522ef19e27acc97b92a0c0800e4",
    "main/call_outbox.h": "7d5e70945992b9db38f8331596ca1010167ff9bf01cf2b7dd515b7334fb13006",
    "main/idf_component.yml": "c429e347d70d3c932999ff92628d4862b32a0782c11227bff453d6e5fb7a7492",
    "main/mqtt_manager.cpp": "a80c9173af144395603e659df12f75cd49bf2b70df484aec8b8d39e0b3fd45d1",
    "main/mqtt_manager.h": "1d84263c29d17ad8690d98303b25506961d81b8b9a8bddde6ec4e458f34d73b1",
    "main/wifi_form.cpp": "a04479ca27a8e1610766ded4cc67137aeacffce068db30894d6f219283f9170f",
    "main/wifi_form.h": "a8a1e4c08611b4a0d5da15f20a9728498bc9bf34733767717d50244ff4d47734",
    "main/wifi_manager.cpp": "1e5ba6f28f04c0d3540a79d1b90f840d9d4ba774db94ab922a19ecb291ab0196",
    "main/wifi_manager.h": "4309b2666ea5a4d2e9f911eb5fa1307c9a7d97883cb2c0d7399e0d7208b3065e",
    "main/wifi_portal.cpp": "ab669d7487a968668162c92e8280e52d9d018308537ca35fc7b308df78c41420",
    "main/wifi_portal.h": "576514d3035e6811cde8bfe3761e8b34f1ea19436d809609c3e56e4caaa78ab1",
}
PROFILE = {
    "CONFIG_CARECALL_DEVICE_ID": "button02",
    "CONFIG_CARECALL_MQTT_USERNAME": "button02",
    "CONFIG_CARECALL_MQTT_CLIENT_ID": "carecall-button02",
}
MQTT_KEY = "CONFIG_CARECALL_MQTT_PASSWORD"
AP_KEY = "CONFIG_CARECALL_SETUP_AP_PASSWORD"
CHANGED_KEYS = set(PROFILE) | {MQTT_KEY, AP_KEY}


class Stop(Exception):
    """Safe diagnostic with no credential contents."""


def require(condition, code):
    if not condition:
        raise Stop(code)


def digest(path):
    with path.open("rb") as stream:
        result = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def no_link(path):
    info = path.lstat()
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
    require(directory.is_dir(), "REQUIRED_SOURCE_DIRECTORY_MISSING")
    no_link(directory)
    found = []
    for entry in sorted(directory.iterdir()):
        no_link(entry)
        if entry.is_dir():
            found.extend(tree_files(entry))
        else:
            found.append(entry)
    return found


def parse_config(raw):
    text = raw.decode("utf-8-sig", errors="strict")
    values = {}
    for line in text.splitlines():
        match = re.fullmatch(r"(CONFIG_[A-Z0-9_]+)=(.*)", line)
        if match:
            key, value = match.groups()
            require(key not in values, "DUPLICATE_CONFIG_KEY:" + key)
            values[key] = value
    return text, values


def string_value(values, key):
    require(key in values, "CONFIG_MISSING:" + key)
    try:
        value = json.loads(values[key])
    except (ValueError, TypeError):
        raise Stop("CONFIG_FORMAT_ERROR:" + key) from None
    require(isinstance(value, str), "CONFIG_NOT_STRING:" + key)
    return value


def source_info(source):
    source = safe_path(source)
    require(source.is_dir(), "SOURCE_NOT_DIRECTORY")
    for name in ("components", "sdkconfig.defaults", "sdkconfig.defaults.esp32c3"):
        require(not os.path.lexists(source / name), "UNREVIEWED_BUILD_INPUT:" + name)
    actual_main = {p.relative_to(source).as_posix() for p in tree_files(source / "main")}
    require(actual_main == {x for x in EXPECTED if x.startswith("main/")},
            "SOURCE_MAIN_FILE_LIST_CHANGED")
    for name, expected in EXPECTED.items():
        path = source / name
        no_link(path)
        require(digest(path) == expected, "SOURCE_DIFF:" + name)
    config_path = source / "sdkconfig"
    stage_path = source / "carecall_wifi_stage.json"
    no_link(config_path)
    no_link(stage_path)
    raw = config_path.read_bytes()
    _, values = parse_config(raw)
    for key, wanted in PROFILE.items():
        require(string_value(values, key) == wanted.replace("button02", "button01"),
                "SOURCE_NOT_BUTTON01:" + key)
    for key in (MQTT_KEY, "CONFIG_CARECALL_WIFI_SSID", "CONFIG_CARECALL_WIFI_PASSWORD"):
        require(bool(string_value(values, key)), "EMPTY_REQUIRED_SETTING:" + key)
    require(re.fullmatch(r"[A-Za-z0-9]{16,63}", string_value(values, AP_KEY)) is not None,
            "SOURCE_AP_PASSWORD_INVALID")
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
        require(values.get(key) == wanted, "UNEXPECTED_SETTING:" + key)
    for key in ("CONFIG_SECURE_BOOT", "CONFIG_FLASH_ENCRYPTION_ENABLED", "CONFIG_NVS_ENCRYPTION"):
        require(values.get(key) != "y", "SECURITY_LAYOUT_CHANGED:" + key)
    stage = json.loads(stage_path.read_text(encoding="utf-8-sig"))
    require(stage.get("version") == "20260930-esp32-wifi-1", "SOURCE_STAGE_VERSION_CHANGED")
    require(stage.get("portal_patch_version") == "20260930-httpaddr-2", "SOURCE_PORTAL_PATCH_CHANGED")
    managed = tree_files(source / "managed_components")
    require(bool(managed), "MANAGED_COMPONENTS_EMPTY")
    selected = [source / name for name in EXPECTED] + managed
    tracked = selected + [config_path, stage_path]
    hashes = {p.relative_to(source).as_posix(): digest(p) for p in tracked}
    return source, raw, values, stage, selected, hashes


def new_password(label):
    require(sys.stdin.isatty(), "REAL_CONSOLE_REQUIRED_FOR_HIDDEN_INPUT")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        first = getpass.getpass(label + " (16-63 ASCII letters/digits): ")
        second = getpass.getpass("Confirm " + label + ": ")
    require(first == second, "PASSWORD_CONFIRMATION_MISMATCH")
    require(re.fullmatch(r"[A-Za-z0-9]{16,63}", first) is not None,
            "NEW_PASSWORD_MUST_BE_16_TO_63_ASCII_LETTERS_OR_DIGITS")
    return first


def replace_profile(raw, replacements):
    require(set(replacements) == CHANGED_KEYS, "UNEXPECTED_PROFILE_KEYS")
    require(all(replacements[key] == value for key, value in PROFILE.items()),
            "BUTTON02_IDENTITY_REQUIRED")
    _, original = parse_config(raw)
    updated = raw
    for key, value in replacements.items():
        pattern = rb"(?m)^" + key.encode("ascii") + rb"=[^\r\n]*"
        replacement = key.encode("ascii") + b"=" + json.dumps(value).encode("ascii")
        updated, count = re.subn(pattern, lambda unused: replacement, updated)
        require(count == 1, "CONFIG_REPLACEMENT_FAILED:" + key)
    _, changed = parse_config(updated)
    actual = {key for key in original.keys() | changed.keys() if original.get(key) != changed.get(key)}
    # Identity must change; either password may retain its original value.
    require(set(PROFILE) <= actual <= CHANGED_KEYS, "UNEXPECTED_CONFIG_DIFFERENCE")
    for key, value in replacements.items():
        require(string_value(changed, key) == value, "PROFILE_VALUE_MISMATCH:" + key)
    # All bytes outside these five value lines must be identical.
    reverted = updated
    for key in replacements:
        pattern = rb"(?m)^" + key.encode("ascii") + rb"=[^\r\n]*"
        line = (key + "=" + original[key]).encode("utf-8")
        reverted = re.sub(pattern, lambda unused, line=line: line, reverted)
    require(reverted == raw, "UNRELATED_CONFIG_BYTES_CHANGED")
    return updated


def prepare(source, destination, password_reader=new_password):
    source, raw, values, stage, selected, before = source_info(source)
    destination = Path(os.path.abspath(destination))
    parent = safe_path(destination.parent)
    require(parent == source.parent and destination.name == "carecall_esp32_button02",
            "DESTINATION_MUST_BE_BUTTON02_SIBLING")
    require(not os.path.lexists(destination), "DESTINATION_ALREADY_EXISTS_NO_OVERWRITE")
    mqtt = password_reader("button02 MQTT password")
    ap = password_reader("button02 setup AP password")
    require(all(isinstance(p, str) and re.fullmatch(r"[A-Za-z0-9]{16,63}", p) for p in (mqtt, ap)),
            "NEW_PASSWORD_FORMAT_INVALID")
    replacements = {**PROFILE, MQTT_KEY: mqtt, AP_KEY: ap}
    updated = replace_profile(raw, replacements)
    new_values = parse_config(updated)[1]
    actual_changed_keys = sorted(key for key in CHANGED_KEYS if values[key] != new_values[key])
    destination.mkdir(mode=0o700)  # Atomic refusal if someone created it meanwhile.
    try:
        for original in selected:
            relative = original.relative_to(source)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            no_link(original)
            shutil.copyfile(original, target)
            require(digest(target) == before[relative.as_posix()], "COPY_VERIFY_FAILED")
        config = destination / "sdkconfig"
        with config.open("xb") as handle:
            handle.write(updated)
        os.chmod(config, 0o600)
        require(config.read_bytes() == updated, "NEW_CONFIG_VERIFY_FAILED")
        # Repeat the source gate to catch concurrent edits and input-list changes.
        after = source_info(source)[5]
        require(after == before, "SOURCE_CHANGED_DURING_PREPARATION")
        record = {
            "version": VERSION,
            "phase": "profile_prepared_not_built",
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "device_id": "button02",
            "mqtt_username": "button02",
            "mqtt_client_id": "carecall-button02",
            "source_project": str(source),
            "idf_root_hint": stage.get("idf_root"),
            "source_hashes": before,
            "profile_sdkconfig_sha256": digest(config),
            "configured_config_keys": sorted(CHANGED_KEYS),
            "changed_config_keys": actual_changed_keys,
            "password_reuse_allowed": True,
            "hardware_identity_verified": False,
            "build_verified": False,
            "pi_registered": False,
            "flash_requested": False,
            "nvs_erase_requested": False,
        }
        with (destination / MARKER).open("x", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, ensure_ascii=True)
            handle.write("\n")
    except BaseException:
        print("INCOMPLETE_DESTINATION_LEFT_IN_PLACE=YES")
        print("DO_NOT_BUILD_OR_FLASH_INCOMPLETE_PROJECT=YES")
        raise
    print("SOURCE_SNAPSHOT_MATCH=YES")
    print("BUTTON01_INPUTS_UNCHANGED=YES")
    print("BUTTON02_DEVICE_ID=button02")
    print("BUTTON02_MQTT_USERNAME=button02")
    print("BUTTON02_MQTT_CLIENT_ID=carecall-button02")
    print("CONFIGURED_CONFIG_KEYS=5")
    print("CHANGED_CONFIG_KEYS=" + str(len(actual_changed_keys)))
    print("COMMON_SOURCE_COPY_VERIFIED=YES")
    print("OTHER_CONFIG_BYTES_PRESERVED=YES")
    print("OLD_BUILD_OUTPUTS_COPIED=NO")
    print("OLD_STAGE_AND_BUILD_MARKERS_COPIED=NO")
    print("PASSWORD_VALUES_PRINTED=NO")
    print("SDKCONFIG_CONTAINS_PLAINTEXT_CREDENTIALS=YES")
    print("BUTTON02_PROJECT=" + str(destination))
    print("BUTTON02_PREPARE=PASS")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--prepare", action="store_true", help="Create sibling button02 project")
    args = parser.parse_args()
    print("TOOL_VERSION=" + VERSION)
    print("PASSWORD_REUSE_ALLOWED=YES")
    try:
        if args.prepare:
            prepare(args.source, args.source.parent / "carecall_esp32_button02")
        else:
            source_info(args.source)
            print("SOURCE_SNAPSHOT_MATCH=YES")
            print("BUTTON02_SOURCE_CHECK=PASS")
            print("PROJECT_CREATION_REQUESTED=NO")
        return 0
    except (Exception, KeyboardInterrupt) as error:
        code = str(error) if isinstance(error, Stop) else type(error).__name__
        print("BUTTON02_PREPARE=FAILED")
        print("ERROR_CODE=" + code)
        print("DO_NOT_BYPASS_CHECKS=YES")
        return 1
    finally:
        print("BUILD_REQUESTED=NO")
        print("FLASH_WRITE_REQUESTED=NO")
        print("NVS_ERASE_REQUESTED=NO")
        print("PI_CHANGE_REQUESTED=NO")


if __name__ == "__main__":
    raise SystemExit(main())
