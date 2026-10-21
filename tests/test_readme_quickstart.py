"""README quickstart contract test.

Extracts the fenced ``python`` block from ``README.md`` and executes it
verbatim against a temporary store, so the documented quickstart can never
drift from the shipped API. The RU mirror must carry a runnable block too.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import agent_personal_memory as apm

REPO_ROOT = Path(__file__).resolve().parent.parent
README_EN = REPO_ROOT / "README.md"
README_RU = REPO_ROOT / "README.ru.md"

_PYTHON_BLOCK = re.compile(r"```python\n(.*?)```", re.DOTALL)


def _python_blocks(path: Path) -> list[str]:
    return _PYTHON_BLOCK.findall(path.read_text(encoding="utf-8"))


def _run_block(source: str, label: str) -> dict[str, object]:
    namespace: dict[str, object] = {"__name__": "__main__"}
    exec(compile(source, f"{label} quickstart", "exec"), namespace)
    return namespace


class ReadmeQuickstartTests(unittest.TestCase):
    def tearDown(self) -> None:
        apm.reset_registry()

    def test_readme_contains_a_python_quickstart_block(self) -> None:
        self.assertTrue(
            _python_blocks(README_EN),
            "README.md must contain a ```python quickstart block",
        )

    def test_en_quickstart_executes_against_a_temp_store(self) -> None:
        blocks = _python_blocks(README_EN)
        self.assertTrue(blocks, "README.md must contain a ```python block")
        namespace = _run_block(blocks[0], "README.md")
        # The snippet ends having physically erased the remembered claim.
        result = namespace["result"]
        self.assertIsInstance(result, dict)
        self.assertFalse(result["preview"])
        self.assertEqual(result["erased_claim_ids"], [namespace["claim_id"]])
        self.assertIn("receipt_id", result)

    def test_ru_mirror_quickstart_block_executes(self) -> None:
        blocks = _python_blocks(README_RU)
        self.assertTrue(blocks, "README.ru.md must contain a ```python block")
        namespace = _run_block(blocks[0], "README.ru.md")
        result = namespace["result"]
        self.assertIsInstance(result, dict)
        self.assertEqual(result["erased_claim_ids"], [namespace["claim_id"]])

    def test_reconcile_quickstart_block_executes(self) -> None:
        # The second fenced python block in each README is the
        # Kazan -> Perm reconcile/accept/history walkthrough; it must run
        # verbatim against a temp store in both language variants.
        for path, label in ((README_EN, "README.md"), (README_RU, "README.ru.md")):
            with self.subTest(readme=label):
                blocks = _python_blocks(path)
                self.assertGreaterEqual(
                    len(blocks), 2, f"{label} must contain a second ```python block"
                )
                namespace = _run_block(blocks[1], f"{label} reconcile")
                resolved = namespace["resolved"]
                self.assertEqual(resolved["status"], "accepted")
                self.assertEqual(resolved["new_state"], "confirmed")
                self.assertEqual(resolved["old_state"], "superseded")
                history = namespace["history"]
                self.assertEqual(
                    history["current_claim_id"],
                    str(namespace["second"]["claim_id"]),
                )


if __name__ == "__main__":
    unittest.main()
