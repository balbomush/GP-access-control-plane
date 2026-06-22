from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import load_config
from .evidence import write_evidence
from .githubsync import pull_only
from .healthcheck import check_domains_direct, write_report
from .render import render_dry_run
from .rules import extract_hostlist, load_stable_rules
from .state import now_iso
from .strategy_finder import (
    domain_sets,
    find_candidate,
    read_candidates,
    run_custom_verification,
    run_standard_discovery,
)
from .validation import validate_all
from .web.app import serve
from .zapret2 import check_install, list_strategies, run_check


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gp-control-plane")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", default="configs/orchestrator.example.yaml", help="Path to orchestrator config")

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("validate", help="Validate shared and local configuration")

    sync_parser = subparsers.add_parser("sync", help="Pull Git repositories without publishing")
    sync_parser.add_argument("--pull-only", action="store_true", help="Required safety flag")

    render_parser = subparsers.add_parser("render", help="Render local dry-run artifacts")
    render_parser.add_argument("--dry-run", action="store_true", help="Required safety flag")

    health_parser = subparsers.add_parser("healthcheck", help="Check direct access from this host")
    health_parser.add_argument("--direct-only", action="store_true", help="Required safety flag")
    health_parser.add_argument("--domain", action="append", default=[], help="Domain to check; can be repeated")

    evidence_parser = subparsers.add_parser("evidence", help="Write local evidence")
    evidence_subparsers = evidence_parser.add_subparsers(dest="evidence_command", required=True)
    evidence_write = evidence_subparsers.add_parser("write", help="Write evidence without pushing")
    evidence_write.add_argument("--no-push", action="store_true", help="Required safety flag")
    evidence_write.add_argument("--rule-id", default="manual")
    evidence_write.add_argument("--result", choices=["success", "failed", "partial"], default="success")
    evidence_write.add_argument("--checks", type=int, default=0)
    evidence_write.add_argument("--success-rate", type=float, default=0.0)
    evidence_write.add_argument("--network-id")

    zapret_parser = subparsers.add_parser("zapret2", help="Local zapret2 helper commands")
    zapret_subparsers = zapret_parser.add_subparsers(dest="zapret_command", required=True)
    zapret_subparsers.add_parser("check-install", help="Report local zapret2 binaries")
    zapret_subparsers.add_parser("list-local", help="List known local strategy directories")
    zapret_run = zapret_subparsers.add_parser("run-check", help="Run local zapret2 check for one strategy")
    zapret_run.add_argument("--domain", required=True)
    zapret_run.add_argument("--strategy", required=True)
    zapret_run.add_argument("--timeout-seconds", type=int, default=60)

    finder_parser = subparsers.add_parser("strategy-finder", help="Find and verify local zapret2 strategies")
    finder_subparsers = finder_parser.add_subparsers(dest="finder_command", required=True)
    finder_subparsers.add_parser("domains", help="Print built-in test domain sets")
    finder_subparsers.add_parser("candidates", help="Print saved local candidate strategies")
    finder_standard = finder_subparsers.add_parser("standard-discovery", help="Run blockcheck2 standard discovery")
    finder_standard.add_argument("--domain", action="append", default=[], help="Domain to test; can be repeated")
    finder_standard.add_argument("--timeout-seconds", type=int, default=900)
    finder_standard.add_argument("--no-quic", action="store_true")
    finder_custom = finder_subparsers.add_parser("custom-verification", help="Verify a saved candidate with custom list")
    finder_custom.add_argument("--candidate-id", required=True)
    finder_custom.add_argument("--domain", action="append", default=[], help="Domain to test; can be repeated")
    finder_custom.add_argument("--timeout-seconds", type=int, default=300)
    finder_custom.add_argument("--no-quic", action="store_true")

    web_parser = subparsers.add_parser("web", help="Run local Raspberry Pi web UI")
    web_parser.add_argument("--host", default="0.0.0.0")
    web_parser.add_argument("--port", type=int, default=8080)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(_normalize_argv(sys.argv[1:] if argv is None else argv))
    try:
        return _main(args)
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _main(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))

    if args.command == "validate":
        errors = validate_all(config)
        if errors:
            _print_json({"ok": False, "errors": errors})
            return 1
        _print_json({"ok": True, "errors": []})
        return 0

    if args.command == "sync":
        if not args.pull_only:
            raise ValueError("sync requires --pull-only in this MVP")
        pull_only([config.repos.rules, config.repos.strategies])
        _print_json({"ok": True, "pulled": [str(config.repos.rules), str(config.repos.strategies)]})
        return 0

    if args.command == "render":
        if not args.dry_run:
            raise ValueError("render requires --dry-run in this MVP")
        _print_json(render_dry_run(config))
        return 0

    if args.command == "healthcheck":
        if not args.direct_only:
            raise ValueError("healthcheck requires --direct-only in this MVP")
        domains = args.domain or _default_healthcheck_domains(config)
        results = check_domains_direct(domains, timeout_seconds=config.healthcheck.timeout_seconds)
        report = config.output.state_dir / "healthchecks" / f"{now_iso().replace(':', '')}.yaml"
        write_report(report, results)
        _print_json({"report": str(report), "results": [result.to_mapping() for result in results]})
        return 0

    if args.command == "evidence":
        if args.evidence_command != "write":
            raise ValueError("unsupported evidence command")
        if not args.no_push:
            raise ValueError("evidence write requires --no-push in this MVP")
        path = write_evidence(
            config.output.evidence_dir,
            rule_id=args.rule_id,
            result=args.result,
            checks=args.checks,
            success_rate=args.success_rate,
            network_id=args.network_id,
        )
        _print_json({"ok": True, "path": str(path)})
        return 0

    if args.command == "zapret2":
        if args.zapret_command == "check-install":
            _print_json(check_install())
            return 0
        if args.zapret_command == "list-local":
            _print_json({"strategies": [str(path) for path in list_strategies(config.repos.strategies)]})
            return 0
        if args.zapret_command == "run-check":
            result = run_check(args.domain, Path(args.strategy), timeout_seconds=args.timeout_seconds)
            _print_json(
                {
                    "returncode": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
            )
            return 0
        raise ValueError("unsupported zapret2 command")

    if args.command == "strategy-finder":
        if args.finder_command == "domains":
            _print_json(domain_sets())
            return 0
        if args.finder_command == "candidates":
            _print_json({"candidates": read_candidates(config.output.state_dir)})
            return 0
        if args.finder_command == "standard-discovery":
            run = run_standard_discovery(
                args.domain,
                config.output.state_dir,
                timeout_seconds=args.timeout_seconds,
                include_quic=not args.no_quic,
            )
            _print_json(run)
            return 0
        if args.finder_command == "custom-verification":
            candidate = find_candidate(config.output.state_dir, args.candidate_id)
            run = run_custom_verification(
                candidate,
                args.domain,
                config.output.state_dir,
                timeout_seconds=args.timeout_seconds,
                include_quic=not args.no_quic,
            )
            _print_json(run)
            return 0
        raise ValueError("unsupported strategy-finder command")

    if args.command == "web":
        serve(config, host=args.host, port=args.port)
        return 0

    raise ValueError(f"unsupported command: {args.command}")


def _default_healthcheck_domains(config: Any) -> list[str]:
    return [entry for entry in extract_hostlist(load_stable_rules(config.repos.rules)) if not entry.startswith("#")]


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _normalize_argv(argv: list[str]) -> list[str]:
    args = list(argv)
    for index, value in enumerate(args):
        if value == "--config" and index + 1 < len(args):
            pair = args[index : index + 2]
            del args[index : index + 2]
            return pair + args
        if value.startswith("--config="):
            del args[index]
            return [value] + args
    return args


if __name__ == "__main__":
    raise SystemExit(main())
