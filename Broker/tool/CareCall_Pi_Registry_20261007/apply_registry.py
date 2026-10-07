#!/usr/bin/env python3
"""Install phase-1 registry files and add registry tables; no service restart."""
import argparse
from contextlib import closing
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
PAYLOAD = ("device_registry.py", "device_admin.py")


class InstallError(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise InstallError(code)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plain(path):
    require(path.is_file() and not path.is_symlink(), "FILE_MISSING_OR_LINK:" + path.name)
    return path


def package_check():
    expected = set()
    for line in plain(ROOT / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        value, name = line.split("  ", 1)
        relative = Path(name)
        require(not relative.is_absolute() and ".." not in relative.parts and
                "\\" not in name and name not in expected, "INVALID_MANIFEST_PATH")
        path = plain(ROOT / name)
        require(path.resolve().is_relative_to(ROOT), "PACKAGE_PATH_OUTSIDE_ROOT")
        require(digest(path) == value, "PACKAGE_HASH_MISMATCH:" + name)
        expected.add(name)
    require({"apply_registry.py", "baseline_manifest.json", "README_KO.md",
             "app/device_registry.py", "app/device_admin.py", "tests/test_device_registry.py"}
            <= expected, "INCOMPLETE_PACKAGE_MANIFEST")


def registry_module():
    spec = importlib.util.spec_from_file_location("carecall_phase1_registry",
                                                ROOT / "app/device_registry.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_owner(project):
    require(os.geteuid() != 0 and project.stat().st_uid == os.geteuid(),
            "RUN_AS_PROJECT_OWNER_WITHOUT_SUDO")


def inspect_install(project, registry):
    require(project.is_dir() and not project.is_symlink(), "PROJECT_DIRECTORY_REQUIRED")
    check_owner(project)
    require(sys.version_info >= (3, 10), "PYTHON_3_10_OR_NEWER_REQUIRED")
    require(sqlite3.sqlite_version_info >= (3, 37, 0), "SQLITE_STRICT_TABLE_SUPPORT_REQUIRED")
    baseline = json.loads(plain(ROOT / "baseline_manifest.json").read_text(encoding="utf-8"))
    for name, expected in baseline["files"].items():
        relative = Path(name)
        require(not relative.is_absolute() and ".." not in relative.parts,
                "INVALID_BASELINE_MANIFEST")
        path = plain(project / name)
        require(path.resolve().is_relative_to(project.resolve()), "BASELINE_PATH_OUTSIDE_PROJECT")
        require(digest(path) == expected, "RUNNING_SOURCE_DIFFERS:" + name)
    for name in PAYLOAD:
        target = project / name
        if target.exists() or target.is_symlink():
            require(digest(plain(target)) == digest(ROOT / "app" / name),
                    "EXISTING_NEW_FILE_DIFFERS:" + name)
    data = project / "data"
    require(data.is_dir() and not data.is_symlink(), "DATA_DIRECTORY_REQUIRED")
    db = plain(data / "carecall_events.db")
    with closing(registry.connect(db, readonly=True)) as c, c:
        c.execute("BEGIN")
        state = registry.inspect(c)
        require([tuple(r) for r in c.execute("PRAGMA quick_check")] == [("ok",)],
                "DATABASE_QUICK_CHECK_FAILED")
    return db, state


def backup_database(db, destination, registry):
    # Exclusive destination creation prevents replacing a previous backup.
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    deadline = time.monotonic() + 30

    def progress(status, remaining, total):
        require(time.monotonic() < deadline, "DATABASE_BACKUP_TIMEOUT")

    try:
        with closing(registry.connect(db, readonly=True)) as source:
            with closing(sqlite3.connect(destination)) as target:
                source.backup(target, pages=128, progress=progress, sleep=0.05)
                require(target.execute("PRAGMA quick_check").fetchall() == [("ok",)],
                        "BACKUP_QUICK_CHECK_FAILED")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    with destination.open("rb") as f:
        os.fsync(f.fileno())


def install_file(source, target):
    if target.exists():
        require(digest(plain(target)) == digest(source), "EXISTING_NEW_FILE_DIFFERS:" + target.name)
        return False
    fd, name = tempfile.mkstemp(prefix=".carecall-registry-", dir=target.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(source.read_bytes())
            f.flush()
            os.fsync(f.fileno())
            os.fchmod(f.fileno(), 0o640)
        # Atomic publication without replacing an existing file (Linux Pi).
        os.link(tmp, target)
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return True
    finally:
        tmp.unlink(missing_ok=True)


def run(project, backup_root, set_id, apply):
    package_check()
    registry = registry_module()
    registry.identifier(set_id)
    project = project.expanduser().absolute()
    db, state = inspect_install(project, registry)
    if state == "installed":
        current = next(d for d in registry.status(db)["devices"] if d["device_id"] == "button01")
        require(current["set_id"] == set_id, "LEGACY_SET_ALREADY_ASSIGNED")
    print("PACKAGE_HASHES=PASS")
    print("PI_BASELINE_SOURCE_HASHES=PASS")
    print("DATABASE_CHECK=PASS")
    print("REGISTRY_BEFORE=" + state)
    if not apply:
        print("REGISTRY_CHECK=PASS")
        print("APPLICATION_FILES_OR_SCHEMA_CHANGED=NO")
        return
    lock_path = project / "data/.carecall-registry-install.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallError("ANOTHER_REGISTRY_INSTALL_IS_RUNNING")
        db, state = inspect_install(project, registry)
        installed_files = all((project / name).is_file() for name in PAYLOAD)
        if state == "installed" and installed_files:
            registry.initialize(db, set_id)
            print("REGISTRY_APPLY=ALREADY_INSTALLED")
        else:
            backup_root = backup_root.expanduser().absolute()
            require(not backup_root.is_symlink(), "BACKUP_ROOT_MUST_NOT_BE_LINK")
            backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            require(backup_root.resolve() != project.resolve() and
                    project.resolve() not in backup_root.resolve().parents,
                    "BACKUP_ROOT_MUST_BE_OUTSIDE_PROJECT")
            backup = Path(tempfile.mkdtemp(prefix="registry_20261007_", dir=backup_root))
            print("BACKUP_DIRECTORY=" + str(backup), flush=True)
            backup_database(db, backup / "carecall_events_before.db", registry)
            (backup / "operation.json").write_text(json.dumps({
                "phase": registry.PHASE, "target": str(project), "set_id": set_id,
                "previous_registry": state, "database_backup": "carecall_events_before.db"
            }, indent=2) + "\n", encoding="utf-8")
            created = []
            try:
                for name in PAYLOAD:
                    target = project / name
                    if install_file(ROOT / "app" / name, target):
                        created.append(target)
                result = registry.initialize(db, set_id)
            except BaseException:
                # A DB failure is rolled back by the registry transaction.
                # Never restore the whole live DB: newer calls may have arrived.
                for target in created:
                    if target.is_file() and digest(target) == digest(ROOT / "app" / target.name):
                        target.unlink()
                raise
            (backup / "result.json").write_text(json.dumps({"result": result}) + "\n", encoding="utf-8")
            print("DATABASE_BACKUP=PASS")
            print("REGISTRY_APPLY=PASS")
        print("LEGACY_DEVICE=button01")
        print("LEGACY_SET=" + set_id)
        print("ADDITIONAL_BUTTON_CALLS_ENABLED=NO")
        print("SERVICE_RESTART_REQUESTED=NO")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path("/home/carecallserver/carecall_receiver"))
    parser.add_argument("--backup-root", type=Path, default=Path.home() / "CareCall_Pi_Backups")
    parser.add_argument("--set-id", default="set01")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        os.umask(0o077)
        run(args.project, args.backup_root, args.set_id, args.apply)
        return 0
    except Exception as exc:
        # RegistryError lives in a deliberately isolated module.
        code = str(exc) if type(exc).__name__ in ("RegistryError", "InstallError") else type(exc).__name__
        print("REGISTRY_INSTALL=FAILED CODE=" + code)
        print("DO_NOT_RESTORE_LIVE_DATABASE_AUTOMATICALLY=YES")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
