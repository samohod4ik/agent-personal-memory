"""Unittest harness for the versioned personal-memory query benchmark."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from run_bench import (  # noqa: E402
    prepare_out_dir,
    run_component_case,
)
from score import load_corpus, score_case  # noqa: E402

REQUIRED_CATEGORIES = frozenset(
    {"acl", "deleted", "stale", "contradictory", "bilingual"}
)
REQUIRED_LANGUAGES = frozenset({"ru", "en"})
PRODUCTION_MARKERS = ("F:\\Hermes\\data", "F:/Hermes/data")


class PersonalMemoryBenchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.corpus = load_corpus(HERE / "corpus.json")
        cls.cases = list(cls.corpus["cases"])
        cls.by_id = {case["id"]: case for case in cls.cases}

    def test_corpus_version_and_required_strata(self) -> None:
        self.assertEqual(self.corpus["corpus_version"], 1)
        self.assertGreaterEqual(len(self.cases), 10)
        categories = {case["category"] for case in self.cases}
        languages = {case["language"] for case in self.cases}
        self.assertEqual(categories, REQUIRED_CATEGORIES)
        self.assertEqual(languages, REQUIRED_LANGUAGES)
        for case in self.cases:
            self.assertIn(case["language"], REQUIRED_LANGUAGES)
            self.assertIn(case["category"], REQUIRED_CATEGORIES)
            self.assertTrue(case["id"])
            self.assertIn("queries", case)
            self.assertTrue(case["queries"])

    def test_each_category_has_ru_and_en_cases(self) -> None:
        by_category: dict[str, set[str]] = {}
        for case in self.cases:
            by_category.setdefault(case["category"], set()).add(case["language"])
        for category in REQUIRED_CATEGORIES:
            self.assertEqual(
                by_category.get(category),
                REQUIRED_LANGUAGES,
                f"{category} must have ru and en cases",
            )

    def test_bilingual_pair_shares_fixture_and_authorized_aliases(self) -> None:
        bilingual = [case for case in self.cases if case["category"] == "bilingual"]
        self.assertGreaterEqual(len(bilingual), 2)
        fixture_ids = {case["fixture_id"] for case in bilingual}
        self.assertEqual(len(fixture_ids), 1)
        include_sets = []
        exclude_sets = []
        prompts = []
        for case in bilingual:
            prompts.append(case["prompt"]["language"])
            includes: set[str] = set()
            excludes: set[str] = set()
            for query in case["queries"]:
                expect = query.get("expect") or {}
                includes.update(expect.get("include_claims") or [])
                excludes.update(expect.get("exclude_claims") or [])
            include_sets.append(includes)
            exclude_sets.append(excludes)
        self.assertEqual(set(prompts), REQUIRED_LANGUAGES)
        self.assertEqual(include_sets[0], include_sets[1])
        self.assertEqual(exclude_sets[0], exclude_sets[1])

    def test_all_corpus_cases_pass(self) -> None:
        for case in self.cases:
            with self.subTest(case_id=case["id"]):
                trajectory = run_component_case(case, repeat=1)
                result = score_case(case, trajectory)
                self.assertTrue(result["passed"], json.dumps(result, ensure_ascii=False))

    def test_component_cases_use_isolated_temp_stores(self) -> None:
        first = run_component_case(self.by_id["contradictory_two_candidates_en"], repeat=1)
        second = run_component_case(self.by_id["acl_cross_tenant_en"], repeat=1)
        first_dir = Path(first["store_dir"])
        second_dir = Path(second["store_dir"])
        temp_root = Path(tempfile.gettempdir()).resolve()
        self.assertNotEqual(first_dir, second_dir)
        self.assertTrue(first_dir.resolve().is_relative_to(temp_root))
        self.assertTrue(second_dir.resolve().is_relative_to(temp_root))
        self.assertFalse(first_dir.exists())
        self.assertFalse(second_dir.exists())
        packed = json.dumps([first, second], default=str)
        for marker in PRODUCTION_MARKERS:
            self.assertNotIn(marker, packed)

    def test_denied_gets_are_byte_identical_to_missing(self) -> None:
        case = self.by_id["acl_cross_tenant_en"]
        trajectory = run_component_case(case, repeat=1)
        result = score_case(case, trajectory)
        self.assertTrue(result["passed"])
        checks = {check["name"]: check for check in result["checks"]}
        self.assertTrue(checks["not_found_identical:alice_get_bob_claim"]["passed"])
        self.assertTrue(checks["not_found_identical:alice_get_bob_source"]["passed"])

    def test_scorer_fails_when_excluded_id_leaks(self) -> None:
        case = self.by_id["acl_cross_tenant_en"]
        trajectory = run_component_case(case, repeat=1)
        leaked = trajectory["alias_ids"]["bob_live"]
        for operation in trajectory["operations"]:
            if operation["id"] == "alice_default_claims":
                operation["claim_ids"].append(leaked)
                operation["rows"].append(
                    {
                        "claim_id": leaked,
                        "state": "candidate",
                        "claim_kind": "preference",
                    }
                )
                break
        result = score_case(case, trajectory)
        self.assertFalse(result["passed"])
        checks = {check["name"]: check for check in result["checks"]}
        self.assertFalse(checks["exclude_claims:alice_default_claims"]["passed"])

    def test_scorer_fails_when_included_id_missing(self) -> None:
        case = self.by_id["contradictory_two_candidates_en"]
        trajectory = run_component_case(case, repeat=1)
        for operation in trajectory["operations"]:
            if operation["id"] == "alice_candidates":
                operation["claim_ids"] = []
                operation["rows"] = []
                break
        result = score_case(case, trajectory)
        self.assertFalse(result["passed"])
        checks = {check["name"]: check for check in result["checks"]}
        self.assertFalse(checks["include_claims:alice_candidates"]["passed"])

    def test_stale_and_restricted_are_excluded_by_default(self) -> None:
        case = self.by_id["stale_expired_default_en"]
        trajectory = run_component_case(case, repeat=1)
        result = score_case(case, trajectory)
        self.assertTrue(result["passed"])
        default_op = next(
            item for item in trajectory["operations"] if item["id"] == "alice_default_claims"
        )
        alias_ids = trajectory["alias_ids"]
        claim_ids = set(default_op["claim_ids"])
        self.assertIn(alias_ids["alice_live"], claim_ids)
        self.assertNotIn(alias_ids["alice_stale"], claim_ids)
        self.assertNotIn(alias_ids["alice_restricted"], claim_ids)
        live = next(
            row for row in default_op["rows"] if row["claim_id"] == alias_ids["alice_live"]
        )
        self.assertEqual(live["state"], "candidate")

    def test_contradictory_filter_returns_both_active_candidates(self) -> None:
        case = self.by_id["contradictory_two_candidates_en"]
        trajectory = run_component_case(case, repeat=1)
        result = score_case(case, trajectory)
        self.assertTrue(result["passed"])
        filtered = next(
            item for item in trajectory["operations"] if item["id"] == "alice_candidates"
        )
        alias_ids = trajectory["alias_ids"]
        self.assertEqual(
            set(filtered["claim_ids"]),
            {alias_ids["alice_tea"], alias_ids["alice_coffee"]},
        )
        states = {row["state"] for row in filtered["rows"]}
        self.assertEqual(states, {"candidate"})

    def test_deleted_rows_are_invisible(self) -> None:
        case = self.by_id["deleted_erased_rows_en"]
        trajectory = run_component_case(case, repeat=1)
        result = score_case(case, trajectory)
        self.assertTrue(result["passed"])
        alias_ids = trajectory["alias_ids"]
        claims = next(
            item for item in trajectory["operations"] if item["id"] == "alice_default_claims"
        )
        sources = next(
            item for item in trajectory["operations"] if item["id"] == "alice_default_sources"
        )
        self.assertNotIn(alias_ids["alice_erased_claim"], claims["claim_ids"])
        self.assertNotIn(alias_ids["alice_erased_source"], sources["source_ids"])

    def test_nonempty_output_directory_is_refused_or_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "results"
            out.mkdir()
            (out / "stale.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                prepare_out_dir(out)
            prepare_out_dir(out, force_clean=True)
            self.assertEqual(list(out.iterdir()), [])


CHECKLIST_FIXTURE_DIR = HERE / "cases"
CHECKLIST_CRITERION_IDS = frozenset(
    {
        "C-POINTER",
        "C-HONEST-ZERO",
        "C-ACCESS",
        "C-REJECTED",
        "C-ABSTAIN",
        "C-SCORE-LOC",
        "C-ACL-3WAY",
    }
)
CHECKLIST_EXPECTS = frozenset(
    {
        "honest_zero",
        "abstain",
        "score_invariant",
        "rejected_options_retained",
        "derived_stale",
    }
)
CHECKLIST_FIXTURES = (
    ("honest_zero_absent.json", "C-HONEST-ZERO", "honest_zero"),
    ("abstention_unknown.json", "C-ABSTAIN", "abstain"),
    ("score_locality_falsifier.json", "C-SCORE-LOC", "score_invariant"),
    ("rejected_alternatives.json", "C-REJECTED", "rejected_options_retained"),
    ("pointer_stale.json", "C-POINTER", "derived_stale"),
)


class ChecklistFixtureStubTests(unittest.TestCase):
    """Optional P1.1 pack. Not loaded by load_corpus / run_bench."""

    def test_checklist_fixtures_exist_parse_and_carry_criterion_id(self) -> None:
        for filename, criterion_id, expect in CHECKLIST_FIXTURES:
            path = CHECKLIST_FIXTURE_DIR / filename
            with self.subTest(filename=filename):
                self.assertTrue(path.is_file(), f"missing checklist fixture {path}")
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertIsInstance(data, dict)
                self.assertTrue(data.get("id"))
                self.assertEqual(data.get("criterion_id"), criterion_id)
                self.assertIn(data["criterion_id"], CHECKLIST_CRITERION_IDS)
                self.assertTrue(data.get("category_note"))
                self.assertEqual(data.get("expect"), expect)
                self.assertIn(data["expect"], CHECKLIST_EXPECTS)
                self.assertNotIn("corpus_version", data)
                self.assertNotIn("cases", data)

    def test_corpus_loader_ignores_checklist_fixture_pack(self) -> None:
        corpus = load_corpus(HERE / "corpus.json")
        corpus_ids = {case["id"] for case in corpus["cases"]}
        categories = {case["category"] for case in corpus["cases"]}
        self.assertEqual(categories, REQUIRED_CATEGORIES)
        for filename, _criterion_id, _expect in CHECKLIST_FIXTURES:
            path = CHECKLIST_FIXTURE_DIR / filename
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn(data["id"], corpus_ids)
            with self.assertRaises(ValueError):
                load_corpus(path)


if __name__ == "__main__":
    unittest.main()
