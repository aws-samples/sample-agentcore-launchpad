"""Hermetic tests for scripts/quality_metrics.py — tiny fixtures, no AWS, not part of `make verify`.

Run: `make metrics-test` (= `python3 scripts/tests/test_quality_metrics.py`).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import quality_metrics as qm  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "quality_metrics")
REPO = os.path.join(FIX, "repo")
LEDGER = os.path.join(FIX, "extern", "deploy-ops-ledger.md")
VERIFY_GLOB = os.path.join(FIX, "extern", "verify-fixture-*.txt")
TODAY = dt.date(2026, 1, 20)


class StorylineTests(unittest.TestCase):
    def setUp(self):
        self.runs = {r["run"]: r for r in qm.collect_runs(REPO)}

    def test_run_summary_counts_and_env_class(self):
        r = self.runs["round-1/run-a"]
        self.assertEqual((r["passed"], r["failed"], r["skipped"]), (3, 1, 1))  # flaky counts as pass
        self.assertEqual(r["counts_source"], "run-summary.json")
        self.assertEqual(r["date"], "2026-01-10")
        self.assertEqual(r["host_sha"], "abcdef12")
        self.assertEqual(r["duration_s"], 9000)
        self.assertEqual(r["verdict"], "FAIL")
        self.assertEqual(r["failures"], [{"step": "s05b", "spec": "s05-use.spec.ts", "class": "ENV", "note": None}])
        self.assertEqual(r["failures_by_class"]["ENV"], 1)

    def test_text_only_report_test_class_and_cascade(self):
        r = self.runs["round-1/run-b"]
        self.assertEqual((r["passed"], r["failed"], r["skipped"]), (8, 2, 0))
        self.assertEqual(r["counts_source"], "regex")
        self.assertEqual(r["host_sha"], "12345678")
        by_step = {f["step"]: f for f in r["failures"]}
        self.assertEqual(by_step["s08c"]["class"], "TEST")
        self.assertEqual(by_step["s09"]["class"], "TEST")
        self.assertEqual(by_step["s09"]["note"], "cascade of s08c")
        self.assertEqual(r["failures_by_class"], {"ENV": 0, "TEST": 2, "PRODUCT": 0, "UNCLASSIFIED": 0})

    def test_classifier_precedence(self):
        self.assertEqual(qm.classify_failure("**PRODUCT** but the text says internalServerException"), "PRODUCT")
        self.assertEqual(qm.classify_failure("Throttling on one call; test-side nit"), "ENV")
        self.assertEqual(qm.classify_failure("Error: scenario state 'runtime' missing"), "TEST")
        self.assertEqual(qm.classify_failure("nothing recognisable"), "UNCLASSIFIED")


class QuarantineTests(unittest.TestCase):
    def test_active_count_and_age(self):
        q = qm.collect_quarantine(REPO, TODAY)
        self.assertEqual(q["total_rows"], 2)
        self.assertEqual(q["count"], 1)
        self.assertEqual(q["max_age_days"], 15)  # 2026-01-05 → 2026-01-20
        active = [e for e in q["entries"] if e["active"]][0]
        self.assertEqual(active["spec"], "s07-evaluate.spec.ts")
        self.assertIn("N12-obs53", active["ids"])


class RegisterTests(unittest.TestCase):
    def setUp(self):
        self.reg = qm.collect_registers(REPO)
        self.rounds = {r["round"]: r for r in self.reg["rounds"]}

    def test_rounds_states_and_severity(self):
        r1 = self.rounds["round 1"]
        self.assertEqual(r1["ids"], 2)  # the fix-table row is a state update of N10-bug1, not a new id
        self.assertEqual(r1["fixed"], 1)
        self.assertEqual(r1["recorded"], 1)
        self.assertEqual(r1["live_retest_pending"], 1)
        self.assertEqual(r1["by_severity"]["High"], 1)
        self.assertEqual(r1["by_severity"]["Low"], 1)
        r2 = self.rounds["round 2"]
        self.assertEqual(r2["ids"], 4)
        self.assertEqual((r2["fixed"], r2["pending"], r2["n_a"]), (1, 2, 1))
        self.assertEqual(r2["by_severity"]["High"], 1)  # P1 → High
        self.assertEqual(r2["by_severity"]["Medium"], 1)

    def test_user_reported_and_escape_ratio(self):
        r2 = self.rounds["round 2"]
        self.assertEqual(r2["user_reported"], 2)  # WB-001 + "(user-reported)"
        self.assertEqual(self.reg["totals"]["ids"], 13)  # 2 + 4 + 7 (round 3 fixture)
        self.assertAlmostEqual(self.reg["defect_escape_ratio"], 3 / 13)  # + `found-by: user` in round 3

    def test_lower_case_severity_cells_count(self):
        r3 = self.rounds["round 3"]
        self.assertEqual(r3["by_severity"]["High"], 2)  # P1 + "High"
        self.assertEqual(r3["by_severity"]["Medium"], 2)  # P2 × 2
        self.assertEqual(r3["by_severity"]["Low"], 2)  # "P3 (PRODUCT)" + "Low"


class DiscoveryTests(unittest.TestCase):
    """Round-16 markers on the round-3 fixture (bug-discovery plan §0 KPIs)."""

    def setUp(self):
        reg = qm.collect_registers(REPO)
        self.d = {r["round"]: r["discovery"] for r in reg["rounds"]}["round 3"]
        self.legacy = {r["round"]: r["discovery"] for r in reg["rounds"]}["round 2"]

    def test_classes_weights_and_process_rows(self):
        d = self.d
        self.assertEqual(d["findings"], 6)  # N16-obs60 is `kind: process`; the obs4 state update is not a new id
        self.assertEqual(d["by_class"], {"PRODUCT": 3, "TEST": 1, "ENV": 0, "unclassified": 2})  # marker, else the upper-case word
        self.assertEqual(d["weighted_score"], 3 + 3 + 1 + 5 + 5 + 1)  # P2 P2 P3 P1 High Low
        self.assertEqual(d["by_module"], {"E02": 1, "E03": 2, "E04": 1, "E11": 1, "E18": 1})

    def test_escaped_and_channels(self):
        d = self.d
        self.assertEqual(d["escaped"], 1)  # `found-by: user`
        self.assertAlmostEqual(d["escape_ratio"], 1 / 6)
        self.assertEqual(d["channels"], {"A": 2, "B": 4, "C": 0, "untagged": 1})  # `A+B` counts in both

    def test_capture_recapture_and_exploratory_yield(self):
        d = self.d
        self.assertEqual(d["overlap_ab"], 2)  # obs1 ~ obs2 by repro-hash + obs4 tagged A+B
        self.assertAlmostEqual(d["estimate_total"], 4.0)  # Chapman: 3 · 5 / 3 − 1
        self.assertAlmostEqual(d["estimate_remaining"], 0.0)  # 4 − (2 + 4 − 2)
        self.assertAlmostEqual(d["session_hours"], 3.5)  # B-E03-1 once (1.5 h, two rows) + B-E11-1 (2 h)
        self.assertAlmostEqual(d["exploratory_yield"], 4 / 3.5)

    def test_legacy_register_has_no_estimate(self):
        self.assertEqual(self.legacy["channels"]["untagged"], self.legacy["findings"])
        self.assertIsNone(self.legacy["estimate_total"])
        self.assertIsNone(self.legacy["exploratory_yield"])

    def test_repro_hash_convention(self):
        self.assertEqual(qm.repro_hash("  Upload a 1-byte ZIP   via BYOC "), qm.repro_hash("upload a 1-byte zip via byoc"))
        self.assertRegex(qm.repro_hash("x"), r"^[0-9a-f]{8}$")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = qm.main(["--repro-hash", "upload a 1-byte zip via byoc"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), qm.repro_hash("upload a 1-byte zip via byoc"))


class MutationTests(unittest.TestCase):
    def test_latest_baseline_sides_and_totals(self):
        m = qm.collect_mutation(REPO)
        self.assertEqual(m["date"], "2026-01-18")
        be = m["sides"]["backend"]
        self.assertEqual(len(be["files"]), 2)
        self.assertEqual(be["total"]["mutants"], 200)
        self.assertAlmostEqual(be["total"]["score"], 68.0)  # computed: 136 / (136 + 44 + 0 + 20)
        self.assertAlmostEqual(be["total"]["score_covered"], 75.6)
        fe = m["sides"]["frontend"]
        self.assertAlmostEqual(fe["total"]["score"], 65.0)  # the report's own **total** row wins
        self.assertEqual(fe["files"][0]["uncovered"], 3)  # `no coverage` column

    def test_missing_baseline(self):
        empty = tempfile.mkdtemp()
        try:
            self.assertIsNone(qm.collect_mutation(empty)["report"])
        finally:
            shutil.rmtree(empty)


class SecurityOpsGateTests(unittest.TestCase):
    def test_security_counts(self):
        s = qm.collect_security(REPO)
        self.assertEqual(s["date"], "2026-01-15")
        self.assertEqual(s["counts"], {"Critical": 2, "High": 13, "Medium": 22, "Low": 3, "Info": 0})
        self.assertEqual(len(s["surfaces"]), 3)

    def test_ops_ledger(self):
        o = qm.collect_ops(LEDGER)
        self.assertEqual(o["lines"], 7)
        self.assertEqual(o["updates_per_host"], {"default": 1, "t1": 1})
        t1 = [u for u in o["host_updates"] if u["host"] == "t1"][0]
        self.assertEqual((t1["from"], t1["to"], t1["minutes"]), ("e15390af", "10254862", 9.0))
        self.assertEqual(o["restarts"], 2)
        self.assertEqual(len(o["rollbacks"]), 1)  # --disable-rollback and the canary rollback do not count
        self.assertEqual(len(o["drills"]), 1)
        self.assertEqual(o["drills"][0]["restore_s"], 3.1)
        self.assertTrue(o["drills"][0]["reverted"])

    def test_gate_logs(self):
        g = qm.collect_gate(VERIFY_GLOB, 10)
        self.assertEqual([r["verdict"] for r in g["runs"]], ["FAIL", "PASS"])  # sorted by mtime
        self.assertEqual([r["rc"] for r in g["runs"]], [1, 0])
        self.assertEqual(g["runs"][0]["pytest_s"], 1876.6)  # the longest suite, not the infra one
        self.assertEqual(g["runs"][0]["failed_sections"], ["pytest"])
        self.assertEqual((g["pass"], g["total"]), (1, 2))

    def test_missing_sources_are_empty_not_fatal(self):
        empty = tempfile.mkdtemp()
        try:
            snap = qm.build_snapshot(empty, os.path.join(empty, "none.md"), os.path.join(empty, "none-*.log"), TODAY, 5)
            self.assertEqual(snap["storyline"], [])
            self.assertEqual(snap["quarantine"]["count"], 0)
            self.assertEqual(snap["registers"]["totals"]["ids"], 0)
            self.assertIsNone(snap["security"]["report"])
            self.assertIsNone(snap["ops"]["ledger"])
            self.assertEqual(snap["gate"]["runs"], [])
            md = qm.render_markdown(snap, None, empty)
            self.assertIn("Quality metrics board", md)
        finally:
            shutil.rmtree(empty)


class EndToEndTests(unittest.TestCase):
    def run_cli(self, out_dir, date):
        out = os.path.join(out_dir, "quality-metrics.md")
        hist = os.path.join(out_dir, "history.jsonl")
        with contextlib.redirect_stdout(io.StringIO()):
            rc = qm.main(["--repo", REPO, "--ops-ledger", LEDGER, "--verify-glob", VERIFY_GLOB, "--date", date, "--out", out, "--history", hist])
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8") as fh:
            md = fh.read()
        with open(hist, encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh if line.strip()]
        return md, lines

    def test_deterministic_idempotent_redacted_and_trending(self):
        d = tempfile.mkdtemp()
        try:
            md1, hist1 = self.run_cli(d, "2026-01-20")
            md2, hist2 = self.run_cli(d, "2026-01-20")
            self.assertEqual(md1, md2)  # deterministic
            self.assertEqual(len(hist2), 1)  # idempotent per day
            self.assertEqual(hist2[0]["date"], "2026-01-20")
            self.assertEqual(hist2[0]["kpis"]["register_ids"], 13)
            self.assertEqual(hist2[0]["kpis"]["discovery_round"], "round 3")
            self.assertEqual(hist2[0]["kpis"]["discovery_weighted"], 18)
            self.assertAlmostEqual(hist2[0]["kpis"]["mutation_score_backend"], 68.0)
            self.assertAlmostEqual(hist2[0]["kpis"]["mutation_score_frontend"], 65.0)
            for leak in ("SHOULD-NOT-APPEAR", "AKIAABCDEFGHIJKLMNOP"):
                self.assertNotIn(leak, md1)
                self.assertNotIn(leak, json.dumps(hist2))
            self.assertIn("## Summary (trend vs previous snapshot)", md1)
            self.assertIn("## 7. Discovery", md1)
            self.assertIn("## 8. Mutation testing", md1)
            self.assertIn("| round 3 | 6 | 3 | 1 | 0 | 2 | 18 | 1 | 0.167 | 2 | 4 | 0 | 1 | 2 | 4.0 | 0.000 | 3.5 | 1.1 |", md1)
            self.assertIn("12. Mutation score (baseline 2026-01-18): backend 68.0 %", md1)
            self.assertIn("(no previous)", md1)
            # second day: the trend lines compare against the first snapshot
            md3, hist3 = self.run_cli(d, "2026-01-21")
            self.assertEqual([h["date"] for h in hist3], ["2026-01-20", "2026-01-21"])
            self.assertIn("previous snapshot 2026-01-20", md3)
            self.assertIn("→ unchanged", md3)
            self.assertNotIn("(no previous)", md3)
            self.assertEqual(len([l for l in md3.splitlines() if l[:3] in ("1. ", "2. ", "3. ", "4. ", "5. ", "6. ", "7. ", "8. ", "9. ", "10.")]), 10)
        finally:
            shutil.rmtree(d)

    def test_redact(self):
        self.assertNotIn("hunter2", qm.redact("password=hunter2 ok"))
        self.assertNotIn("AKIA", qm.redact("key AKIAABCDEFGHIJKLMNOP here"))
        self.assertEqual(qm.redact("host-update done e15390af → 10254862"), "host-update done e15390af → 10254862")




class GraphCoverageTests(unittest.TestCase):
    def test_code_span_keeps_underscores(self):
        self.assertEqual(qm.code_span("`chat_session`"), "chat_session")
        self.assertEqual(qm.code_span("**total**"), "total")
        self.assertEqual(qm.strip_md("`a_b`"), "ab")  # the general cell helper is unchanged

    def test_total_row_and_per_graph_rows(self):
        g = qm.collect_graph(REPO)
        self.assertEqual(g["report"], os.path.join("docs", "cn", "reports", "graph-coverage.md"))
        self.assertEqual([r["graph"] for r in g["graphs"]], ["agent", "chat_session"])
        self.assertEqual(g["total"]["edges"], 50)
        self.assertEqual(g["total"]["edges_covered"], 49)
        self.assertEqual(g["total"]["prime_paths"], 67)
        self.assertEqual(g["total"]["prime_paths_covered"], 66)
        self.assertEqual(g["total"]["gaps"], 6)

    def test_missing_report_is_none_everywhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = qm.collect_graph(tmp)
        self.assertIsNone(g["report"])
        self.assertIsNone(g["total"])

    def test_snapshot_carries_the_graph_keys(self):
        snap = qm.build_snapshot(REPO, LEDGER, VERIFY_GLOB, TODAY, 5)
        self.assertEqual(snap["kpis"]["graph_edges_covered"], 49)
        self.assertEqual(snap["kpis"]["graph_prime_paths_covered"], 66)
        self.assertEqual(snap["kpis"]["graph_edges"], 50)
        self.assertEqual(snap["kpis"]["graph_prime_paths"], 67)
        text = qm.render_markdown(snap, None, REPO)
        self.assertIn("7a. Lifecycle-graph coverage", text)
        self.assertIn("| **total** | 50 | **49** | 67 | **66** |", text)


class LoopEngineTests(unittest.TestCase):
    """Round 17 (r17-b): the loop-engine table, the deep-dive capture–recapture and the stop rule."""

    def test_loop_table_rows_and_total(self):
        g = qm.collect_graph(REPO)["loop"]
        self.assertEqual([r["graph"] for r in g["graphs"]], ["agent", "chat_session"])
        chat = g["graphs"][1]
        self.assertEqual((chat["walks"], chat["walk_findings"], chat["neighbourhood_paths"], chat["neighbourhood_new"]), (8, 1, 168, 53))
        self.assertEqual((chat["cross_walks"], chat["cross_findings"]), (12, 2))  # the leading int of `12 (agent_x_chat 6, …)`
        self.assertEqual(g["total"]["walks"], 16)
        self.assertEqual(g["total"]["cross_walks"], 12)

    def test_loop_total_is_summed_when_the_total_row_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "docs", "cn", "reports")
            os.makedirs(d)
            with open(os.path.join(d, "graph-coverage.md"), "w", encoding="utf-8") as fh:
                fh.write("| graph | walks | walk findings | neighbourhood paths | neighbourhood newly covered | cross-graph walks | cross findings |\n"
                         "|---|---|---|---|---|---|---|\n| `a` | 3 | 1 | 10 | 4 | 0 | 0 |\n| `b_c` | 5 | 0 | 0 | 0 | 6 | 1 |\n")
            g = qm.collect_graph(tmp)["loop"]
        self.assertEqual([r["graph"] for r in g["graphs"]], ["a", "b_c"])
        self.assertEqual(g["total"]["walks"], 8)
        self.assertEqual(g["total"]["neighbourhood_new"], 4)

    def test_snapshot_carries_the_loop_keys_and_the_stop_rule(self):
        snap = qm.build_snapshot(REPO, LEDGER, VERIFY_GLOB, TODAY, 5)
        k = snap["kpis"]
        self.assertEqual(k["graph_walks"], 16)
        self.assertEqual(k["graph_walk_findings"], 1)
        self.assertEqual(k["neighbourhood_paths"], 168)
        self.assertEqual(k["graph_cross_walks"], 12)
        # 66 / 67 prime paths covered in the fixture
        self.assertEqual(k["graph_stop_prime_pct"], 98.5)
        # the fixture registers carry no graph-channel rows: every round is a zero-finding round
        rounds = len(snap["registers"]["rounds"])
        self.assertEqual(k["graph_stop_zero_rounds"], rounds)
        latest = snap["registers"]["rounds"][-1]["discovery"]
        self.assertEqual(k["graph_stop_est_remaining"], latest["estimate_remaining"])
        self.assertEqual(snap["stop_rule"]["zero_finding_rounds"], rounds)
        # the user's loop rule from the fixture's two round lines
        self.assertEqual((k["loop_round"], k["loop_round_findings"], k["loop_zero_streak"]), (2, 0, 1))
        self.assertEqual(k["loop_round_method"], "fault-injected walks")
        self.assertFalse(k["loop_stop"])
        self.assertTrue(k["loop_switch_method"])  # a zero-yield round → a different method next
        text = qm.render_markdown(snap, None, REPO)
        self.assertIn("7b. Loop engine", text)
        self.assertIn("| **total** | **16** | 1 | 168 | 53 | **12** | 2 |", text)
        self.assertIn("**Loop rule (user ruling 2026-10-19):** round 2 · fault-injected walks · findings 0 · zero-yield streak 1/3 · next round: a DIFFERENT method", text)
        self.assertIn("| 1 | random walks + neighbourhood + cross-graph + soak | 6 | N17-obs10, N17-obs11, N17-obs12, N17-obs13, N17-obs14, N17-obs15 | 0 |", text)
        self.assertIn(f"**Reference metrics:** prime paths 98.5 % covered · {rounds} consecutive register(s) without a graph finding · capture–recapture remaining {qm.fmt_val(latest['estimate_remaining'])}", text)
        summary = "\n".join(qm.summary_lines(snap, None))
        self.assertIn("loop engine: walks 16 (1 findings), neighbourhood paths 168, cross-graph walks 12 (2 findings)", summary)
        self.assertIn("loop rule: round 2 · fault-injected walks", summary)

    def test_graph_rows_and_deep_dive_paths_from_a_register(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "docs", "cn", "reports")
            os.makedirs(d)
            with open(os.path.join(d, "bugs-round-9.md"), "w", encoding="utf-8") as fh:
                fh.write("# r9\n\n| id | subsystem | severity | symptom |\n|---|---|---|---|\n"
                         "| N9-obs1 (channel: A / r random walks over `eval_run`; class: TEST; module: E11; repro-hash: aaaaaaaa) | lifecycle graph `eval_run` | low | x |\n"
                         "| N9-obs2 (channel: B; class: PRODUCT; module: E11; repro-hash: aaaaaaaa; session: s1/2h) | eval | medium | same defect seen by B |\n"
                         "| N9-obs3 (channel: A; class: PRODUCT; module: E13) | canary | medium | y |\n"
                         "| N9-obs4 (channel: A / cross-graph walk; class: DOC; module: E04) | chat | low | z |\n"
                         "| N9-obs5 (kind: process; channel: A) | process | PROCESS | ledger |\n")
            reg = qm.collect_registers(tmp)
        d = reg["rounds"][0]["discovery"]
        self.assertEqual(d["findings"], 4)
        self.assertEqual(d["graph_findings"], 2)  # obs1 (random walks / lifecycle graph) + obs4 (cross-graph walk)
        paths = d["paths"]
        ev = paths["evaluation (E09–E12/E14)"]
        self.assertEqual((ev["findings"], ev["n_a"], ev["n_b"], ev["overlap"]), (2, 1, 1, 1))
        self.assertEqual(ev["estimate_total"], 1.0)   # (1+1)(1+1)/(1+1) − 1
        self.assertEqual(ev["estimate_remaining"], 0.0)
        cd = paths["create-deploy-invoke (E03/E04)"]
        self.assertEqual((cd["findings"], cd["n_a"], cd["n_b"]), (1, 1, 0))
        self.assertIsNone(cd["estimate_total"])
        self.assertEqual(paths["assistant + canary (E02/E13)"]["findings"], 1)
        stop = qm.stop_rule({"prime_paths": 10, "prime_paths_covered": 10}, reg)
        self.assertEqual((stop["prime_paths_pct"], stop["zero_finding_rounds"]), (100.0, 0))

    def test_reference_metrics(self):
        reg = {"rounds": [
            {"discovery": {"graph_findings": 3, "estimate_remaining": 5.0}},
            {"discovery": {"graph_findings": 0, "estimate_remaining": 2.5}},
            {"discovery": {"graph_findings": 0, "estimate_remaining": 1.0}},
        ]}
        stop = qm.stop_rule({"prime_paths": 4, "prime_paths_covered": 4}, reg)
        self.assertEqual(stop, {"prime_paths_pct": 100.0, "zero_finding_rounds": 2, "estimate_remaining": 1.0})
        self.assertEqual(qm.stop_rule(None, {"rounds": []}), {"prime_paths_pct": None, "zero_finding_rounds": 0, "estimate_remaining": None})

    def test_loop_rule_streak_switch_and_stop(self):
        text = ("- round 1 · walks · findings 2 (A-obs1, A-obs2) · streak 0\n"
                "- round 2 · cross-graph · findings 0 · streak 1\n"
                "- round 3 · fault-injected walks · findings 0 · streak 2\n"
                "- round 4 · time-shifted walks · findings 0 · streak 3 · STOP\n")
        rounds = [{"round": int(m.group("n")), "method": m.group("method"), "findings": int(m.group("k")), "streak": int(m.group("streak"))}
                  for m in qm.LOOP_ROUND_RE.finditer(text)]
        self.assertEqual([r["method"] for r in rounds], ["walks", "cross-graph", "fault-injected walks", "time-shifted walks"])
        rule = qm.loop_rule(rounds)
        self.assertEqual((rule["round"], rule["streak"], rule["stop"], rule["switch_method"]), (4, 3, True, True))
        rule = qm.loop_rule(rounds[:1])
        self.assertEqual((rule["streak"], rule["stop"], rule["switch_method"]), (0, False, False))
        self.assertEqual(qm.loop_rule([])["round"], None)
        self.assertIn("STOP — three consecutive zero-yield rounds", qm.stop_rule_text(
            {"loop_round": 4, "loop_round_method": "time-shifted walks", "loop_round_findings": 0, "loop_zero_streak": 3, "loop_stop": True, "loop_switch_method": True}))


if __name__ == "__main__":
    unittest.main()
