#!/usr/bin/env python3
"""User-side vault export/validation used by the one-way clean installer."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="create or validate a device-local GP clean-install vault")
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from gp_control_plane.backups import clean_install_vault_info, create_clean_install_vault
    info = clean_install_vault_info(target_home=args.home) if args.verify else create_clean_install_vault(args.state_dir, target_home=args.home)
    if not info.get("pending") or not info.get("vault_id"):
        raise RuntimeError("clean-install vault is not ready")
    print(f"status=ready vault_id={info['vault_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
