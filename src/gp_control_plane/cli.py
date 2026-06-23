from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import load_config
from .strategy_finder import (
    domain_sets,
    read_candidates,
    run_multi_domain_discovery,
    run_standard_discovery,
)
from .web.app import serve
from .zapret2 import check_install


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gp-control-plane")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", default="configs/orchestrator.example.yaml", help="Path to orchestrator config")

    subparsers = parser.add_subparsers(dest="command", required=True)

    zapret_parser = subparsers.add_parser("zapret2", help="Local zapret2 helper commands")
    zapret_subparsers = zapret_parser.add_subparsers(dest="zapret_command", required=True)
    zapret_subparsers.add_parser("check-install", help="Report local zapret2 binaries")

    finder_parser = subparsers.add_parser("strategy-finder", help="Find local zapret2 strategies")
    finder_subparsers = finder_parser.add_subparsers(dest="finder_command", required=True)
    finder_subparsers.add_parser("domains", help="Print built-in test domain sets")
    finder_subparsers.add_parser("candidates", help="Print saved local candidate strategies")
    finder_standard = finder_subparsers.add_parser("standard-discovery", help="Run blockcheck2 standard discovery")
    finder_standard.add_argument("--domain", action="append", default=[], help="Domain to test; can be repeated")
    finder_standard.add_argument("--timeout-seconds", type=int, default=21600)
    finder_standard.add_argument("--no-quic", action="store_true")
    finder_standard.add_argument("--enable-http", action="store_true")
    finder_standard.add_argument("--no-tls12", action="store_true")
    finder_standard.add_argument("--enable-tls13", action="store_true")
    finder_standard.add_argument("--scan-level", choices=["quick", "standard", "force"], default="standard")
    finder_standard.add_argument("--repeats", type=int, default=1)
    finder_standard.add_argument("--repeat-parallel", action="store_true")
    finder_standard.add_argument("--no-skip-dnscheck", action="store_true")
    finder_standard.add_argument("--no-skip-ipblock", action="store_true")
    finder_multi = finder_subparsers.add_parser(
        "multi-domain-discovery",
        help="Run experimental strategy-first discovery across multiple domains",
    )
    finder_multi.add_argument("--domain", action="append", default=[], help="Domain to test; can be repeated")
    finder_multi.add_argument("--timeout-seconds", type=int, default=21600)
    finder_multi.add_argument("--no-quic", action="store_true")
    finder_multi.add_argument("--enable-http", action="store_true")
    finder_multi.add_argument("--no-tls12", action="store_true")
    finder_multi.add_argument("--enable-tls13", action="store_true")
    finder_multi.add_argument("--scan-level", choices=["quick", "standard", "force"], default="standard")
    finder_multi.add_argument("--repeats", type=int, default=1)
    finder_multi.add_argument("--repeat-parallel", action="store_true")
    finder_multi.add_argument("--no-skip-dnscheck", action="store_true")
    finder_multi.add_argument("--no-skip-ipblock", action="store_true")
    finder_multi.add_argument("--curl-parallelism", type=int, default=4)
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

    if args.command == "zapret2":
        if args.zapret_command == "check-install":
            _print_json(check_install())
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
                enable_http=args.enable_http,
                enable_tls12=not args.no_tls12,
                enable_tls13=args.enable_tls13,
                scan_level=args.scan_level,
                repeats=args.repeats,
                repeat_parallel=args.repeat_parallel,
                skip_dnscheck=not args.no_skip_dnscheck,
                skip_ipblock=not args.no_skip_ipblock,
            )
            _print_json(run)
            return 0
        if args.finder_command == "multi-domain-discovery":
            run = run_multi_domain_discovery(
                args.domain,
                config.output.state_dir,
                timeout_seconds=args.timeout_seconds,
                include_quic=not args.no_quic,
                enable_http=args.enable_http,
                enable_tls12=not args.no_tls12,
                enable_tls13=args.enable_tls13,
                scan_level=args.scan_level,
                repeats=args.repeats,
                repeat_parallel=args.repeat_parallel,
                skip_dnscheck=not args.no_skip_dnscheck,
                skip_ipblock=not args.no_skip_ipblock,
                curl_parallelism=args.curl_parallelism,
            )
            _print_json(run)
            return 0
        raise ValueError("unsupported strategy-finder command")

    if args.command == "web":
        serve(config, host=args.host, port=args.port)
        return 0

    raise ValueError(f"unsupported command: {args.command}")


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
