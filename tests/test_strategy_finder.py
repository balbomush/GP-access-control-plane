from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.state import append_jsonl
from gp_control_plane.strategy_finder import (
    DiscoveryOptions,
    _resolve_blockcheck_script,
    _standard_attempt_plan,
    _write_multidomain_runner,
    candidate_id_for,
    latest_log_tail,
    parse_blockcheck_stdout,
    progress_from_stdout,
    read_candidates,
    upsert_candidates,
)


class StrategyFinderTests(unittest.TestCase):
    def test_discovery_options_build_blockcheck_env(self) -> None:
        options = DiscoveryOptions(
            enable_http=True,
            enable_tls12=False,
            enable_tls13=True,
            enable_quic=False,
            scan_level="force",
            repeats=3,
            repeat_parallel=True,
            skip_dnscheck=False,
            skip_ipblock=False,
        )

        self.assertEqual(
            options.to_blockcheck_env(),
            {
                "SKIP_DNSCHECK": "0",
                "SKIP_IPBLOCK": "0",
                "ENABLE_HTTP": "1",
                "ENABLE_HTTPS_TLS12": "0",
                "ENABLE_HTTPS_TLS13": "1",
                "ENABLE_HTTP3": "0",
                "SCANLEVEL": "force",
                "REPEATS": "3",
                "PARALLEL": "1",
            },
        )
        self.assertEqual(options.to_run_fields()["enable_tls"], False)
        self.assertEqual(options.to_run_fields()["discovery_options"]["scan_level"], "force")

    def test_discovery_options_normalize_scan_and_repeats(self) -> None:
        options = DiscoveryOptions(scan_level="bad", repeats=99)

        self.assertEqual(options.normalized().scan_level, "standard")
        self.assertEqual(options.normalized().repeats, 10)

    def test_discovery_options_require_protocol(self) -> None:
        options = DiscoveryOptions(enable_http=False, enable_tls12=False, enable_tls13=False, enable_quic=False)

        with self.assertRaises(ValueError):
            options.normalized()

    def test_parse_blockcheck_summary_extracts_candidates(self) -> None:
        stdout = """
noise
* SUMMARY
curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload tls_client_hello --lua-desync=fake
curl_test_http3 ipv4 discord.com : nfqws2 --filter-udp=443 --dpi-desync=fake
curl_test_https_tls12 ipv4 web.telegram.org : nfqws2 not working
"""

        parsed = parse_blockcheck_stdout(stdout)

        self.assertEqual(len(parsed["candidates"]), 2)
        self.assertEqual(parsed["candidates"][0]["protocol"], "tls")
        self.assertEqual(parsed["candidates"][1]["protocol"], "quic")
        self.assertEqual(parsed["not_working"][0]["domain"], "web.telegram.org")

    def test_upsert_candidates_persists_unique_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            parsed = {
                "candidates": [
                    {
                        "domain": "youtube.com",
                        "test": "curl_test_https_tls12",
                        "ip_version": "4",
                        "protocol": "tls",
                        "args": "--payload tls_client_hello --lua-desync=fake",
                    }
                ]
            }
            run = {"id": "run1"}

            upsert_candidates(state_dir, parsed, run)
            upsert_candidates(state_dir, parsed, run)
            candidates = read_candidates(state_dir)

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["id"], candidate_id_for("tls", "--payload tls_client_hello --lua-desync=fake"))
            self.assertEqual(len(candidates[0]["seen"]), 2)
            self.assertNotIn("verifications", candidates[0])

    def test_parse_blockcheck_common_extracts_common_candidates(self) -> None:
        stdout = """
* SUMMARY
curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload tls_client_hello --lua-desync=fake
curl_test_https_tls12 ipv4 discord.com : nfqws2 --payload tls_client_hello --lua-desync=fake

* COMMON
curl_test_https_tls12 ipv4 : nfqws2 --payload tls_client_hello --lua-desync=fake
"""

        parsed = parse_blockcheck_stdout(stdout)

        self.assertEqual(len(parsed["candidates"]), 2)
        self.assertEqual(len(parsed["common_candidates"]), 1)
        self.assertEqual(parsed["common_candidates"][0]["scope"], "common")
        self.assertEqual(parsed["common_candidates"][0]["domain"], "")

    def test_upsert_candidates_persists_common_seen(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            parsed = {
                "candidates": [],
                "common_candidates": [
                    {
                        "domain": "",
                        "test": "curl_test_https_tls12",
                        "ip_version": "4",
                        "protocol": "tls",
                        "args": "--payload tls_client_hello --lua-desync=fake",
                    }
                ],
            }
            run = {"id": "run1", "domains": ["youtube.com", "discord.com"]}

            upsert_candidates(state_dir, parsed, run)
            candidates = read_candidates(state_dir)

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["common_seen"][0]["domains"], ["youtube.com", "discord.com"])

    def test_parse_live_success_without_summary(self) -> None:
        stdout = """
!!!!! curl_test_https_tls12: working strategy found for ipv4 youtube.com : nfqws2 --payload tls_client_hello --lua-desync=fake !!!!!
"""

        parsed = parse_blockcheck_stdout(stdout)

        self.assertEqual(len(parsed["candidates"]), 1)
        self.assertEqual(parsed["candidates"][0]["domain"], "youtube.com")

    def test_parse_live_available_attempt_without_summary(self) -> None:
        stdout = """
* script : standard/20-multi.sh
- curl_test_https_tls12 ipv4 youtube.com : nfqws2 --lua-desync=wssize:wsize=1:scale=6 --payload=tls_client_hello --lua-desync=multidisorder:pos=1,sniext+1,host+1,midsld-2,midsld,midsld+2,endhost-1
!!!!! AVAILABLE !!!!!
- curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=tls_client_hello --lua-desync=multisplit:pos=1
UNAVAILABLE code=28
"""

        parsed = parse_blockcheck_stdout(stdout)

        self.assertEqual(len(parsed["candidates"]), 1)
        self.assertEqual(parsed["candidates"][0]["domain"], "youtube.com")
        self.assertIn("multidisorder", parsed["candidates"][0]["args"])

    def test_upsert_candidates_persists_stopped_live_available_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            stdout = """
- curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=tls_client_hello --lua-desync=multidisorder:pos=host+1
!!!!! AVAILABLE !!!!!
"""
            parsed = parse_blockcheck_stdout(stdout)

            upsert_candidates(state_dir, parsed, {"id": "stopped-run", "status": "stopped"})
            candidates = read_candidates(state_dir)

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["seen"][0]["run_id"], "stopped-run")

    def test_progress_counts_attempts_and_successes(self) -> None:
        stdout = """
* script : standard/15-misc.sh
- curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload one
UNAVAILABLE code=28
!!!!! curl_test_https_tls12: working strategy found for ipv4 youtube.com : nfqws2 --payload tls_client_hello --lua-desync=fake !!!!!
"""

        progress = progress_from_stdout(stdout, {"timestamp": "2026-06-20T00:00:00Z", "status": "running"})

        self.assertEqual(progress["attempted"], 1)
        self.assertEqual(progress["successful"], 1)
        self.assertEqual(progress["current_script"], "standard/15-misc.sh")

    def test_standard_attempt_plan_counts_file_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "10-test.sh").write_text(
                """
pktws_check_https_tls12()
{
    for repeats in 1 2 3; do
        pktws_curl_test_update "$1" "$2" --payload=tls
    done
}
pktws_check_http3()
{
    pktws_curl_test_update "$1" "$2" --payload=quic
}
""",
                encoding="utf-8",
            )

            plan = _standard_attempt_plan(
                domains=["youtube.com", "discord.com"],
                enable_tls=True,
                enable_quic=True,
                root=root,
            )

            self.assertEqual(plan["scripts"]["standard/10-test.sh"], 8)
            self.assertEqual(plan["total"], 8)

    def test_progress_uses_attempt_total_and_timeout_eta(self) -> None:
        plan = {
            "total": 40,
            "scripts": {"standard/10-test.sh": 40},
            "script_order": ["standard/10-test.sh"],
            "source": "test",
        }
        stdout = "\n".join(["* script : standard/10-test.sh"] + ["- curl_test_https_tls12 ipv4 youtube.com : nfqws2 --one"] * 30)
        progress = progress_from_stdout(stdout, {"id": "run-eta", "status": "running", "attempt_plan": plan})

        self.assertEqual(progress["attempted"], 30)
        self.assertEqual(progress["attempt_total"], 40)
        self.assertEqual(progress["eta_seconds"], 21)
        self.assertEqual(progress["eta_estimate_ms_per_attempt"], 2100)

    def test_stopped_progress_keeps_attempt_percent(self) -> None:
        plan = {
            "total": 100,
            "scripts": {"standard/10-test.sh": 100},
            "script_order": ["standard/10-test.sh"],
            "source": "test",
        }
        stdout = "\n".join(["* script : standard/10-test.sh"] + ["- curl_test_https_tls12 ipv4 youtube.com : nfqws2 --one"] * 25)

        progress = progress_from_stdout(stdout, {"id": "run-stopped", "status": "stopped", "attempt_plan": plan})

        self.assertEqual(progress["attempted"], 25)
        self.assertEqual(progress["percent"], 25.0)
        self.assertEqual(progress["script_index"], 1)

    def test_latest_log_tail_reads_recent_run_output(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            root = state_dir / "strategy-finder"
            logs = root / "logs"
            logs.mkdir(parents=True)
            stdout = logs / "run.stdout.log"
            stderr = logs / "run.stderr.log"
            stdout.write_text("a\nb\nc\n", encoding="utf-8")
            stderr.write_text("err\n", encoding="utf-8")
            append_jsonl(
                root / "runs.jsonl",
                {
                    "id": "run",
                    "kind": "standard-discovery",
                    "status": "running",
                    "stdout_log": str(stdout),
                    "stderr_log": str(stderr),
                },
            )

            tail = latest_log_tail(state_dir, max_lines=2)

            self.assertEqual(tail["run_id"], "run")
            self.assertEqual(tail["stdout_tail"], "b\nc")
            self.assertEqual(tail["stderr_tail"], "err")

    def test_multidomain_runner_overrides_strategy_check_order(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            blockcheck = tmp / "blockcheck2.sh"
            blockcheck.write_text(
                "#!/bin/sh\n"
                "pktws_curl_test_update() { echo stock; }\n"
                "\nfsleep_setup\n"
                "echo stock main\n",
                encoding="utf-8",
            )

            runner = _write_multidomain_runner(tmp, blockcheck)
            text = runner.read_text(encoding="utf-8")

            self.assertIn("gp_md_run_protocol pktws_check_http curl_test_http", text)
            self.assertIn("gp_md_run_protocol pktws_check_https_tls12", text)
            self.assertIn("gp_md_run_protocol pktws_check_https_tls13", text)
            self.assertIn("gp_md_run_protocol pktws_check_http3", text)
            self.assertIn("for gp_domain in $DOMAINS", text)
            self.assertIn('pktws_start "$@"', text)
            self.assertIn("GP_MD_CURL_PARALLELISM", text)
            self.assertIn('gp_md_run_domain_curl "$idx" "$testf" "$gp_domain" &', text)
            self.assertIn("gp_md_collect_record", text)
            self.assertIn('curl_test "$testf" "$gp_domain"', text)
            self.assertNotIn("echo stock main", text)

    def test_resolve_blockcheck_script_follows_exec_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            real = tmp / "real-blockcheck2.sh"
            real.write_text("#!/bin/sh\n\nfsleep_setup\necho real\n", encoding="utf-8")
            wrapper = tmp / "blockcheck2.sh"
            wrapper.write_text("#!/bin/sh\nexec real-blockcheck2.sh \"$@\"\n", encoding="utf-8")

            self.assertEqual(_resolve_blockcheck_script(wrapper), real.resolve())

    def test_multidomain_progress_eta_uses_curl_parallelism(self) -> None:
        plan = {
            "total": 40,
            "scripts": {"standard/10-test.sh": 40},
            "script_order": ["standard/10-test.sh"],
            "source": "test",
        }
        stdout = "\n".join(["* script : standard/10-test.sh"] + ["- curl_test_https_tls12 ipv4 youtube.com : nfqws2 --one"] * 20)

        progress = progress_from_stdout(
            stdout,
            {
                "id": "run-parallel",
                "kind": "multi-domain-discovery",
                "status": "running",
                "attempt_plan": plan,
                "curl_parallelism": 4,
            },
        )

        self.assertEqual(progress["remaining_attempts"], 20)
        self.assertEqual(progress["eta_parallelism"], 4)
        self.assertEqual(progress["eta_seconds"], 10)

    def test_progress_eta_accounts_for_sequential_repeats(self) -> None:
        plan = {
            "total": 10,
            "scripts": {"standard/10-test.sh": 10},
            "script_order": ["standard/10-test.sh"],
            "source": "test",
        }
        stdout = "\n".join(["* script : standard/10-test.sh"] + ["- curl_test_https_tls12 ipv4 youtube.com : nfqws2 --one"] * 5)

        progress = progress_from_stdout(
            stdout,
            {
                "id": "run-repeats",
                "kind": "standard-discovery",
                "status": "running",
                "attempt_plan": plan,
                "repeats": 3,
                "repeat_parallel": False,
            },
        )

        self.assertEqual(progress["eta_estimate_ms_per_attempt"], 6300)
        self.assertEqual(progress["eta_seconds"], 31)


if __name__ == "__main__":
    unittest.main()
