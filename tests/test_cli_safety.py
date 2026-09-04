from __future__ import annotations

import ast
import sys
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.cli import build_parser

_COMMAND_RUNNERS = {
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell",
    "os.system",
    "os.popen",
}
_ROUTER_EXECUTABLES = {"ssh", "rci", "keenetic"}
_ROUTER_MUTATIONS = {"apply", "restart"}


pytestmark = pytest.mark.quality
def _call_name(call: ast.Call) -> str:
    parts: list[str] = []
    node: ast.AST = call.func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _literal_command_terms(expression: ast.AST) -> list[str]:
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        return expression.value.lower().split()
    if isinstance(expression, (ast.List, ast.Tuple)):
        return [
            item.value.lower()
            for item in expression.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        ]
    return []


def _router_operation_hits(source: str) -> list[str]:
    hits: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or _call_name(node) not in _COMMAND_RUNNERS or not node.args:
            continue
        terms = _literal_command_terms(node.args[0])
        executable = Path(terms[0]).name if terms else ""
        is_router_command = executable in _ROUTER_EXECUTABLES
        is_router_mutation = bool(set(terms) & _ROUTER_EXECUTABLES) and bool(set(terms) & _ROUTER_MUTATIONS)
        if is_router_command or is_router_mutation:
            hits.append(f"line {node.lineno}: {' '.join(terms)}")
    return hits


