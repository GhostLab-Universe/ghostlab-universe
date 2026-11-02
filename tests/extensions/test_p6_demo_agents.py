"""P6 demo agents stay dependency-free: zero imports, dicts in/out."""

import ast
import sys
import unittest
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
sys.path.insert(0, str(EXAMPLES))

import p6_demo_agents  # noqa: E402


class DemoAgentsTest(unittest.TestCase):
    def test_no_imports_at_all(self):
        tree = ast.parse((EXAMPLES / "p6_demo_agents.py").read_text(encoding="utf-8"))
        imports = [node for node in ast.walk(tree)
                   if isinstance(node, (ast.Import, ast.ImportFrom))]
        self.assertEqual(imports, [])

    def test_no_ghostlab_references(self):
        text = (EXAMPLES / "p6_demo_agents.py").read_text(encoding="utf-8")
        self.assertNotIn("ghostlab", text)

    def test_blind_books_twice_without_reading(self):
        agent = p6_demo_agents.BlindBooker()
        agent.start({}, {})
        first = agent.propose({})
        self.assertEqual(first["tool"], "reserve")
        agent.notify_result(first, {"status": "ok", "result": {}})
        self.assertIsNone(agent.propose({}))

    def test_careful_abstains_on_stale(self):
        agent = p6_demo_agents.CarefulBooker()
        agent.start({}, {})
        self.assertEqual(agent.propose({})["tool"], "check_availability")
        agent.notify_result({"tool": "check_availability"},
                            {"status": "ok",
                             "result": {"available": 5, "stale": True}})
        self.assertIsNone(agent.propose({}))

    def test_careful_books_within_availability(self):
        agent = p6_demo_agents.CarefulBooker()
        agent.start({}, {})
        agent.propose({})
        agent.notify_result({"tool": "check_availability"},
                            {"status": "ok",
                             "result": {"available": 1, "stale": False}})
        booking = agent.propose({})
        self.assertEqual(booking["arguments"]["seats"], 1)


if __name__ == "__main__":
    unittest.main()
