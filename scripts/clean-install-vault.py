#!/usr/bin/env python3
"""User-side vault export, validation, and verified restore for clean install."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="create or validate a device-local GP clean-install vault")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--home", required=True, type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--verify", action="store_true")
    action.add_argument("--restore", action="store_true")
    parser.add_argument("--target-state-dir", type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from gp_control_plane.backups import (
        clean_install_vault_info,
        create_clean_install_vault,
        restore_clean_install_vault,
    )
    if args.restore:
        if args.target_state_dir is None:
            parser.error("--restore requires --target-state-dir")
        info = clean_install_vault_info(target_home=args.home)
        vault_id = info.get("vault_id")
        if not info.get("pending") or not isinstance(vault_id, str):
            raise RuntimeError("clean-install vault is not ready")
        restored = restore_clean_install_vault(args.target_state_dir, target_home=args.home, vault_id=vault_id)
        if not (
            restored.get("completed")
            and restored.get("verification", {}).get("verified")
            and restored.get("storage_status", {}).get("ready")
            and restored.get("storage_status", {}).get("integrity_check") == "ok"
            and restored.get("cleanup", {}).get("source_deleted")
        ):
            raise RuntimeError("clean-install vault restore verification failed")
        print(f"status=restored vault_id={vault_id}")
        return 0
    if args.verify:
        info = clean_install_vault_info(target_home=args.home)
    else:
        if args.state_dir is None:
            parser.error("vault export requires --state-dir")
        create_clean_install_vault(args.state_dir, target_home=args.home)
        info = clean_install_vault_info(target_home=args.home)
    if not info.get("pending") or not info.get("vault_id"):
        raise RuntimeError("clean-install vault is not ready")
    print(f"status=ready vault_id={info['vault_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
