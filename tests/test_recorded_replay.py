"""Every file in demo/recorded/ replays keylessly (0 live model calls) or is a known table."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ghostlab.capsule import _digest
from ghostlab.llm import adversary as ADV
from ghostlab.llm import agent as A
from ghostlab.llm import capsules as C

ROOT = Path(__file__).resolve().parents[1]
RECORDED = ROOT / "demo" / "recorded"
TABLES = {"calibration.json"}  # derived from capsules, not replayable themselves
NO_KEYS = {"META_API_KEY_DEV": "", "META_API_KEY_PUBLIC": "", "GLIMMER_BASE_URL": ""}


class RecordedReplayTests(unittest.TestCase):
    def test_every_recorded_file_replays(self):
        with mock.patch.dict(os.environ, NO_KEYS):
            for path in sorted(RECORDED.rglob("*.json")):
                name = str(path.relative_to(RECORDED))
                with self.subTest(name):
                    capsule = json.loads(path.read_text(encoding="utf-8"))
                    if name in TABLES:
                        continue
                    self.assertEqual(_digest(capsule["payload"]), capsule["sha256"])
                    kind = capsule["payload"]["kind"]
                    if kind == C.CAPSULE_KIND:
                        out = C.replay(path)
                    elif kind == ADV.EXPLORATION_KIND:
                        with tempfile.TemporaryDirectory() as tmp:
                            out = ADV.replay_exploration(capsule["payload"], tmp)
                    elif kind == C.VERIFY_KIND:
                        out = C.replay_verify(capsule["payload"])
                    else:
                        self.fail(f"unknown recorded kind {kind}")
                    self.assertTrue(out["reproduced"], name)
                    self.assertEqual(out["model_calls_live"], 0)

    def test_wonder_patch_is_verbatim_from_its_diagnosis(self):
        source = json.loads((ROOT / A.WONDER_SOURCE["capsule"]).read_text(encoding="utf-8"))
        self.assertEqual(source["sha256"], A.WONDER_SOURCE["sha256"])
        self.assertEqual(source["payload"]["diagnosis"]["model"], A.WONDER_SOURCE["model"])
        self.assertEqual(source["payload"]["diagnosis"]["proposed_patch"], A.WONDER_PATCH)
        self.assertIn(A.WONDER_PATCH, A.prompt_for("llm-naive-wonder"))
        self.assertEqual(A.tools_for("llm-naive-wonder"), A.tools_for("llm-naive"))
        self.assertTrue(A.prompt_for("llm-naive-wonder").startswith(A.prompt_for("llm-naive")))


if __name__ == "__main__":
    unittest.main()
