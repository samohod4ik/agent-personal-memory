"""Exhaustive synthetic parity for exact-span deletion matching."""
import itertools
import random
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from difflib import SequenceMatcher
from agent_personal_memory.diff_exact import ExactSequenceMatcher


class TestM1DiffParity(unittest.TestCase):
    def test_repeated_short_strings_match_existing_opcodes(self):
        strings = ["".join(x) for n in range(1, 7) for x in itertools.product("ab", repeat=n)]
        for old in strings:
            for new in strings:
                expected = SequenceMatcher(None, old, new, autojunk=False).get_opcodes()
                actual = ExactSequenceMatcher(None, old, new, autojunk=False, threshold=0).get_opcodes()
                self.assertEqual(actual, expected, (old, new))

    def test_random_unicode_ranges_and_opcodes_match_existing_policy(self):
        rng = random.Random(719)
        for _ in range(3000):
            old = "".join(rng.choices("abж🙂", k=rng.randrange(1, 70)))
            new = "".join(rng.choices("abж🙂", k=rng.randrange(1, 70)))
            baseline = SequenceMatcher(None, old, new, autojunk=False)
            candidate = ExactSequenceMatcher(None, old, new, autojunk=False, threshold=0)
            alo = rng.randrange(len(old)); ahi = rng.randrange(alo, len(old) + 1)
            blo = rng.randrange(len(new)); bhi = rng.randrange(blo, len(new) + 1)
            self.assertEqual(candidate.find_longest_match(alo, ahi, blo, bhi), baseline.find_longest_match(alo, ahi, blo, bhi))
            self.assertEqual(candidate.get_opcodes(), baseline.get_opcodes())

    def test_insufficient_memory_aborts_before_exact_match(self):
        with patch("agent_personal_memory.diff_exact.available_memory", return_value=1):
            with self.assertRaises(MemoryError):
                ExactSequenceMatcher(None, "abab", "baba", autojunk=False, threshold=0).get_opcodes()

    @unittest.skipUnless(os.name == "posix", "Linux cgroup membership")
    def test_missing_cgroup_v2_membership_is_not_assumed_to_be_root(self):
        from agent_personal_memory.resources import available_memory
        def read(path, *args, **kwargs):
            if path == Path("/proc/meminfo"):
                return "MemAvailable: 1000000 kB\n"
            if path == Path("/proc/self/cgroup"):
                return "1:cpu:/unexpected\n"
            raise AssertionError(f"unexpected read: {path}")
        with patch.object(Path, "exists", return_value=True), patch.object(Path, "read_text", read):
            with self.assertRaises(ValueError):
                available_memory()
