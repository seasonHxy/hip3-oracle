import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from hip3_oracle.cli import main

ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        raw = json.loads((ROOT / "config/example.json").read_text())
        raw["stateFile"] = "runtime/state.json"
        self.path = Path(self.directory.name) / "config.json"
        self.path.write_text(json.dumps(raw))

    def invoke(self, *command):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--config", str(self.path), *command])
        return code, json.loads(output.getvalue())

    def test_status_and_preflight_work_without_creating_runtime_files(self):
        code, report = self.invoke("status")
        self.assertEqual(code, 0)
        self.assertFalse(report["exists"])
        code, report = self.invoke("preflight")
        self.assertEqual(code, 0)
        self.assertTrue(report["ok"])
        self.assertFalse((self.path.parent / "runtime").exists())

    def test_pause_block_resume_and_success_through_public_cli(self):
        code, paused = self.invoke("pause", "--reason", "provider investigation")
        self.assertEqual(code, 0)
        code, result = self.invoke("once")
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "paused")
        code, _ = self.invoke("resume", "--pause-id", paused["pause"]["id"], "--reason", "incident resolved")
        self.assertEqual(code, 0)
        code, result = self.invoke("once")
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "simulated")

    def test_synthetic_data_is_replayable_through_cli(self):
        code, result = self.invoke("replay", "--journal", str(ROOT / "examples/synthetic-replay.jsonl"))
        self.assertEqual(code, 0)
        self.assertTrue(result["matches"])
        self.assertEqual(result["cycles"], 6)

    def test_wrong_resume_id_returns_error_without_traceback(self):
        self.invoke("pause", "--reason", "provider investigation")
        errors = io.StringIO()
        with redirect_stderr(errors):
            code = main(["--config", str(self.path), "resume", "--pause-id", "wrong", "--reason", "incident resolved"])
        self.assertEqual(code, 2)
        self.assertIn("matching active pause ID", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())
