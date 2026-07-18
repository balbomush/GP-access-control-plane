from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.storage import (
    append_run,
    connect,
    storage_status,
    upsert_candidate_event_conn as storage_upsert_candidate_event_conn,
)
from gp_control_plane.strategy_finder import (
    DiscoveryOptions,
    LOG_RETENTION_MAX_FILES,
    _CompactStdoutWriter,
    _LiveStdoutRecorder,
    _RotatingTextWriter,
    _average_attempt_ms,
    _cleanup_old_strategy_logs,
    _eta_recalculation_attempts,
    _eta_recalculation_step,
    _progress_from_counts,
    _resolve_blockcheck_script,
    _run_multidomain_blockcheck_live,
    _run_process_with_live_stdout,
    _standard_attempt_plan,
    _stdout_log_mode,
    _write_multidomain_runner,
    candidate_id_for,
    candidate_total,
    classify_domain_input,
    classify_stderr_diagnostics,
    close_stale_running_runs,
    curl_failure_info,
    latest_log_tail,
    parse_blockcheck_stdout,
    progress_from_stdout,
    read_candidate_domain_index,
    read_candidate_page,
    read_candidates,
    read_runs,
    run_standard_discovery,
    upsert_candidates,
    validate_domain_inputs,
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
                "CURL_MAX_TIME": "2",
                "CURL_MAX_TIME_QUIC": "2",
                "CURL_MAX_TIME_DOH": "2",
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

    def test_domain_validation_rejects_non_domain_rules(self) -> None:
        result = validate_domain_inputs(
            [
                "YouTube.COM",
                "*.example.com",
                "keyword:discord",
                "regexp:^google",
                "domain:google.com",
                "https://youtube.com/watch",
                "googlevideo.com",
            ]
        )

        self.assertEqual(result["domains"], ["youtube.com", "googlevideo.com"])
        self.assertEqual(result["valid_count"], 2)
        self.assertEqual(result["skipped_count"], 5)
        skipped_statuses = {item["status"] for item in result["domain_skipped"]}
        self.assertIn("wildcard", skipped_statuses)
        self.assertIn("domain_list_rule", skipped_statuses)
        self.assertIn("url", skipped_statuses)
        service = classify_domain_input("googlevideo.com")
        self.assertTrue(service["valid"])
        self.assertEqual(service["status"], "service")

    def test_invalid_only_domain_run_fails_before_blockcheck_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(ValueError):
                run_standard_discovery(["keyword:discord"], Path(raw), timeout_seconds=1)

    def test_curl_failure_info_maps_human_statuses(self) -> None:
        self.assertEqual(curl_failure_info("3")["label"], "некорректная строка домена")
        self.assertEqual(curl_failure_info("6")["label"], "DNS ошибка")
        self.assertEqual(curl_failure_info("28")["label"], "таймаут")
        self.assertEqual(curl_failure_info("35")["label"], "SSL/connect ошибка")
        self.assertEqual(curl_failure_info("60", domain="googlevideo.com")["label"], "TLS/SNI проблема")
        self.assertEqual(curl_failure_info("7", test="curl_test_http3")["label"], "QUIC/connect ошибка")

    def test_stdout_log_mode_defaults_to_raw_terminal_output(self) -> None:
        self.assertEqual(_stdout_log_mode({}), "raw")
        self.assertEqual(_stdout_log_mode({"GP_DEBUG_STDOUT": "0"}), "raw")
        self.assertEqual(_stdout_log_mode({"GP_COMPACT_STDOUT": "1"}), "compact")
        self.assertEqual(_stdout_log_mode({"GP_DEBUG_STDOUT": "1"}), "debug")

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

    def test_parse_blockcheck_stdout_reports_curl_diagnostics(self) -> None:
        stdout = """
- curl_test_https_tls12 ipv4 googlevideo.com : nfqws2 --payload=tls_client_hello --lua-desync=multidisorder
UNAVAILABLE code=60
- curl_test_https_tls12 ipv4 no-such-domain.invalid : nfqws2 --payload=tls_client_hello --lua-desync=fake
UNAVAILABLE code=6
- curl_test_http3 ipv4 discord.com : nfqws2 --filter-udp=443 --lua-desync=fake
UNAVAILABLE code=7
"""

        parsed = parse_blockcheck_stdout(stdout)
        by_domain = {item["domain"]: item for item in parsed["domain_diagnostics"]}

        self.assertEqual(by_domain["googlevideo.com"]["status"], "tls_sni_problem")
        self.assertEqual(by_domain["googlevideo.com"]["label"], "TLS/SNI проблема")
        self.assertEqual(by_domain["no-such-domain.invalid"]["status"], "dns_error")
        self.assertEqual(by_domain["discord.com"]["status"], "quic_connect_error")
        self.assertEqual(parsed["curl_diagnostics_summary"], {"60": 1, "6": 1, "7": 1})
        self.assertFalse(parsed["curl_diagnostics"][0]["strategy_failure"])

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
            self.assertEqual(len(candidates[0]["seen"]), 1)
            self.assertEqual(candidates[0]["seen"][0]["domain"], "youtube.com")
            self.assertEqual(candidates[0]["fragmentation_class"], "position_free")
            self.assertTrue(candidates[0]["fragmentation_safe"])
            self.assertEqual(candidates[0]["family"], "fake")
            self.assertIn("fragmentation_reason", candidates[0])
            self.assertIn("family_reason", candidates[0])
            self.assertNotIn("verifications", candidates[0])

    def test_candidate_page_can_filter_fragmentation_classes(self) -> None:
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
                    },
                    {
                        "domain": "youtube.com",
                        "test": "curl_test_https_tls12",
                        "ip_version": "4",
                        "protocol": "tls",
                        "args": "--payload=tls_client_hello --lua-desync=multisplit:pos=1",
                    },
                ]
            }

            upsert_candidates(state_dir, parsed, {"id": "run1"})

            safe_page = read_candidate_page(
                state_dir,
                domain="youtube.com",
                fragmentation_classes=["position_free", "position_safe", "unknown"],
            )
            risky_page = read_candidate_page(state_dir, domain="youtube.com", fragmentation_classes=["position_risky"])

            self.assertEqual(safe_page["total"], 1)
            self.assertEqual(safe_page["candidates"][0]["fragmentation_class"], "position_free")
            self.assertEqual(risky_page["total"], 1)
            self.assertEqual(risky_page["candidates"][0]["fragmentation_class"], "position_risky")

    def test_candidate_domain_index_respects_fragmentation_filter(self) -> None:
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
                    },
                    {
                        "domain": "discord.com",
                        "test": "curl_test_https_tls12",
                        "ip_version": "4",
                        "protocol": "tls",
                        "args": "--payload=tls_client_hello --lua-desync=multisplit:pos=1",
                    },
                ]
            }

            upsert_candidates(state_dir, parsed, {"id": "run1"})
            index = read_candidate_domain_index(state_dir, fragmentation_classes=["position_risky"])

            self.assertEqual([item["domain"] for item in index["domains"]], ["discord.com"])
            self.assertEqual(index["strategy_total"], 1)

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
            self.assertEqual(sorted(candidates[0]["common_seen"][0]["domains"]), ["discord.com", "youtube.com"])

    def test_upsert_candidates_writes_normalized_model(self) -> None:
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
                ],
                "common_candidates": [
                    {
                        "domain": "",
                        "test": "curl_test_https_tls12",
                        "ip_version": "4",
                        "protocol": "tls",
                        "args": "--payload tls_client_hello --lua-desync=common",
                    }
                ],
            }

            upsert_candidates(state_dir, parsed, {"id": "run1", "domains": ["youtube.com", "discord.com"]})

            with connect(state_dir) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) AS count FROM strategies").fetchone()["count"], 2)
                self.assertEqual(conn.execute("SELECT COUNT(*) AS count FROM domains").fetchone()["count"], 2)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) AS count FROM strategy_domain_results").fetchone()["count"],
                    3,
                )
                self.assertFalse(
                    conn.execute(
                        """
                        SELECT COUNT(*) AS count
                        FROM sqlite_master
                        WHERE type = 'table' AND name = 'strategy_attempts'
                        """
                    ).fetchone()["count"],
                )
                self.assertEqual(
                    conn.execute("SELECT strategy_count FROM domain_stats WHERE domain = 'youtube.com'").fetchone()[0],
                    2,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT domain_count FROM strategy_stats WHERE strategy_id = ?",
                        (candidate_id_for("tls", "--payload tls_client_hello --lua-desync=common"),),
                    ).fetchone()[0],
                    2,
                )
            common = read_candidate_page(state_dir, view="common", domains=["youtube.com", "discord.com"])
            self.assertEqual(common["total"], 1)
            self.assertEqual(common["candidates"][0]["args"], "--payload tls_client_hello --lua-desync=common")

    def test_storage_status_reports_normalized_counts(self) -> None:
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
                ],
            }

            upsert_candidates(state_dir, parsed, {"id": "run1"})
            status = storage_status(state_dir)

            self.assertEqual(status["schema_version"], "10")
            self.assertEqual(status["expected_schema_version"], "10")
            self.assertEqual(status["integrity_check"], "ok")
            self.assertGreater(status["db_size_bytes"], 0)
            self.assertEqual(status["tables"]["domains"], 1)
            self.assertEqual(status["tables"]["strategies"], 1)
            self.assertEqual(status["tables"]["strategy_domain_results"], 1)
            self.assertEqual(status["views"]["domain_stats"], 1)
            self.assertEqual(status["views"]["strategy_stats"], 1)

    def test_strategy_domain_results_has_no_persistent_counters(self) -> None:
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

            upsert_candidates(state_dir, parsed, {"id": "run1"})

            with connect(state_dir) as conn:
                columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(strategy_domain_results)").fetchall()}
                self.assertNotIn("success_count", columns)
                self.assertNotIn("fail_count", columns)
                self.assertEqual(
                    conn.execute("SELECT strategy_count FROM domain_stats WHERE domain = 'youtube.com'").fetchone()[0],
                    1,
                )

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
            self.assertEqual(candidates[0]["seen"][0]["domain"], "youtube.com")
            self.assertEqual(candidates[0]["seen"][0]["run_id"], "")

    def test_live_recorder_preserves_unavailable_curl_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            run = {"id": "run-diagnostics", "domains": ["googlevideo.com"]}
            recorder = _LiveStdoutRecorder(state_dir, run)

            recorder.record_line("- curl_test_https_tls12 ipv4 googlevideo.com : nfqws2 --payload=tls_client_hello")
            recorder.record_line("UNAVAILABLE code=60")
            parsed = recorder.parsed()

            self.assertEqual(parsed["curl_diagnostics_summary"], {"60": 1})
            self.assertEqual(parsed["domain_diagnostics"][0]["domain"], "googlevideo.com")
            self.assertEqual(parsed["domain_diagnostics"][0]["status"], "tls_sni_problem")
            self.assertFalse(parsed["curl_diagnostics"][0]["strategy_failure"])

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
            self.assertEqual(plan["strategy_scripts"]["standard/10-test.sh"], 4)
            self.assertEqual(plan["strategy_total"], 4)

    def test_standard_attempt_plan_accounts_for_ipv6(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "10-test.sh").write_text(
                """
pktws_check_https_tls12()
{
    pktws_curl_test_update "$1" "$2" --payload=tls
}
""",
                encoding="utf-8",
            )

            plan = _standard_attempt_plan(
                domains=["youtube.com", "discord.com"],
                enable_tls=True,
                enable_quic=False,
                enable_ipv6=True,
                root=root,
            )

            self.assertEqual(plan["ip_version_count"], 2)
            self.assertEqual(plan["total"], 4)
            self.assertEqual(plan["strategy_total"], 1)

    def test_progress_reports_strategy_templates_separately_from_attempts(self) -> None:
        plan = {
            "total": 80,
            "scripts": {"standard/10-test.sh": 80},
            "strategy_total": 4,
            "strategy_scripts": {"standard/10-test.sh": 4},
            "script_order": ["standard/10-test.sh"],
            "domain_count": 20,
            "ip_version_count": 1,
            "source": "test",
        }

        progress = _progress_from_counts(
            run={"id": "run-strategy-progress", "status": "running", "attempt_plan": plan},
            attempted=39,
            attempts_by_script={"standard/10-test.sh": 39},
            successful=0,
            current_script="standard/10-test.sh",
            elapsed_seconds_override=10,
        )

        self.assertEqual(progress["attempted"], 39)
        self.assertEqual(progress["attempt_total"], 80)
        self.assertEqual(progress["strategy_checked"], 1)
        self.assertEqual(progress["strategy_total"], 4)
        self.assertEqual(progress["current_script_strategy_checked"], 1)
        self.assertEqual(progress["current_script_strategy_total"], 4)

    def test_progress_waits_for_elapsed_eta_without_started_at(self) -> None:
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
        self.assertIsNone(progress["eta_seconds"])
        self.assertEqual(progress["eta_status"], "calculating")
        self.assertEqual(progress["eta_method"], "waiting_for_attempts")

    def test_progress_eta_uses_elapsed_average_per_attempt(self) -> None:
        plan = {
            "total": 40,
            "scripts": {"standard/10-test.sh": 40},
            "script_order": ["standard/10-test.sh"],
            "source": "test",
        }

        progress = _progress_from_counts(
            run={"id": "run-eta", "status": "running", "attempt_plan": plan},
            attempted=30,
            attempts_by_script={"standard/10-test.sh": 30},
            successful=0,
            current_script="standard/10-test.sh",
            elapsed_seconds_override=60,
        )

        self.assertEqual(progress["remaining_attempts"], 10)
        self.assertEqual(progress["eta_status"], "elapsed_average")
        self.assertEqual(progress["eta_method"], "elapsed_average")
        self.assertEqual(progress["eta_ms_per_attempt"], 2000)
        self.assertEqual(progress["eta_seconds"], 20)

    def test_progress_eta_recalculation_attempts_use_10_then_100_steps(self) -> None:
        self.assertEqual(_eta_recalculation_attempts(0), 0)
        self.assertEqual(_eta_recalculation_attempts(3), 3)
        self.assertEqual(_eta_recalculation_step(999), 10)
        self.assertEqual(_eta_recalculation_attempts(999), 990)
        self.assertEqual(_eta_recalculation_step(1000), 100)
        self.assertEqual(_eta_recalculation_attempts(1099), 1000)

    def test_progress_eta_uses_matched_elapsed_baseline_between_recalculations(self) -> None:
        plan = {
            "total": 72780,
            "scripts": {"standard/20-multi.sh": 72780},
            "script_order": ["standard/20-multi.sh"],
            "source": "test",
        }

        progress = _progress_from_counts(
            run={"id": "run-eta-baseline", "status": "running", "attempt_plan": plan},
            attempted=177,
            attempts_by_script={"standard/20-multi.sh": 177},
            successful=0,
            current_script="standard/20-multi.sh",
            elapsed_seconds_override=89,
            eta_recalculation_attempts_override=170,
            eta_elapsed_seconds_override=85,
        )

        self.assertEqual(progress["elapsed_seconds"], 89)
        self.assertEqual(progress["eta_elapsed_seconds"], 85)
        self.assertEqual(progress["eta_recalculation_attempts"], 170)
        self.assertEqual(progress["eta_ms_per_attempt"], 500)
        self.assertEqual(progress["remaining_attempts"], 72603)
        self.assertEqual(progress["eta_seconds"], 36301)

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
        self.assertIsNone(progress["remaining_attempts"])
        self.assertIsNone(progress["eta_seconds"])
        self.assertEqual(progress["eta_status"], "stopped")

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
            append_run(
                state_dir,
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
            self.assertEqual(tail["stderr_diagnostics"], [])

    def test_classify_stderr_diagnostics_reports_nfqueue_maxlen_warning(self) -> None:
        diagnostics = classify_stderr_diagnostics(
            "can't set queue maxlen: No such file or directory\nother stderr\n"
            "can't set queue maxlen: No such file or directory\n"
        )

        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["severity"], "warning")
        self.assertEqual(diagnostics[0]["status"], "nfqueue_maxlen_sysctl_missing")
        self.assertIn("не считается фатальной ошибкой", diagnostics[0]["message"])

    def test_latest_log_tail_exposes_nfqueue_stderr_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            root = state_dir / "strategy-finder"
            logs = root / "logs"
            logs.mkdir(parents=True)
            stdout = logs / "run.stdout.log"
            stderr = logs / "run.stderr.log"
            stdout.write_text("line\n", encoding="utf-8")
            stderr.write_text("can't set queue maxlen: No such file or directory\n", encoding="utf-8")
            append_run(
                state_dir,
                {
                    "id": "run",
                    "kind": "multi-domain-discovery",
                    "status": "running",
                    "stdout_log": str(stdout),
                    "stderr_log": str(stderr),
                },
            )

            tail = latest_log_tail(state_dir, max_lines=2)

            self.assertEqual(tail["stderr_diagnostics"][0]["status"], "nfqueue_maxlen_sysctl_missing")
            self.assertEqual(tail["stderr_diagnostics"][0]["severity"], "warning")

    def test_latest_log_tail_reads_only_requested_tail(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            root = state_dir / "strategy-finder"
            logs = root / "logs"
            logs.mkdir(parents=True)
            stdout = logs / "run.stdout.log"
            stdout.write_text("\n".join(f"line-{index}" for index in range(1000)) + "\n", encoding="utf-8")
            append_run(
                state_dir,
                {
                    "id": "run",
                    "kind": "standard-discovery",
                    "status": "running",
                    "stdout_log": str(stdout),
                },
            )

            tail = latest_log_tail(state_dir, max_lines=3)

            self.assertEqual(tail["stdout_tail"], "line-997\nline-998\nline-999")

    def test_old_strategy_logs_are_rotated_before_new_runs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            logs = Path(raw) / "logs"
            logs.mkdir()
            for index in range(LOG_RETENTION_MAX_FILES + 5):
                path = logs / f"run-{index}.standard-discovery.stdout.log"
                path.write_text(f"log {index}\n", encoding="utf-8")
                os.utime(path, (index + 1, index + 1))
            keep = logs / "manual-note.txt"
            keep.write_text("do not delete\n", encoding="utf-8")

            result = _cleanup_old_strategy_logs(logs)

            remaining_logs = list(logs.glob("*.stdout.log"))
            self.assertEqual(len(remaining_logs), LOG_RETENTION_MAX_FILES)
            self.assertEqual(result["removed_files"], 5)
            self.assertTrue(keep.exists())
            self.assertTrue((logs / f"run-{LOG_RETENTION_MAX_FILES + 4}.standard-discovery.stdout.log").exists())
            self.assertFalse((logs / "run-0.standard-discovery.stdout.log").exists())

    def test_latest_log_tail_does_not_read_full_stdout_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            root = state_dir / "strategy-finder"
            logs = root / "logs"
            logs.mkdir(parents=True)
            stdout = logs / "large.stdout.log"
            stdout.write_text("\n".join(f"line-{index}" for index in range(5000)) + "\n", encoding="utf-8")
            append_run(
                state_dir,
                {
                    "id": "run-large-log",
                    "kind": "standard-discovery",
                    "status": "running",
                    "stdout_log": str(stdout),
                },
            )

            original_read_text = Path.read_text

            def guarded_read_text(path: Path, *args: object, **kwargs: object) -> str:
                if path == stdout:
                    raise AssertionError("stdout log must be read through tail, not full read_text")
                return original_read_text(path, *args, **kwargs)

            with patch.object(Path, "read_text", guarded_read_text):
                tail = latest_log_tail(state_dir, max_lines=2)

            self.assertEqual(tail["stdout_tail"], "line-4998\nline-4999")

    def test_read_candidate_page_limits_results_and_reports_total(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            candidates = [
                {
                    "id": f"tls-{index}",
                    "protocol": "tls",
                    "args": f"--strategy {index}",
                    "status": "candidate",
                    "seen": [{"domain": f"domain-{index}.test", "run_id": "run"}],
                }
                for index in range(3)
            ]
            _store_candidate_rows(state_dir, candidates)

            page = read_candidate_page(state_dir, limit=2)

            self.assertEqual(page["total"], 3)
            self.assertEqual(len(page["candidates"]), 2)
            self.assertTrue(page["has_more"])
            self.assertEqual(page["tested_domains"], ["domain-0.test", "domain-1.test", "domain-2.test"])

    def test_read_candidate_page_keeps_large_database_paged(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            candidates = [
                {
                    "protocol": "tls",
                    "args": f"--strategy {index}",
                    "status": "candidate",
                    "seen": [{"domain": "youtube.com"}],
                }
                for index in range(1200)
            ]
            _store_candidate_rows(state_dir, candidates)

            page = read_candidate_page(state_dir, limit=25, domain="youtube.com")

            self.assertEqual(page["total"], 1200)
            self.assertEqual(len(page["candidates"]), 25)
            self.assertTrue(page["has_more"])
            self.assertLess(len(json.dumps(page, ensure_ascii=False)), 20000)

    def test_read_candidate_page_filters_common_domains(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            candidates = [
                {
                    "id": "tls-common",
                    "protocol": "tls",
                    "args": "--strategy common",
                    "status": "candidate",
                    "seen": [{"domain": "youtube.com"}, {"domain": "discord.com"}],
                },
                {
                    "id": "tls-youtube",
                    "protocol": "tls",
                    "args": "--strategy youtube-only",
                    "status": "candidate",
                    "seen": [{"domain": "youtube.com"}],
                },
            ]
            _store_candidate_rows(state_dir, candidates)

            page = read_candidate_page(state_dir, view="common", domains=["youtube.com", "discord.com"])

            self.assertEqual(page["total"], 1)
            self.assertEqual(page["candidates"][0]["args"], "--strategy common")

    def test_read_candidate_page_filters_single_domain(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            candidates = [
                {
                    "id": "tls-youtube",
                    "protocol": "tls",
                    "args": "--strategy youtube",
                    "status": "candidate",
                    "seen": [{"domain": "youtube.com"}],
                },
                {
                    "id": "tls-discord",
                    "protocol": "tls",
                    "args": "--strategy discord",
                    "status": "candidate",
                    "seen": [{"domain": "discord.com"}],
                },
                {
                    "id": "quic-common",
                    "protocol": "quic",
                    "args": "--strategy common-quic",
                    "status": "candidate",
                    "common_seen": [{"domains": ["youtube.com", "discord.com"]}],
                },
            ]
            _store_candidate_rows(state_dir, candidates)

            page = read_candidate_page(state_dir, domain="youtube.com")

            self.assertEqual(page["total"], 2)
            self.assertEqual({item["args"] for item in page["candidates"]}, {"--strategy youtube", "--strategy common-quic"})

    def test_read_candidate_domain_index_counts_by_domain_and_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            candidates = [
                {
                    "id": "tls-common",
                    "protocol": "tls",
                    "args": "--strategy common",
                    "status": "candidate",
                    "seen": [{"domain": "youtube.com"}, {"domain": "discord.com"}],
                },
                {
                    "id": "quic-youtube",
                    "protocol": "quic",
                    "args": "--strategy quic",
                    "status": "candidate",
                    "seen": [{"domain": "youtube.com"}],
                },
                {
                    "id": "quic-common",
                    "protocol": "quic",
                    "args": "--strategy common-quic",
                    "status": "candidate",
                    "common_seen": [{"domains": ["youtube.com", "discord.com"]}],
                },
            ]
            _store_candidate_rows(state_dir, candidates)

            index = read_candidate_domain_index(state_dir)
            youtube = next(item for item in index["domains"] if item["domain"] == "youtube.com")
            discord = next(item for item in index["domains"] if item["domain"] == "discord.com")

            self.assertEqual(index["total"], 2)
            self.assertEqual(youtube["strategy_count"], 3)
            self.assertEqual(discord["strategy_count"], 2)
            self.assertEqual(
                youtube["protocols"],
                [{"protocol": "quic", "count": 2}, {"protocol": "tls", "count": 1}],
            )

    def test_latest_log_tail_prefers_live_progress_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            root = state_dir / "strategy-finder"
            logs = root / "logs"
            logs.mkdir(parents=True)
            stdout = logs / "run.stdout.log"
            progress = logs / "run.progress.json"
            stdout.write_text("- curl_test_https_tls12 ipv4 youtube.com : nfqws2 --one\n", encoding="utf-8")
            progress.write_text(json.dumps({"attempted": 42, "successful": 7}), encoding="utf-8")
            append_run(
                state_dir,
                {
                    "id": "run",
                    "kind": "standard-discovery",
                    "status": "running",
                    "stdout_log": str(stdout),
                    "progress_log": str(progress),
                },
            )

            tail = latest_log_tail(state_dir, max_lines=1)

            self.assertEqual(tail["progress"]["attempted"], 42)
            self.assertEqual(tail["progress"]["successful"], 7)
            self.assertNotIn("partial", tail["progress"])

    def test_latest_log_tail_can_return_only_appended_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            root = state_dir / "strategy-finder"
            logs = root / "logs"
            logs.mkdir(parents=True)
            stdout = logs / "run.stdout.log"
            progress = logs / "run.progress.json"
            stdout.write_text("line 1\nline 2\n", encoding="utf-8")
            progress.write_text(json.dumps({"attempted": 2, "successful": 0}), encoding="utf-8")
            append_run(
                state_dir,
                {
                    "id": "run",
                    "kind": "standard-discovery",
                    "status": "running",
                    "stdout_log": str(stdout),
                    "progress_log": str(progress),
                },
            )
            initial = latest_log_tail(state_dir, max_lines=2)

            with stdout.open("a", encoding="utf-8") as handle:
                handle.write("line 3\n")
            appended = latest_log_tail(
                state_dir,
                max_lines=2,
                stdout_from_size=initial["stdout_size"],
                stdout_log_match=initial["stdout_log"],
            )

            self.assertEqual(initial["stdout_tail"], "line 1\nline 2")
            self.assertEqual(appended["stdout_tail"], "")
            self.assertEqual(appended["stdout_append"].replace("\r\n", "\n"), "line 3\n")
            self.assertGreater(appended["stdout_size"], initial["stdout_size"])
            self.assertEqual(appended["progress"]["attempted"], 2)

    def test_close_stale_running_runs_appends_stopped_update(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            root = state_dir / "strategy-finder"
            root.mkdir(parents=True)
            append_run(
                state_dir,
                {
                    "id": "run-active",
                    "kind": "multi-domain-discovery",
                    "status": "running",
                    "timestamp": "2026-06-25T17:18:40Z",
                    "domains": ["youtube.com"],
                    "candidate_count": 0,
                },
            )

            closed = close_stale_running_runs(state_dir)
            runs = read_runs(state_dir, limit=10)

            self.assertEqual(closed, 1)
            self.assertEqual(runs[-1]["id"], "run-active")
            self.assertEqual(runs[-1]["status"], "stopped")
            self.assertTrue(runs[-1]["interrupted"])

    def test_read_runs_omits_heavy_summary_fields(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            append_run(
                state_dir,
                {
                    "id": "run-heavy",
                    "kind": "multi-domain-discovery",
                    "status": "stopped",
                    "timestamp": "2026-06-25T17:18:40Z",
                    "domains": ["youtube.com"],
                    "candidate_count": 1,
                    "summary": ["large"] * 1000,
                    "results": [{"raw": "large"}] * 1000,
                },
            )

            runs = read_runs(state_dir, limit=10)

            self.assertEqual(runs[0]["id"], "run-heavy")
            self.assertNotIn("summary", runs[0])
            self.assertNotIn("results", runs[0])

    def test_live_stdout_recorder_persists_success_without_full_stdout_parse(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            progress_log = state_dir / "strategy-finder" / "logs" / "run.progress.json"
            metrics_log = state_dir / "strategy-finder" / "logs" / "run.metrics.ndjson"
            run = {
                "id": "run-live",
                "kind": "standard-discovery",
                "status": "running",
                "timestamp": "2026-06-20T00:00:00Z",
                "progress_log": str(progress_log),
                "metrics_log": str(metrics_log),
                "attempt_plan": {
                    "total": 2,
                    "scripts": {"standard/10-test.sh": 2},
                    "script_order": ["standard/10-test.sh"],
                    "source": "test",
                },
            }
            recorder = _LiveStdoutRecorder(state_dir, run)

            recorder.record_line("* script : standard/10-test.sh")
            recorder.record_line("- curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=tls_client_hello")
            recorder.record_line("!!!!! AVAILABLE !!!!!")

            parsed = recorder.parsed()
            progress = recorder.progress(run)
            candidates = read_candidates(state_dir)

            self.assertEqual(len(parsed["candidates"]), 1)
            self.assertEqual(parsed["candidates"][0]["domain"], "youtube.com")
            self.assertEqual(progress["attempted"], 1)
            self.assertEqual(progress["successful"], 1)
            self.assertEqual(len(candidates), 1)
            self.assertTrue(progress_log.exists())
            self.assertTrue(metrics_log.exists())

    def test_live_stdout_recorder_persists_multidomain_success_event(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            progress_log = state_dir / "strategy-finder" / "logs" / "run.progress.json"
            run = {
                "id": "run-live",
                "kind": "multi-domain-discovery",
                "status": "running",
                "timestamp": "2026-06-20T00:00:00Z",
                "progress_log": str(progress_log),
            }
            recorder = _LiveStdoutRecorder(state_dir, run)

            recorder.record_line(
                "- curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=tls_client_hello --lua-desync=fake"
            )
            recorder.record_line(
                "!!!!! curl_test_https_tls12: working strategy found for ipv4 youtube.com : "
                "nfqws2 --payload=tls_client_hello --lua-desync=fake !!!!!"
            )

            parsed = recorder.parsed()
            progress = recorder.progress(run)
            candidates = read_candidates(state_dir)

            self.assertEqual(len(parsed["candidates"]), 1)
            self.assertEqual(parsed["candidates"][0]["domain"], "youtube.com")
            self.assertEqual(progress["successful"], 1)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["seen"][0]["domain"], "youtube.com")

    def test_compact_stdout_writer_keeps_success_and_skips_failed_attempt_noise(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "compact.log"
            with path.open("w", encoding="utf-8") as handle:
                writer = _CompactStdoutWriter(handle)
                writer.write("* script : standard/10-test.sh\n")
                writer.write("- curl_test_https_tls12 ipv4 youtube.com : nfqws2 --failed\n")
                writer.write("UNAVAILABLE code=28\n")
                writer.write("- curl_test_https_tls12 ipv4 youtube.com : nfqws2 --success\n")
                writer.write("!!!!! AVAILABLE !!!!!\n")
                writer.close()

            text = path.read_text(encoding="utf-8")

            self.assertIn("* script : standard/10-test.sh", text)
            self.assertNotIn("--failed", text)
            self.assertIn("--success", text)
            self.assertIn("!!!!! AVAILABLE !!!!!", text)

    def test_raw_stdout_log_keeps_failed_attempts_for_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            stdout_log = state_dir / "stdout.log"
            stderr_log = state_dir / "stderr.log"
            progress_log = state_dir / "progress.json"
            metrics_log = state_dir / "metrics.ndjson"
            run = {
                "id": "run-raw",
                "kind": "standard-discovery",
                "status": "running",
                "timestamp": "2026-06-20T00:00:00Z",
                "progress_log": str(progress_log),
                "metrics_log": str(metrics_log),
                "attempt_plan": {
                    "total": 1,
                    "scripts": {"standard/10-test.sh": 1},
                    "script_order": ["standard/10-test.sh"],
                    "source": "test",
                },
            }
            recorder = _LiveStdoutRecorder(state_dir, run)
            command = [
                sys.executable,
                "-c",
                "print('* script : standard/10-test.sh'); "
                "print('- curl_test_https_tls12 ipv4 youtube.com : nfqws2 --failed'); "
                "print('UNAVAILABLE code=28')",
            ]

            result = _run_process_with_live_stdout(
                command=command,
                env=os.environ.copy(),
                stdout_log=stdout_log,
                stderr_log=stderr_log,
                debug_stdout_log=None,
                timeout_seconds=10,
                stop_event=None,
                recorder=recorder,
            )
            text = stdout_log.read_text(encoding="utf-8")

            self.assertEqual(result["status"], "success")
            self.assertIn("--failed", text)
            self.assertIn("UNAVAILABLE code=28", text)

    def test_stop_event_wins_over_nonzero_process_exit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            stdout_log = state_dir / "stdout.log"
            stderr_log = state_dir / "stderr.log"
            run = {
                "id": "run-stop-race",
                "kind": "standard-discovery",
                "status": "running",
                "timestamp": "2026-06-20T00:00:00Z",
                "progress_log": str(state_dir / "progress.json"),
                "metrics_log": str(state_dir / "metrics.ndjson"),
                "attempt_plan": {"total": 1, "scripts": {}, "script_order": [], "source": "test"},
            }
            recorder = _LiveStdoutRecorder(state_dir, run)
            stop_event = threading.Event()
            command = [sys.executable, "-c", "import sys,time; time.sleep(0.2); sys.exit(1)"]

            def request_stop() -> None:
                time.sleep(0.05)
                stop_event.set()

            stopper = threading.Thread(target=request_stop)
            stopper.start()
            with (
                patch("gp_control_plane.strategy_finder._cleanup_blockcheck_processes"),
                patch("gp_control_plane.strategy_finder._cleanup_nft_blockcheck_tables"),
            ):
                result = _run_process_with_live_stdout(
                    command=command,
                    env=os.environ.copy(),
                    stdout_log=stdout_log,
                    stderr_log=stderr_log,
                    debug_stdout_log=None,
                    timeout_seconds=10,
                    stop_event=stop_event,
                    recorder=recorder,
                )
            stopper.join(timeout=2)

            self.assertEqual(result["status"], "stopped")
            self.assertTrue(result["stopped"])
            self.assertIsNotNone(result["returncode"])

    def test_live_recorder_can_close_connection_from_controller_thread(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            progress_log = state_dir / "strategy-finder" / "logs" / "run.progress.json"
            metrics_log = state_dir / "strategy-finder" / "logs" / "run.metrics.ndjson"
            run = {
                "id": "run-threaded",
                "kind": "standard-discovery",
                "status": "running",
                "timestamp": "2026-06-20T00:00:00Z",
                "progress_log": str(progress_log),
                "metrics_log": str(metrics_log),
                "attempt_plan": {
                    "total": 1,
                    "scripts": {"standard/10-test.sh": 1},
                    "script_order": ["standard/10-test.sh"],
                    "source": "test",
                },
            }
            recorder = _LiveStdoutRecorder(state_dir, run)

            def write_success() -> None:
                recorder.record_line("* script : standard/10-test.sh")
                recorder.record_line("- curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=tls_client_hello")
                recorder.record_line("!!!!! AVAILABLE !!!!!")

            thread = threading.Thread(target=write_success)
            thread.start()
            thread.join(timeout=5)

            self.assertFalse(thread.is_alive())
            self.assertEqual(len(recorder.parsed()["candidates"]), 1)

    def test_rotating_text_writer_limits_active_log_size(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "runtime.log"
            with _RotatingTextWriter(path, max_bytes=128) as writer:
                for index in range(60):
                    writer.write(f"line-{index}-{'x' * 40}\n")

            rotated = path.with_suffix(path.suffix + ".1")
            active = path.read_text(encoding="utf-8")

            self.assertTrue(rotated.is_file())
            self.assertLessEqual(path.stat().st_size, 1024)
            self.assertIn("log rotated", active)

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
            self.assertIn("gp_md_normalize_ip_list", text)
            self.assertIn('ips="$(gp_md_normalize_ip_list "$ips")"', text)
            self.assertIn("GP-MULTIDOMAIN no resolved ip addresses for $proto/$port", text)
            self.assertIn('tcp) pktws_ipt_prepare_tcp "$port" "$ips" ;;', text)
            self.assertIn('udp) pktws_ipt_prepare_udp "$port" "$ips" ;;', text)
            self.assertIn('gp_md_run_domain_curl "$idx" "$testf" "$gp_domain" &', text)
            self.assertIn("gp_md_collect_record", text)
            self.assertIn('curl_test "$testf" "$gp_domain"', text)
            self.assertIn("working strategy found for ipv$IPV $gp_domain", text)
            self.assertNotIn('[ "$n" -gt 16 ] && n=16', text)
            self.assertNotIn("echo stock main", text)

            resolve_pos = text.index('ips="$(gp_md_resolve_all_ips)"')
            normalize_pos = text.index('ips="$(gp_md_normalize_ip_list "$ips")"', resolve_pos)
            empty_guard_pos = text.index('[ -n "$ips" ] || {', normalize_pos)
            prepare_pos = text.index("pktws_ipt_prepare_udp", empty_guard_pos)
            self.assertLess(resolve_pos, normalize_pos)
            self.assertLess(normalize_pos, empty_guard_pos)
            self.assertLess(empty_guard_pos, prepare_pos)

    def test_multidomain_run_preserves_ui_curl_parallelism_above_10(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            state_dir = tmp / "state"
            blockcheck = tmp / "blockcheck2.sh"
            blockcheck.write_text("#!/bin/sh\n\nfsleep_setup\necho real\n", encoding="utf-8")
            captured: dict[str, object] = {}
            root_calls: list[tuple[list[str], dict[str, object]]] = []

            def fake_run_blockcheck_command_live(**kwargs: object) -> dict[str, object]:
                captured.update(kwargs)
                return {"status": "success"}

            def fake_root_command(command: list[str], **kwargs: object) -> list[str]:
                root_calls.append((command, kwargs))
                return command

            with (
                patch("gp_control_plane.strategy_finder.shutil.which", return_value=str(blockcheck)),
                patch("gp_control_plane.strategy_finder.root_command", side_effect=fake_root_command),
                patch(
                    "gp_control_plane.strategy_finder._run_blockcheck_command_live",
                    side_effect=fake_run_blockcheck_command_live,
                ),
            ):
                _run_multidomain_blockcheck_live(
                    state_dir=state_dir,
                    domains=["youtube.com"],
                    timeout_seconds=60,
                    options=DiscoveryOptions(),
                    curl_parallelism=30,
                )

            env = captured["env"]
            self.assertIsInstance(env, dict)
            self.assertEqual(env["GP_MD_CURL_PARALLELISM"], "30")
            self.assertEqual(captured["curl_parallelism"], 30)
            self.assertEqual(root_calls[0][0], [str(blockcheck.resolve())])
            self.assertEqual(root_calls[0][1]["helper_command"], "run-multidomain")
            self.assertEqual(captured["command"], [str(blockcheck.resolve())])

    def test_resolve_blockcheck_script_follows_exec_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            real = tmp / "real-blockcheck2.sh"
            real.write_text("#!/bin/sh\n\nfsleep_setup\necho real\n", encoding="utf-8")
            wrapper = tmp / "blockcheck2.sh"
            wrapper.write_text("#!/bin/sh\nexec real-blockcheck2.sh \"$@\"\n", encoding="utf-8")

            self.assertEqual(_resolve_blockcheck_script(wrapper), real.resolve())

    def test_multidomain_progress_eta_uses_elapsed_average_not_parallelism(self) -> None:
        plan = {
            "total": 40,
            "scripts": {"standard/10-test.sh": 40},
            "script_order": ["standard/10-test.sh"],
            "source": "test",
        }

        progress = _progress_from_counts(
            run={
                "id": "run-parallel",
                "kind": "multi-domain-discovery",
                "status": "running",
                "attempt_plan": plan,
                "curl_parallelism": 4,
            },
            attempted=20,
            attempts_by_script={"standard/10-test.sh": 20},
            successful=0,
            current_script="standard/10-test.sh",
            elapsed_seconds_override=40,
        )

        self.assertEqual(progress["remaining_attempts"], 20)
        self.assertEqual(progress["eta_configured_parallelism"], 4)
        self.assertEqual(progress["eta_parallelism"], 1)
        self.assertEqual(progress["eta_seconds"], 40)

    def test_elapsed_average_eta_ignores_live_sample_and_parallelism(self) -> None:
        plan = {
            "total": 100,
            "scripts": {"standard/10-test.sh": 100},
            "script_order": ["standard/10-test.sh"],
            "source": "test",
        }

        progress = _progress_from_counts(
            run={
                "id": "run-live-sample",
                "kind": "multi-domain-discovery",
                "status": "running",
                "attempt_plan": plan,
                "curl_parallelism": 4,
            },
            attempted=20,
            attempts_by_script={"standard/10-test.sh": 20},
            successful=0,
            current_script="standard/10-test.sh",
            runtime_ms_per_attempt=500,
            runtime_sample_count=50,
            elapsed_seconds_override=20,
        )

        self.assertEqual(progress["remaining_attempts"], 80)
        self.assertEqual(progress["eta_status"], "elapsed_average")
        self.assertEqual(progress["eta_method"], "elapsed_average")
        self.assertEqual(progress["eta_ms_per_attempt"], 1000)
        self.assertEqual(progress["eta_configured_parallelism"], 4)
        self.assertEqual(progress["eta_parallelism"], 1)
        self.assertEqual(progress["eta_seconds"], 80)

    def test_progress_elapsed_uses_started_at(self) -> None:
        progress = _progress_from_counts(
            run={
                "id": "finished-run",
                "kind": "multi-domain-discovery",
                "status": "success",
                "started_at": "2026-06-20T00:00:00Z",
                "timestamp": "2999-01-01T00:00:00Z",
                "attempt_plan": {
                    "total": 1,
                    "scripts": {"standard/10-test.sh": 1},
                    "script_order": ["standard/10-test.sh"],
                    "source": "test",
                },
            },
            attempted=1,
            attempts_by_script={"standard/10-test.sh": 1},
            successful=1,
            current_script="standard/10-test.sh",
        )

        self.assertGreater(progress["elapsed_seconds"], 0)

    def test_average_attempt_ms_winsorizes_large_live_outliers(self) -> None:
        timestamps = [0.0]
        intervals = [1.0] * 22 + [20.0] * 3
        for value in intervals:
            timestamps.append(timestamps[-1] + value)

        self.assertLess(_average_attempt_ms(deque(timestamps)), 4000)

    def test_progress_eta_elapsed_average_does_not_multiply_sequential_repeats(self) -> None:
        plan = {
            "total": 10,
            "scripts": {"standard/10-test.sh": 10},
            "script_order": ["standard/10-test.sh"],
            "source": "test",
        }

        progress = _progress_from_counts(
            run={
                "id": "run-repeats",
                "kind": "standard-discovery",
                "status": "running",
                "attempt_plan": plan,
                "repeats": 3,
                "repeat_parallel": False,
            },
            attempted=5,
            attempts_by_script={"standard/10-test.sh": 5},
            successful=0,
            current_script="standard/10-test.sh",
            elapsed_seconds_override=10,
        )

        self.assertEqual(progress["eta_estimate_ms_per_attempt"], 2000)
        self.assertEqual(progress["eta_seconds"], 10)

    def test_running_progress_does_not_report_zero_eta_when_plan_is_underestimated(self) -> None:
        plan = {
            "total": 2,
            "scripts": {"standard/10-test.sh": 2},
            "script_order": ["standard/10-test.sh"],
            "source": "test",
        }
        stdout = "\n".join(["* script : standard/10-test.sh"] + ["- curl_test_https_tls12 ipv4 youtube.com : nfqws2 --one"] * 3)

        progress = progress_from_stdout(stdout, {"id": "run-underestimated", "status": "running", "attempt_plan": plan})

        self.assertEqual(progress["progress_status"], "underestimated")
        self.assertIsNone(progress["eta_seconds"])
        self.assertEqual(progress["eta_status"], "underestimated")
        self.assertEqual(progress["percent"], 99.0)

    def test_running_progress_detects_underestimated_current_script(self) -> None:
        plan = {
            "total": 100,
            "scripts": {"standard/50-fake-multi.sh": 10, "standard/60-next.sh": 90},
            "script_order": ["standard/50-fake-multi.sh", "standard/60-next.sh"],
            "source": "shell",
        }
        stdout = "\n".join(
            ["* script : standard/50-fake-multi.sh"]
            + ["- curl_test_https_tls12 ipv4 youtube.com : nfqws2 --one"] * 15
        )

        progress = progress_from_stdout(
            stdout,
            {"id": "run-script-underestimated", "status": "running", "attempt_plan": plan},
        )

        self.assertEqual(progress["attempted"], 15)
        self.assertEqual(progress["current_script_attempted"], 15)
        self.assertEqual(progress["current_script_attempt_total"], 10)
        self.assertEqual(progress["progress_status"], "underestimated")
        self.assertEqual(progress["effective_attempt_total"], 100)
        self.assertIsNone(progress["remaining_attempts"])
        self.assertIsNone(progress["eta_seconds"])
        self.assertEqual(progress["eta_status"], "underestimated")
        self.assertLess(progress["percent"], 99.0)

    def test_live_summary_is_validation_and_only_fallback_writes_missing_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            progress_log = state_dir / "strategy-finder" / "logs" / "run.progress.json"
            fallback_log = state_dir / "strategy-finder" / "logs" / "run.summary-fallback.ndjson"
            run = {
                "id": "run-summary",
                "kind": "standard-discovery",
                "status": "running",
                "timestamp": "2026-06-20T00:00:00Z",
                "progress_log": str(progress_log),
                "summary_fallback_log": str(fallback_log),
                "domains": ["youtube.com"],
            }
            recorder = _LiveStdoutRecorder(state_dir, run)

            recorder.record_line("* script : standard/10-test.sh")
            recorder.record_line("- curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=live")
            recorder.record_line("!!!!! AVAILABLE !!!!!")
            recorder.record_line("* SUMMARY")
            recorder.record_line("curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=live")
            recorder.record_line("curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=fallback")
            parsed = recorder.parsed()

            self.assertEqual(parsed["summary_verified"], 1)
            self.assertEqual(parsed["summary_fallbacks"], 1)
            self.assertEqual(parsed["summary_line_count"], 2)
            self.assertEqual(parsed["summary"], [])
            self.assertEqual(parsed["results"], [])
            self.assertEqual(len(parsed["candidates"]), 2)
            self.assertTrue(fallback_log.exists())

    def test_live_recorder_flushes_candidate_buffer_on_close(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            recorder = _LiveStdoutRecorder(
                state_dir,
                {
                    "id": "run-buffer-close",
                    "kind": "standard-discovery",
                    "status": "running",
                    "domains": ["youtube.com"],
                },
            )

            recorder.record_line("- curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=one")
            recorder.record_line("!!!!! AVAILABLE !!!!!")

            self.assertEqual(candidate_total(state_dir), 0)

            recorder.close()

            self.assertEqual(candidate_total(state_dir), 1)

    def test_live_recorder_flushes_candidate_buffer_by_size(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch("gp_control_plane.strategy_finder.LIVE_CANDIDATE_FLUSH_SIZE", 2):
            state_dir = Path(raw)
            recorder = _LiveStdoutRecorder(
                state_dir,
                {
                    "id": "run-buffer-size",
                    "kind": "standard-discovery",
                    "status": "running",
                    "domains": ["youtube.com", "discord.com"],
                },
            )

            recorder.record_line("- curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=one")
            recorder.record_line("!!!!! AVAILABLE !!!!!")
            self.assertEqual(candidate_total(state_dir), 0)

            recorder.record_line("- curl_test_https_tls12 ipv4 discord.com : nfqws2 --payload=two")
            recorder.record_line("!!!!! AVAILABLE !!!!!")

            with recorder._lock:
                self.assertEqual(len(recorder._pending_candidate_events), 0)
            recorder.close()
            self.assertEqual(candidate_total(state_dir), 2)

    def test_live_recorder_writes_candidates_on_background_thread(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch("gp_control_plane.strategy_finder.LIVE_CANDIDATE_FLUSH_SIZE", 1):
            state_dir = Path(raw)
            caller_thread = threading.get_ident()
            writer_threads: list[int] = []

            def capture_write(conn, **event):
                writer_threads.append(threading.get_ident())
                storage_upsert_candidate_event_conn(conn, **event)

            recorder = _LiveStdoutRecorder(
                state_dir,
                {
                    "id": "run-writer-thread",
                    "kind": "standard-discovery",
                    "status": "running",
                    "domains": ["youtube.com"],
                },
            )

            with patch("gp_control_plane.strategy_finder.upsert_candidate_event_conn", side_effect=capture_write):
                recorder.record_line("- curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=thread")
                recorder.record_line("!!!!! AVAILABLE !!!!!")
                recorder.close()

            self.assertEqual(candidate_total(state_dir), 1)
            self.assertTrue(writer_threads)
            self.assertNotIn(caller_thread, writer_threads)

    def test_live_recorder_keeps_only_candidate_sample_in_memory(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch("gp_control_plane.strategy_finder.LIVE_CANDIDATE_SAMPLE_LIMIT", 2):
            state_dir = Path(raw)
            recorder = _LiveStdoutRecorder(
                state_dir,
                {
                    "id": "run-candidate-sample",
                    "kind": "standard-discovery",
                    "status": "running",
                    "domains": ["youtube.com"],
                },
            )

            for index in range(5):
                recorder.record_line(f"- curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=item-{index}")
                recorder.record_line("!!!!! AVAILABLE !!!!!")

            parsed = recorder.parsed()

            self.assertEqual(parsed["candidate_count"], 5)
            self.assertEqual(len(parsed["candidates"]), 2)
            self.assertEqual(candidate_total(state_dir), 5)


def _store_candidate_rows(state_dir: Path, candidates: list[dict[str, object]]) -> None:
    parsed: dict[str, list[dict[str, str]]] = {"candidates": [], "common_candidates": []}
    common_domains: list[str] = []
    for candidate in candidates:
        protocol = str(candidate.get("protocol") or "tls")
        args = str(candidate.get("args") or "")
        test = "curl_test_http3" if protocol == "quic" else "curl_test_https_tls12"
        seen_items = candidate.get("seen") if isinstance(candidate.get("seen"), list) else []
        for seen in seen_items:
            if not isinstance(seen, dict):
                continue
            domain = str(seen.get("domain") or "")
            if not domain:
                continue
            parsed["candidates"].append(
                {
                    "domain": domain,
                    "test": test,
                    "ip_version": "4",
                    "protocol": protocol,
                    "args": args,
                }
            )
        common_items = candidate.get("common_seen") if isinstance(candidate.get("common_seen"), list) else []
        for seen in common_items:
            if not isinstance(seen, dict):
                continue
            domains = [str(item or "") for item in seen.get("domains", [])] if isinstance(seen.get("domains"), list) else []
            common_domains = [domain for domain in domains if domain]
            parsed["common_candidates"].append(
                {
                    "domain": "",
                    "test": test,
                    "ip_version": "4",
                    "protocol": protocol,
                    "args": args,
                }
            )
    upsert_candidates(state_dir, parsed, {"id": "run", "domains": common_domains})


if __name__ == "__main__":
    unittest.main()
