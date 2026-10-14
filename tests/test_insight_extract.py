"""Deterministic native insight extraction and secret filters."""

from __future__ import annotations

import unittest

from agent_personal_memory.engines.native import NativeMemoryEngine
from agent_personal_memory.engines.types import ExtractionRequest
from agent_personal_memory.insight_extract import extract_insights
from agent_personal_memory.types import Actor
from agent_personal_memory.validation import ValidationError, validate_claim_content

ALICE = Actor(platform="telegram", user_id="3101", display_name="Alice")


class InsightExtractTests(unittest.TestCase):
    def test_extracts_preference_style_and_fact(self) -> None:
        insights = extract_insights(
            "I prefer window seats. Call me Sasha. I live in Moscow."
        )
        kinds = {item.claim_kind for item in insights}
        texts = " ".join(item.content.lower() for item in insights)
        self.assertIn("preference", kinds)
        self.assertTrue(any(item.claim_kind == "confirmed_fact" for item in insights))
        self.assertIn("window", texts)
        self.assertIn("sasha", texts)
        self.assertIn("moscow", texts)

    def test_explicit_remember_cue_keeps_remainder(self) -> None:
        insights = extract_insights("запомни: я не ем мясо")
        self.assertTrue(insights)
        self.assertIn("мясо", insights[0].content.lower())
        self.assertEqual(insights[0].claim_kind, "preference")

    def test_russian_preference_and_decision(self) -> None:
        insights = extract_insights(
            "Я предпочитаю тихие отели. Я решил летать утром."
        )
        kinds = {item.claim_kind for item in insights}
        self.assertIn("preference", kinds)
        self.assertIn("decision", kinds)

    def test_rejects_secrets_payment_and_id_docs(self) -> None:
        self.assertEqual(extract_insights("запомни мой пароль hunter2secret"), [])
        self.assertEqual(
            extract_insights("remember my api key sk-" + "a1B2" * 12),
            [],
        )
        self.assertEqual(
            extract_insights("I prefer paying with 4111 1111 1111 1111"),
            [],
        )
        self.assertEqual(
            extract_insights("remember passport number 123456789"),
            [],
        )
        with self.assertRaises(ValidationError):
            validate_claim_content(b"pay with 4111111111111111")

    def test_skips_questions_and_ephemeral_travel_search(self) -> None:
        self.assertEqual(extract_insights("Which seat do I prefer on flights?"), [])
        self.assertEqual(extract_insights("найди билеты в Сочи на завтра"), [])
        self.assertEqual(extract_insights("hello"), [])

    def test_per_turn_budget_caps_insights(self) -> None:
        text = (
            "I prefer tea. I like quiet hotels. I live in Berlin. "
            "Call me Alex. I decided to fly mornings."
        )
        insights = extract_insights(text)
        self.assertLessEqual(len(insights), 3)

    def test_native_engine_returns_candidates(self) -> None:
        engine = NativeMemoryEngine()
        result, candidates = engine.extract_candidates(
            ExtractionRequest(
                actor=ALICE,
                text="I prefer aisle seats.",
                source_ids=("src-1",),
            )
        )
        self.assertEqual(result.engine, "native")
        self.assertFalse(result.degraded)
        self.assertTrue(candidates)
        self.assertEqual(candidates[0].claim_kind, "preference")
        self.assertIn(b"aisle", candidates[0].content.lower())
        self.assertEqual(candidates[0].source_ids, ("src-1",))


if __name__ == "__main__":
    unittest.main()
