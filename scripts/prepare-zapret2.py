#!/usr/bin/env python3
"""Verify and unpack the GP-qualified upstream release without running its installer."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import struct
import subprocess
import tarfile

VERSION = "v1.0.5.2"
ARCHIVE_SHA256 = "fb3bcf69e7d86b9fa2d60bd53c956ac06d9dcc3adf9392afd84865f1d94b1158"
TOP = f"zapret2-{VERSION}"
ARCHITECTURES = {
    "x86_64": ("linux-x86_64", 2, 62), "i686": ("linux-x86", 1, 3),
    "i386": ("linux-x86", 1, 3), "aarch64": ("linux-arm64", 2, 183),
    "armv7l": ("linux-arm", 1, 40), "armv6l": ("linux-arm", 1, 40),
}


def unpack(archive: Path, destination: Path, expected_digest: str = ARCHIVE_SHA256) -> None:
    if archive.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("archive exceeds size limit")
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected_digest:
            raise ValueError("zapret2 archive checksum mismatch")
    # Validate every member before creating any archive paths. No links/devices.
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        seen: set[str] = set()
        total = 0
        for member in members:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or "\\" in member.name
                    or not path.parts or path.parts[0] != TOP
                    or not (member.isdir() or member.isfile())):
                raise ValueError(f"unsafe archive member: {member.name}")
            name = str(path)
            if name in seen:
                raise ValueError(f"duplicate archive member: {name}")
            seen.add(name)
            total += member.size
            if len(seen) > 20000 or total > 256 * 1024 * 1024:
                raise ValueError("expanded archive exceeds size limit")
        destination.mkdir(mode=0o755)
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts[1:])
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                source = bundle.extractfile(member)
                if source is None:
                    raise ValueError(f"unreadable archive member: {member.name}")
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)


def prepare_runtime(root: Path, machine: str) -> None:
    if machine not in ARCHITECTURES:
        raise ValueError(f"unsupported Linux architecture: {machine}")
    directory, elf_class, elf_machine = ARCHITECTURES[machine]
    required = (
        "blockcheck2.sh", "config.default", "common/base.sh", "common/dialog.sh",
        "common/elevate.sh", "common/fwtype.sh", "common/virt.sh",
        "lua/zapret-lib.lua", "lua/zapret-antidpi.lua", "blockcheck2.d/standard/def.inc",
    )
    for name in required:
        if not (root / name).is_file():
            raise ValueError(f"missing zapret2 runtime file: {name}")
    # Native release binaries only: no compilation or binaries/my fallback.
    for name, target_dir in (("nfqws2", "nfq2"), ("mdig", "mdig"), ("ip2net", "ip2net")):
        binary = root / "binaries" / directory / name
        with binary.open("rb") as stream:
            header = stream.read(20)
        if (len(header) < 20 or header[:4] != b"\x7fELF" or header[4] != elf_class
                or header[5] != 1 or struct.unpack_from("<H", header, 18)[0] != elf_machine):
            raise ValueError(f"incompatible release binary: {binary}")
        if not os.access(binary, os.X_OK):
            raise ValueError(f"release binary is not executable: {binary}")
        target = root / target_dir / name
        if target.is_symlink() or target.exists():
            if not target.is_file() or target.is_symlink():
                raise ValueError(f"unsafe release binary destination: {target}")
            target.unlink()
        target.parent.mkdir(exist_ok=True)
        target.symlink_to(f"../binaries/{directory}/{name}")
    if not os.access(root / "blockcheck2.sh", os.X_OK):
        raise ValueError("blockcheck2.sh is not executable")
    subprocess.run(["bash", "-n", str(root / "blockcheck2.sh")], check=True, timeout=15)


def probe(root: Path) -> None:
    subprocess.run([str(root / "nfq2/nfqws2"), "--version"], check=True, timeout=15)
    subprocess.run([str(root / "nfq2/nfqws2"), "--dry-run",
                    f"--lua-init=@{root}/lua/zapret-lib.lua",
                    f"--lua-init=@{root}/lua/zapret-antidpi.lua"], check=True, timeout=15)
    result = subprocess.run([str(root / "ip2net/ip2net")], input="0.0.0.0\n",
                            text=True, capture_output=True, check=True, timeout=15)
    if not result.stdout.strip():
        raise ValueError("ip2net runtime probe returned no output")
    result = subprocess.run([str(root / "mdig/mdig"), "--family=4", "--threads=1"],
                            input="127.0.0.1\n", text=True, capture_output=True,
                            check=True, timeout=15)
    if "127.0.0.1" not in result.stdout.split():
        raise ValueError("mdig runtime probe did not resolve the numeric loopback address")


def verify_installed(prepared: Path, installed: Path) -> None:
    """Reuse matching package-owned files; never replace an existing foreign tree."""
    if installed.is_symlink() or not installed.is_dir():
        raise ValueError("existing managed zapret2 path is unsafe")
    expected = {path.relative_to(prepared) for path in prepared.rglob("*")}
    # Upstream sources config and discovers scripts that are not necessarily in
    # the release member table. Check the installed side too; preserve conflicts
    # for the owner instead of executing or deleting them.
    for target in installed.rglob("*"):
        relative = target.relative_to(installed)
        info = target.lstat()
        if info.st_uid != 0 or (not target.is_symlink() and info.st_mode & 0o022):
            raise ValueError(f"existing managed runtime is not root protected: {target}")
        if relative not in expected:
            if (target.is_symlink() or not (target.is_dir() or target.is_file())
                    or relative.parts[0] in {"common", "lua", "blockcheck2.d", "binaries", "nfq2", "mdig", "ip2net"}
                    or target.name == "config" or target.name.startswith("config.")
                    or target.suffix in {".sh", ".lua"}):
                raise ValueError(f"unexpected active runtime input; preserved for owner: {target}")
    for source in (prepared, *prepared.rglob("*")):
        target = installed / source.relative_to(prepared)
        info = target.lstat()
        if info.st_uid != 0 or (not target.is_symlink() and info.st_mode & 0o022):
            raise ValueError(f"existing managed runtime is not root protected: {target}")
        if source.is_symlink():
            if not target.is_symlink() or os.readlink(target) != os.readlink(source):
                raise ValueError(f"existing managed runtime link differs: {target}")
        elif source.is_dir():
            if target.is_symlink() or not target.is_dir():
                raise ValueError(f"existing managed runtime directory differs: {target}")
        else:
            if target.is_symlink() or not target.is_file():
                raise ValueError(f"existing managed runtime file differs: {target}")
            with source.open("rb") as left, target.open("rb") as right:
                if hashlib.file_digest(left, "sha256").digest() != hashlib.file_digest(right, "sha256").digest():
                    raise ValueError(f"existing managed runtime checksum differs: {target}")
            if (source.stat().st_mode & 0o111) != (info.st_mode & 0o111):
                raise ValueError(f"existing managed runtime mode differs: {target}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--verify-installed", type=Path)
    args = parser.parse_args()
    try:
        if args.verify_installed:
            verify_installed(args.destination, args.verify_installed)
        elif args.probe:
            probe(args.destination)
        else:
            if platform.system() != "Linux" or platform.machine() not in ARCHITECTURES:
                raise ValueError(f"unsupported Linux architecture: {platform.system()}/{platform.machine()}")
            if args.archive is None:
                raise ValueError("--archive is required")
            unpack(args.archive, args.destination)
            prepare_runtime(args.destination, platform.machine())
    except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"ERROR: zapret2 preparation failed: {exc}\n")


if __name__ == "__main__":
    main()
