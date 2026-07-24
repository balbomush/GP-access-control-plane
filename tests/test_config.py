from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.config import build_config, default_install_dir


class ConfigTests(unittest.TestCase):
    def test_build_config_defaults_to_install_dir_build_state(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch("pathlib.Path.cwd", side_effect=AssertionError("Path.cwd must not be used")):
            config = build_config()

        self.assertEqual(config.install.root_dir, default_install_dir())
        self.assertEqual(config.output.state_dir, (default_install_dir() / "build" / "state").resolve())

    def test_build_config_uses_gp_install_dir_for_default_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            install_dir = Path(raw) / "install"
            with patch.dict(os.environ, {"GP_INSTALL_DIR": str(install_dir)}, clear=True), patch(
                "pathlib.Path.cwd", side_effect=AssertionError("Path.cwd must not be used")
            ):
                config = build_config()

            self.assertEqual(config.install.root_dir, install_dir.resolve())
            self.assertEqual(config.output.state_dir, (install_dir / "build" / "state").resolve())

    def test_build_config_uses_gp_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "custom-state"
            install_dir = Path(raw) / "install"
            with patch.dict(os.environ, {"GP_STATE_DIR": str(state_dir), "GP_INSTALL_DIR": str(install_dir)}, clear=True), patch(
                "pathlib.Path.cwd", side_effect=AssertionError("Path.cwd must not be used")
            ):
                config = build_config()

            self.assertEqual(config.install.root_dir, install_dir.resolve())
            self.assertEqual(config.output.state_dir, state_dir.resolve())

    def test_build_config_argument_overrides_environment(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            selected = Path(raw) / "selected"
            env_state = Path(raw) / "env"
            selected_install = Path(raw) / "selected-install"
            env_install = Path(raw) / "env-install"
            with patch.dict(os.environ, {"GP_STATE_DIR": str(env_state), "GP_INSTALL_DIR": str(env_install)}, clear=True), patch(
                "pathlib.Path.cwd", side_effect=AssertionError("Path.cwd must not be used")
            ):
                config = build_config(selected, install_dir=selected_install)

            self.assertEqual(config.install.root_dir, selected_install.resolve())
            self.assertEqual(config.output.state_dir, selected.resolve())

    def test_build_config_resolves_relative_state_dir_against_install_dir(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            install_dir = Path(raw) / "install"
            with patch.dict(os.environ, {"GP_INSTALL_DIR": str(install_dir), "GP_STATE_DIR": "state"}, clear=True), patch(
                "pathlib.Path.cwd", side_effect=AssertionError("Path.cwd must not be used")
            ):
                config = build_config()

            self.assertEqual(config.output.state_dir, (install_dir / "state").resolve())


if __name__ == "__main__":
    unittest.main()
