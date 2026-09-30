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
               for name, expected in PACKAGE_HASHES.items()):
            candidates.append(package.resolve())
    require(candidates, "KNOWN_SETUP_PACKAGE_NOT_FOUND")
    # Multiple byte-identical verifiers are harmless; the verifier checks its full package manifest.
    return sorted(set(candidates), key=str)[0]


class Upload:
    def __init__(self, port, baud=460800):
        self.port, self.baud = port, baud
        self.directory = None
        self.flash_attempted = False
        self.flash_completed = False
        self.backup_verified = False
        self.storage_preserved = False
        self.reboot_requested = False

    def tool(self, step, arguments, after="no_reset"):
        emit("ESPTOOL_STEP", step)
        environment = os.environ.copy()
        environment["ESPTOOL_OPEN_PORT_ATTEMPTS"] = "30"
        result = subprocess.run([sys.executable, "-I", "-B", "-m", "esptool", "--chip", "esp32c3",
                                 "--port", self.port, "--baud", str(self.baud), "--before", "default_reset",
                                 "--after", after] + arguments, env=environment)
        require(result.returncode == 0, step + "_FAILED")

    def report(self, result, error=None):
        if self.directory is not None:
            data = {"version": VERSION, "result": result, "error": error, "port": self.port,
                    "backup_verified": self.backup_verified, "flash_write_attempted": self.flash_attempted,
                    "flash_write_completed": self.flash_completed, "nvs_and_phy_preserved": self.storage_preserved,
                    "firmware_restart_requested": self.reboot_requested}
            (self.directory / "upload_result.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def execute(self, project, package_base, backup_root):
        emit("VERSION", VERSION)
        installed = importlib.metadata.version("esptool")
        emit("ESPTOOL_VERSION", installed)
        require(installed == "4.12.0", "EXPECTED_ESPTOOL_4_12_0_REQUIRED")
        project = project.resolve()
        package = find_package(package_base.resolve())
        record_path = project / "carecall_wifi_build_verified.json"
        before = json.loads(record_path.read_text(encoding="utf-8"))
        validated_images(project, before)
        emit("BUILD_RECHECK", "BEGIN")
        check = subprocess.run([sys.executable, "-I", "-B", str(package / "verify_build.py"), "--project", str(project)])
        require(check.returncode == 0, "BUILD_RECHECK_FAILED")
        after = json.loads(record_path.read_text(encoding="utf-8"))
        require(before == after, "BUILD_RECORD_CHANGED_DURING_RECHECK")
        images = validated_images(project, after)
        emit("BUILD_RECHECK", "PASS")
        backup_root = backup_root.resolve()
        backup_root.mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix="wifi_upload_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") ,dir=backup_root))
        emit("BACKUP_DIR", self.directory)
        emit("BACKUP_AND_IMAGES_CONTAIN_SECRETS", "KEEP_ON_PC_DO_NOT_UPLOAD")
        # Use an exact snapshot even if a parallel editor/build later changes the project.
        snapshot = self.directory / "new_images"
        for name, data in images.items():
            path = snapshot / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            require(sha(path.read_bytes()) == sha(data), "SNAPSHOT_WRITE_FAILED")
        (self.directory / "build_verified.json").write_text(json.dumps(after, indent=2) + "\n", encoding="utf-8")
        partial = self.directory / "flash_before_4MB.bin.partial"
        self.tool("READ_FULL_BACKUP", ["read_flash", "--flash_size", "keep", "0x0", "ALL", str(partial)])
        require(partial.stat().st_size == FLASH_BYTES, "DEVICE_FLASH_SIZE_IS_NOT_4MB")
        full = partial.read_bytes()
        existing_app_size = partition_layout(full[0x8000:0x9000])
        require(full[0] == 0xe9 and full[0x10000] == 0xe9, "EXISTING_FIRMWARE_HEADER_INVALID")
        # Explicit keep options prevent environment defaults from modifying the comparison image.
        self.tool("VERIFY_FULL_BACKUP", ["verify_flash", "--flash_mode", "keep", "--flash_freq", "keep",
                                         "--flash_size", "keep", "0x0", str(partial)])
        require(sha(partial.read_bytes()) == sha(full), "BACKUP_FILE_CHANGED")
        backup = self.directory / "flash_before_4MB.bin"
        partial.rename(backup)
        (self.directory / "flash_before_4MB.sha256").write_text(sha(full) + "  " + backup.name + "\n", encoding="ascii")
        self.backup_verified = True
        emit("FULL_BACKUP_BYTES", FLASH_BYTES)
        emit("FULL_BACKUP_VERIFY", "PASS")
        emit("DEVICE_APP_PARTITION_BYTES_BEFORE", existing_app_size)
        emit("DEVICE_NVS_LAYOUT", "EXPECTED_24K_AT_0x9000")
        self.report("BACKUP_READY")
        args = ["write_flash", "--flash_mode", "dio", "--flash_size", "4MB", "--flash_freq", "80m"]
        for name, (offset, _) in FILES.items():
            path = snapshot / name
            require(sha(path.read_bytes()) == after["files"][name]["sha256"], "SNAPSHOT_CHANGED_BEFORE_FLASH")
            args.extend([hex(offset), str(path)])
        self.flash_attempted = True
        self.report("FLASHING")
        self.tool("WRITE_VERIFIED_IMAGES", args)
        self.flash_completed = True
        # The application stays stopped until the original data sectors are compared.
        storage = self.directory / "nvs_phy_after.bin"
        self.tool("READ_STORAGE_AFTER_FLASH", ["read_flash", "--flash_size", "keep", "0x9000", "0x7000", str(storage)])
        require(storage.read_bytes() == full[0x9000:0x10000], "NVS_OR_PHY_CHANGED_DO_NOT_REBOOT")
        self.storage_preserved = True
        emit("NVS_AND_PHY_PRESERVED", "YES")
        self.report("FLASH_VERIFIED_BEFORE_REBOOT")
        # A read-only command followed by documented hard_reset starts the new firmware.
        self.tool("START_FIRMWARE", ["read_mac"], after="hard_reset")
        self.reboot_requested = True
        self.report("SUCCESS")
        emit("ESP32_WIFI_UPLOAD", "SUCCESS")
        emit("DEVICE_RESTART_REQUESTED", "YES")
        emit("HARDWARE_AP_TEST", "NOT_RUN")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--package-base", type=Path, required=True)
    parser.add_argument("--port", required=True)
    parser.add_argument("--backup-root", type=Path, default=Path.home() / "CareCall_ESP32_Backups")
    parser.add_argument("--baud", type=int, choices=(115200, 460800), default=460800)
    args = parser.parse_args()
    upload = Upload(args.port.strip().upper(), args.baud)
    try:
        require(re.fullmatch(r"COM[1-9][0-9]*", upload.port) is not None, "EXPECTED_WINDOWS_COM_PORT")
        upload.execute(args.project, args.package_base, args.backup_root)
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        code = str(exc) if type(exc) is RuntimeError and re.fullmatch(r"[A-Z0-9_]+", str(exc)) else type(exc).__name__
        try:
            upload.report("FAILED", code)
        except Exception:
            pass
        emit("ESP32_WIFI_UPLOAD", "FAILED code=" + code)
        emit("FLASH_WRITE_ATTEMPTED", "YES" if upload.flash_attempted else "NO")
        emit("FLASH_WRITE_COMPLETED", "YES" if upload.flash_completed else "NO")
        emit("DEVICE_RESTART_REQUESTED", "YES" if upload.reboot_requested else "NO")
        emit("NEXT_ACTION", "KEEP_BACKUP_PRIVATE_AND_SHARE_TERMINAL_OUTPUT")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
