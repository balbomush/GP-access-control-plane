from __future__ import annotations

import hashlib
import importlib.util
import io
import os
from pathlib import Path
import struct
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prepare_zapret2", ROOT / "scripts/prepare-zapret2.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ZapretReleaseTests(unittest.TestCase):
    def archive(self, root, entries):
        archive = root / "release.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            for name, kind, content in entries:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.mode = 0o7777
                if kind == tarfile.REGTYPE:
                    member.size = len(content)
                    bundle.addfile(member, io.BytesIO(content))
                else:
                    member.linkname = content.decode()
                    bundle.addfile(member)
        return archive, hashlib.sha256(archive.read_bytes()).hexdigest()

    def test_integrity_is_archive_digest_and_extraction_strips_special_permissions(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive, digest = self.archive(root, [(release.TOP + "/test.sh", tarfile.REGTYPE, b"hello")])
            with self.assertRaisesRegex(ValueError, "checksum"):
                release.unpack(archive, root / "bad")
            self.assertFalse((root / "bad").exists())
            release.unpack(archive, root / "good", digest)
            self.assertEqual((root / "good/test.sh").read_bytes(), b"hello")
            if os.name == "posix":
                self.assertEqual((root / "good/test.sh").stat().st_mode & 0o7777, 0o755)

    def test_entire_archive_checked_before_any_extraction(self):
        bad_members = [
            ("/tmp/escape", tarfile.REGTYPE, b"bad"),
            (release.TOP + "/../escape", tarfile.REGTYPE, b"bad"),
            (release.TOP + "/link", tarfile.SYMTYPE, b"../outside"),
            (release.TOP + "/hard", tarfile.LNKTYPE, b"outside"),
            (release.TOP + "/device", tarfile.CHRTYPE, b""),
            ("wrong-root/file", tarfile.REGTYPE, b"bad"),
            (release.TOP + "/file", tarfile.REGTYPE, b"duplicate"),
        ]
        for member in bad_members:
            with self.subTest(member=member), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                archive, digest = self.archive(root, [(release.TOP + "/file", tarfile.REGTYPE, b"safe"), member])
                with self.assertRaises(ValueError):
                    release.unpack(archive, root / "dest", digest)
                self.assertFalse((root / "dest").exists())

    def test_valid_digest_does_not_make_invalid_gzip_a_valid_archive(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); archive = root / "bad.gz"; archive.write_bytes(b"not gzip")
            with self.assertRaises(tarfile.TarError):
                release.unpack(archive, root / "dest", hashlib.sha256(archive.read_bytes()).hexdigest())
            self.assertFalse((root / "dest").exists())

    def test_unsupported_architecture_and_missing_runtime_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(ValueError, "architecture"):
                release.prepare_runtime(Path(raw), "unknown")
            with self.assertRaisesRegex(ValueError, "missing.*blockcheck"):
                release.prepare_runtime(Path(raw), "aarch64")

    @unittest.skipUnless(os.name == "posix", "executable ELF and symlink behavior requires Linux")
    def test_native_selection_reuse_and_foreign_content_preserved(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for name in ("blockcheck2.sh", "config.default", "common/base.sh", "common/dialog.sh",
                         "common/elevate.sh", "common/fwtype.sh", "common/virt.sh",
                         "lua/zapret-lib.lua", "lua/zapret-antidpi.lua", "blockcheck2.d/standard/def.inc"):
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("#!/bin/sh\n", encoding="utf-8"); path.chmod(0o755)
            for name in ("nfqws2", "mdig", "ip2net"):
                path = root / "binaries/linux-arm64" / name; path.parent.mkdir(parents=True, exist_ok=True)
                header = bytearray(20); header[:6] = b"\x7fELF\x02\x01"; struct.pack_into("<H", header, 18, 183)
                path.write_bytes(header); path.chmod(0o755)
            release.prepare_runtime(root, "aarch64")
            self.assertEqual((root / "config").read_bytes(), (root / "config.default").read_bytes())
            self.assertEqual(os.readlink(root / "nfq2/nfqws2"), "../binaries/linux-arm64/nfqws2")
            (root / "foreign.cfg").write_text("keep")
            # Test ownership rejection independently of the test process UID.
            if os.getuid() != 0:
                with self.assertRaisesRegex(ValueError, "root protected"):
                    release.verify_installed(root, root)
            self.assertEqual((root / "foreign.cfg").read_text(), "keep")

    def test_runtime_exec_failure_is_not_success(self):
        with patch.object(release.subprocess, "run", side_effect=OSError("cannot execute")):
            with self.assertRaisesRegex(OSError, "cannot execute"):
                release.probe(Path("/fixture"))

    def test_dry_run_passes_queue_parameter_without_starting_interception(self):
        result = SimpleNamespace(stdout="127.0.0.1\n")
        with patch.object(release.subprocess, "run", return_value=result) as run:
            release.probe(Path("/fixture"))
        dry_run = run.call_args_list[1].args[0]
        self.assertIn("--dry-run", dry_run)
        self.assertIn("--qnum=0", dry_run)

    def test_reuse_checks_additional_active_inputs_and_preserves_foreign_files(self):
        # Only UID/mode metadata is modelled; reads and traversal are real. The
        # actual root-owned positive case also runs in the Linux fixture gate.
        original = Path.lstat
        def trusted_stat(path):
            info = original(path)
            return SimpleNamespace(st_uid=0, st_mode=info.st_mode & ~0o022)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); prepared = root / "prepared"; installed = root / "installed"
            prepared.mkdir(); installed.mkdir()
            for path in (prepared / "release.txt", installed / "release.txt"):
                path.write_text("qualified")
            with patch.object(Path, "lstat", trusted_stat):
                release.verify_installed(prepared, installed)
                (installed / "foreign.txt").write_text("keep")
                release.verify_installed(prepared, installed)
                for relative in ("config", "custom.sh", "custom.lua", "blockcheck2.d/standard/foreign.inc"):
                    target = installed / relative; target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("foreign active input")
                    with self.assertRaisesRegex(ValueError, "unexpected active"):
                        release.verify_installed(prepared, installed)
                    self.assertEqual(target.read_text(), "foreign active input")
                    target.unlink()
                    if relative.startswith("blockcheck2.d"):
                        target.parent.rmdir(); target.parent.parent.rmdir()
                self.assertEqual((installed / "foreign.txt").read_text(), "keep")

    def test_reuse_rejects_user_writable_extra_without_deleting_it(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); prepared = root / "prepared"; installed = root / "installed"
            prepared.mkdir(); installed.mkdir(); extra = installed / "config"
            extra.write_text("untrusted"); extra.chmod(0o666)
            with self.assertRaisesRegex(ValueError, "root protected"):
                release.verify_installed(prepared, installed)
            self.assertEqual(extra.read_text(), "untrusted")


if __name__ == "__main__":
    unittest.main()
