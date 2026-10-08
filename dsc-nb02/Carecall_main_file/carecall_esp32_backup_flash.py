#!/usr/bin/env python3
"""CareCall ESP32-C3: verified 4MB backup -> flash the verified build -> check NVS/PHY -> reboot.

Requires the existing esptool 4.12.0 Python environment and the previously supplied
CareCall_ESP32_WiFi_Setup_20260930 package. No package installation or rebuild.
Backups and firmware contain credentials: keep them on the owner's PC.
Never uses erase_flash, erase_region, --erase-all, --force, or a merged flash image.
Do not unplug/reboot the device or run another serial program during execution.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile

VERSION = "20260930-esp32-backup-flash-1"
FIRMWARE_VERSION = "20260930-esp32-wifi-1"
PACKAGE_HASHES = {
    "stage.py": "44c59be27fe6debf1d6331cedcff0431a2a89c8c9f2f82936144c5492d710e54",
    "baseline.py": "681ed1b363a086c130ff1b86a6557002e4311f4750914dac8ddc78229f2a4fb0",
    "verify_build.py": "85c0181a55c366d7e04ac8c1438086229f2effe78908928fd73be63bef7973cb",
}
FILES = {"bootloader/bootloader.bin": (0, 0x8000),
         "partition_table/partition-table.bin": (0x8000, 0x9000),
         "carecall_esp32.bin": (0x10000, 0x210000)}
FLASH_BYTES = 0x400000


def require(condition, code):
    if not condition:
        raise RuntimeError(code)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def emit(key, value):
    print(str(key) + "=" + str(value), flush=True)


def partition_layout(data):
    require(len(data) == 4096, "PARTITION_SECTOR_LENGTH_INVALID")
    entries = []
    verified = False
    for offset in range(0, len(data), 32):
        block = data[offset:offset + 32]
        if block[:2] == b"\xeb\xeb":
            require(block[2:16] == b"\xff" * 14 and hashlib.md5(data[:offset]).digest() == block[16:],
                    "DEVICE_PARTITION_MD5_FAILED")
            require(all(value == 255 for value in data[offset + 32:]), "DEVICE_PARTITION_TRAILING_DATA")
            verified = True
            break
        if block == b"\xff" * 32:
            break
        magic, typ, subtype, start, size, label, flags = struct.unpack("<HBBII16sI", block)
        require(magic == 0x50aa, "DEVICE_PARTITION_ENTRY_INVALID")
        entries.append((typ, subtype, start, size, flags))
    require(verified, "DEVICE_PARTITION_CHECKSUM_REQUIRED")
    require(len(entries) == 3 and entries[:2] == [(1, 2, 0x9000, 0x6000, 0), (1, 1, 0xf000, 0x1000, 0)]
            and entries[2] in [(0, 0, 0x10000, 0x100000, 0), (0, 0, 0x10000, 0x200000, 0)],
            "DEVICE_PARTITION_LAYOUT_REQUIRES_REVIEW")
    return entries[2][3]


def validated_images(project, record):
    require(record.get("version") == FIRMWARE_VERSION and set(record.get("files", {})) == set(FILES),
            "BUILD_VERIFICATION_RECORD_INVALID")
    images = {}
    for name, (offset, limit) in FILES.items():
        item = record["files"][name]
        data = (project / "build_wifi_setup" / name).read_bytes()
        require(item.get("offset") == offset and item.get("size") == len(data) and
                item.get("sha256") == sha(data), "BUILD_IMAGE_CHANGED_SINCE_VERIFICATION")
        end = offset + ((len(data) + 4095) // 4096) * 4096
        require(data and end <= limit and not (offset < 0x10000 and end > 0x9000), "FLASH_RANGE_INVALID")
        images[name] = data
    table = images["partition_table/partition-table.bin"]
    require(len(table) <= 4096 and partition_layout(table.ljust(4096, b"\xff")) == 0x200000,
            "NEW_PARTITION_LAYOUT_INVALID")
    return images


def find_package(package_base):
    require(package_base.is_dir(), "PACKAGE_BASE_NOT_FOUND")
    candidates = []
    for path in package_base.rglob("verify_build.py"):
        package = path.parent
        if all((package / name).is_file() and sha((package / name).read_bytes()) == expected
