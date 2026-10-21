"""Deterministic ru/en token normalization, stemming and stop words (T1-NORM)."""

from __future__ import annotations

import unittest

from agent_personal_memory.text_norm import (
    NORMALIZER_VERSION,
    Token,
    query_terms,
    stem_text,
    tokenize,
)


class TokenizeTests(unittest.TestCase):
    def test_version_is_pinned(self) -> None:
        self.assertEqual(NORMALIZER_VERSION, 1)

    def test_russian_case_forms_share_one_stem(self) -> None:
        stems = {
            tokenize(word)[0].stem
            for word in ("видеокарта", "видеокартой", "видеокарты")
        }
        self.assertEqual(stems, {"видеокарт"})

    def test_known_russian_stems(self) -> None:
        self.assertEqual(tokenize("созвоны")[0].stem, "созвон")
        self.assertEqual(tokenize("созваниваться")[0].stem, "созванива")

    def test_yo_is_folded_to_e(self) -> None:
        token = tokenize("ёлка")[0]
        self.assertEqual(token.raw, "елка")
        self.assertNotIn("ё", token.stem)

    def test_short_tokens_are_dropped(self) -> None:
        self.assertEqual(tokenize("я у в а"), [])

    def test_protected_tokens_keep_raw_stem(self) -> None:
        samples = (
            "RTX",
            "3060",
            "rtx3060",
            "abcдеф",
            "abcdefab-cdef-abcd-efab-abcdefabcdef",
            "2026-10-19",
        )
        for text in samples:
            with self.subTest(text=text):
                tokens = tokenize(text)
                self.assertTrue(tokens)
                for token in tokens:
                    self.assertIsInstance(token, Token)
                    self.assertTrue(token.protected, token)
                    self.assertEqual(token.stem, token.raw)

    def test_unprotected_token_is_stemmed(self) -> None:
        token = tokenize("ноутбуке")[0]
        self.assertFalse(token.protected)
        self.assertEqual(token.stem, "ноутбук")


class QueryTermsTests(unittest.TestCase):
    def test_stop_words_drop_when_content_terms_remain(self) -> None:
        terms = query_terms("какая видеокарта у меня в ноутбуке")
        self.assertEqual([term.raw for term in terms], ["видеокарта", "ноутбуке"])
        self.assertTrue(all(not term.stop for term in terms))

    def test_stop_word_flags(self) -> None:
        flagged = {token.raw: token.stop for token in tokenize("что это кот")}
        self.assertEqual(flagged, {"что": True, "это": True, "кот": False})
        flagged_en = {token.raw: token.stop for token in tokenize("what is my seat")}
        self.assertEqual(flagged_en, {"what": True, "is": True, "my": True, "seat": False})

    def test_only_stop_words_keeps_tokens(self) -> None:
        terms = query_terms("у меня")
        self.assertEqual([term.raw for term in terms], ["меня"])
        self.assertTrue(all(term.stop for term in terms))

    def test_dedup_by_stem(self) -> None:
        terms = query_terms("видеокарта видеокартой видеокарты")
        self.assertEqual(len(terms), 1)
        self.assertEqual(terms[0].stem, "видеокарт")


class StemTextTests(unittest.TestCase):
    def test_stem_text_joins_stems(self) -> None:
        self.assertEqual(stem_text("у меня есть кот"), "мен ест кот")
        self.assertEqual(stem_text("Видеокартой RTX"), "видеокарт rtx")


if __name__ == "__main__":
    unittest.main()
