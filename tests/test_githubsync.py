from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.githubsync import GitError, ensure_clean


class GitSyncTests(unittest.TestCase):
    @unittest.skipIf(shutil.which("git") is None, "git CLI is not available")
    def test_dirty_working_tree_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw) / "repo"
            repo.mkdir()
            subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True, text=True)
            (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")

            with self.assertRaises(GitError):
                ensure_clean(repo)


if __name__ == "__main__":
    unittest.main()