class CliSafetyTests(unittest.TestCase):
    def test_current_commands_are_present(self) -> None:
        parser = build_parser()

        self.assertEqual(parser.parse_args(["zapret2", "check-install"]).zapret_command, "check-install")
        self.assertEqual(parser.parse_args(["strategy-finder", "domains"]).finder_command, "domains")
        self.assertEqual(parser.parse_args(["strategy-finder", "candidates"]).finder_command, "candidates")
        self.assertEqual(
            parser.parse_args(["strategy-finder", "standard-discovery", "--domain", "youtube.com"]).finder_command,
            "standard-discovery",
        )
        self.assertEqual(
            parser.parse_args(["strategy-finder", "multi-domain-discovery", "--domain", "youtube.com"]).finder_command,
            "multi-domain-discovery",
        )
        self.assertEqual(
            parser.parse_args(
                ["strategy-finder", "multi-domain-discovery", "--domain", "youtube.com", "--curl-parallelism", "6"]
            ).curl_parallelism,
            6,
        )
        self.assertEqual(
            parser.parse_args(["strategy-finder", "multi-domain-discovery", "--domain", "youtube.com"]).curl_parallelism,
            4,
        )
        self.assertEqual(parser.parse_args(["storage", "status"]).storage_command, "status")
        self.assertEqual(
            parser.parse_args(["domain-sources", "prepare-v2fly"]).domain_sources_command,
            "prepare-v2fly",
        )
        core_args = parser.parse_args(["core"])
        self.assertEqual(core_args.command, "core")
        self.assertEqual(core_args.host, "127.0.0.1")
        self.assertEqual(core_args.port, 8081)
        web_proxy_args = parser.parse_args(["web", "--core-url", "http://127.0.0.1:8081"])
        self.assertEqual(web_proxy_args.command, "web")
        self.assertEqual(web_proxy_args.core_url, "http://127.0.0.1:8081")
        standard_args = parser.parse_args(
            [
                "strategy-finder",
                "standard-discovery",
                "--domain",
                "youtube.com",
                "--enable-http",
                "--no-tls12",
                "--enable-tls13",
                "--scan-level",
                "force",
                "--repeats",
                "3",
                "--repeat-parallel",
                "--no-skip-dnscheck",
                "--no-skip-ipblock",
            ]
        )
        self.assertTrue(standard_args.enable_http)
        self.assertTrue(standard_args.no_tls12)
        self.assertTrue(standard_args.enable_tls13)
        self.assertEqual(standard_args.scan_level, "force")
        self.assertEqual(standard_args.repeats, 3)
        self.assertTrue(standard_args.repeat_parallel)
        self.assertTrue(standard_args.no_skip_dnscheck)
        self.assertTrue(standard_args.no_skip_ipblock)
        self.assertEqual(parser.parse_args(["web"]).command, "web")

    def test_future_commands_are_absent(self) -> None:
        parser = build_parser()
        removed_commands = [
            ["validate"],
            ["sync", "--pull-only"],
            ["render", "--dry-run"],
            ["healthcheck", "--direct-only"],
            ["evidence", "write", "--no-push"],
            ["zapret2", "list-local"],
            ["zapret2", "run-check", "--domain", "youtube.com", "--strategy", "strategy"],
            ["strategy-finder", "custom-verification", "--candidate-id", "tls-test"],
        ]

        for command in removed_commands:
            with self.subTest(command=command), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit):
                    parser.parse_args(command)

    def test_state_dir_argument_can_be_after_command(self) -> None:
        from gp_control_plane.cli import _normalize_argv

        self.assertEqual(
            _normalize_argv(["web", "--state-dir", "/tmp/gp-state"]),
            ["--state-dir", "/tmp/gp-state", "web"],
        )
        self.assertEqual(
            _normalize_argv(["strategy-finder", "domains", "--state-dir=/tmp/gp-state"]),
            ["--state-dir=/tmp/gp-state", "strategy-finder", "domains"],
        )

    def test_legacy_web_config_argument_is_ignored_for_release_update_compatibility(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["web", "--config", "configs/orchestrator.example.yaml"])
        self.assertEqual(args.command, "web")
        self.assertEqual(args.config, "configs/orchestrator.example.yaml")

        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["--config", "configs/orchestrator.example.yaml", "web"])

    def test_forbidden_router_operations_are_not_in_source(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "src" / "gp_control_plane"
        hits: list[str] = []
        for path in source_root.rglob("*.py"):
            for hit in _router_operation_hits(path.read_text(encoding="utf-8")):
                hits.append(f"{path.relative_to(source_root)}:{hit}")

        self.assertEqual(hits, [])
        self.assertEqual(_router_operation_hits("def _apply_strict_helper_evidence():\n    pass\n"), [])
        self.assertEqual(
            _router_operation_hits("import subprocess\nsubprocess.run(['rci', 'apply'])\n"),
            ["line 2: rci apply"],
        )

    def test_subprocess_calls_are_list_based_without_shell(self) -> None:
        """S603-intent guard: no subprocess call may use shell=True or a bare
        string command anywhere in src (commands must be explicit arg lists)."""
        import ast

        source_root = Path(__file__).resolve().parents[1] / "src" / "gp_control_plane"
        subprocess_attrs = {
            "Popen", "run", "call", "check_call", "check_output",
        }
        bad: list[str] = []

        def _walk(path: Path, tree: ast.AST) -> None:
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute):
                    continue
                if not (isinstance(func.value, ast.Name) and func.value.id == "subprocess"):
                    continue
                if func.attr not in subprocess_attrs:
                    continue
                for kw in node.keywords:
                    if kw.arg == "shell":
                        if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                            bad.append(f"{path.relative_to(source_root)}:{node.lineno} shell=True")
                args = node.args
                if not args:
                    continue
                first = args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    bad.append(f"{path.relative_to(source_root)}:{node.lineno} string command")

        for path in source_root.rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            _walk(path, tree)

        self.assertEqual(bad, [])
        self.assertEqual(
            _subprocess_flags(ast.parse("import subprocess\nsubprocess.run('x', shell=True)\n")),
            {"string command", "shell=True"},
        )


def _subprocess_flags(tree: ast.AST) -> set[str]:
    flags: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        func = node.func
        if not (isinstance(func.value, ast.Name) and func.value.id == "subprocess"):
            continue
        if func.attr not in {"Popen", "run", "call", "check_call", "check_output"}:
            continue
        for kw in node.keywords:
            if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                flags.add("shell=True")
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            flags.add("string command")
    return flags


if __name__ == "__main__":
    unittest.main()

