"""Issue #4: Russian case forms and stop words in fact ranking (T1-NORM)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_personal_memory.learn import remember_personal_insight
from agent_personal_memory.ranking import (
    extract_entities,
    rank_claims,
    score_claim_row,
    tokenize_query,
)
from agent_personal_memory.store import PersonalMemoryStore
from agent_personal_memory.types import Actor

TENANT_A = Actor("test", "user-a")
F1 = "у меня ноутбук с видеокартой RTX 3060"
F2 = "я предпочитаю утренние созвоны"
F3 = "у меня есть кот"
F4 = "я не ем мясо"
FACTS = (F1, F2, F3, F4)


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8")
    return ""


class StemmedRankingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pm-rank-ru-")
        self.store = PersonalMemoryStore(data_dir=Path(self.tmp.name))
        for text in FACTS:
            remember_personal_insight(TENANT_A, text, store=self.store)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _contents(self, query: str) -> list[str]:
        rows = rank_claims(
            TENANT_A, query, states=["confirmed"], store=self.store
        )
        return [_as_text(row.get("content")) for row in rows]

    def test_q1_nominative_finds_instrumental(self) -> None:
        self.assertEqual(self._contents("видеокарта")[0], F1)

    def test_q9_plural_finds_instrumental(self) -> None:
        self.assertEqual(self._contents("видеокарты")[0], F1)

    def test_q3_adjective_and_noun_stems_match(self) -> None:
        self.assertEqual(self._contents("утренний созвон")[0], F2)

    def test_q4_exact_word(self) -> None:
        self.assertEqual(self._contents("кот")[0], F3)

    def test_q5_exact_word(self) -> None:
        self.assertEqual(self._contents("мясо")[0], F4)

    @unittest.expectedFailure
    def test_q2_verb_form_does_not_match_noun_stem(self) -> None:
        # Known stemming limit: «созваниваться» stems to «созванива» while
        # «созвоны» stems to «созвон», so this query cannot match F2.
        self.assertEqual(self._contents("когда мне удобнее созваниваться?")[0], F2)

    def test_q6_stop_words_do_not_pull_unrelated_fact(self) -> None:
        contents = self._contents("какая видеокарта у меня в ноутбуке")
        self.assertEqual(contents[0], F1)
        self.assertNotIn(F3, contents)

    def test_q7_identifier_tokens_still_match(self) -> None:
        self.assertEqual(self._contents("RTX 3060")[0], F1)

    def test_q8_bare_number_still_matches(self) -> None:
        self.assertEqual(self._contents("3060")[0], F1)

    def test_q10_unrelated_query_returns_nothing(self) -> None:
        self.assertEqual(self._contents("который час"), [])


class ScoreClaimRowStemTests(unittest.TestCase):
    def test_stop_word_only_query_matches_as_before(self) -> None:
        row = {"content": F3.encode("utf-8"), "explanation": "note"}
        before = score_claim_row(
            row,
            query_tokens=tokenize_query("у меня"),
            entities=extract_entities("у меня"),
        )
        normalized = score_claim_row(
            row,
            query_tokens=tokenize_query("у меня"),
            entities=extract_entities("у меня"),
            query_text="у меня",
        )
        self.assertEqual(normalized, before)

    def test_stop_word_entity_yields_no_entity_match(self) -> None:
        row = {"content": F3.encode("utf-8"), "explanation": "note"}
        _, kinds = score_claim_row(
            row,
            query_tokens=tokenize_query("какая видеокарта у меня в ноутбуке"),
            entities=["меня"],
            query_text="какая видеокарта у меня в ноутбуке",
        )
        self.assertNotIn("entity", kinds)

    def test_query_text_none_keeps_old_entity_stopword_behavior(self) -> None:
        row = {"content": F3.encode("utf-8"), "explanation": "note"}
        _, kinds = score_claim_row(
            row,
            query_tokens=tokenize_query("у меня"),
            entities=["меня"],
        )
        self.assertIn("entity", kinds)


if __name__ == "__main__":
    unittest.main()
