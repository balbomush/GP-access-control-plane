from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.config import build_config


class ConfigTests(unittest.TestCase):
    def test_build_config_defaults_to_build_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cwd = Path(raw)
            with patch.dict(os.environ, {}, clear=True), patch("pathlib.Path.cwd", return_value=cwd):
                config = build_config()

            self.assertEqual(config.output.state_dir, (cwd / "build" / "state").resolve())

    def test_build_config_uses_gp_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "custom-state"
            with patch.dict(os.environ, {"GP_STATE_DIR": str(state_dir)}, clear=True):
                config = build_config()

            self.assertEqual(config.output.state_dir, state_dir)

    def test_build_config_argument_overrides_environment(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            selected = Path(raw) / "selected"
            env_state = Path(raw) / "env"
            with patch.dict(os.environ, {"GP_STATE_DIR": str(env_state)}, clear=True):
                config = build_config(selected)

            self.assertEqual(config.output.state_dir, selected)


if __name__ == "__main__":
    unittest.main()
