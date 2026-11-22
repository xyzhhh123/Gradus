"""`--mode exec` 的离线测试：不碰真 Docker，只看参数与文件行为。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bench import exec_mode
from bench.common import Task
from bench.trace_writer import TraceWriter


class TraceWriterTest(unittest.TestCase):
    def test_records_are_flushed_line_by_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            with TraceWriter(path) as trace:
                trace.record("turn_start", turn=0)
                trace.record("tool_call", name="bash", args="ls")
                trace.close(status="done")

            lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual([line["type"] for line in lines], ["turn_start", "tool_call", "end"])
        self.assertEqual(lines[0]["turn"], 0)
        self.assertEqual(lines[-1]["status"], "done")


class StartContainerArgsTest(unittest.TestCase):
    def test_argv_carries_mount_blocked_hosts_and_image(self):
        calls: list[tuple[str, ...]] = []

        def fake_docker(*args: str, timeout: float = 120.0):
            calls.append(args)
            return type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        task = Task(
            id="pallets__flask-5014",
            instruction="x",
            workspace="H:/bench/work/pallets__flask-5014",
            image="swebench/sweb.eval.x86_64.pallets_1776_flask-5014:latest",
        )

        with patch.object(exec_mode, "_docker", fake_docker):
            name = exec_mode.start_container(task, Path("H:/bench/work/pallets__flask-5014"))

        self.assertEqual(name, "gradus-exec-pallets-flask-5014")
        run_args = next(args for args in calls if args[:2] == ("run", "-d"))
        self.assertEqual(run_args[:2], ("run", "-d"))
        self.assertIn("--name", run_args)
        self.assertIn("H:\\bench\\work\\pallets__flask-5014:/testbed", run_args)
        self.assertIn("--add-host", run_args)
        self.assertIn(task.image, run_args)

    def test_missing_image_is_reported(self):
        task = Task(id="x__y-1", instruction="x", workspace="H:/bench/work/x__y-1")

        with self.assertRaises(RuntimeError):
            exec_mode.start_container(task, Path("H:/bench/work/x__y-1"))


class TestPatchSpecTest(unittest.TestCase):
    def test_reads_dataset_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset = Path(tmp)
            payload = {
                "test_patch": "diff --git a/tests/test_a.py b/tests/test_a.py\n",
                "FAIL_TO_PASS": ["tests/test_a.py::test_one", "tests/test_a.py::test_two"],
            }
            (dataset / "x__y-1.json").write_text(json.dumps(payload), encoding="utf-8")

            with patch.object(exec_mode, "DATASET_DIR", dataset):
                spec = exec_mode._test_patch(Task(id="x__y-1", instruction="x", workspace=""))

            self.assertIsNotNone(spec)
            patch_path, command, files = spec
            self.assertTrue(patch_path.exists())
            self.assertIn("-m pytest", command)
            self.assertIn("test_one", command)
            self.assertEqual(files, ["tests/test_a.py"])
            patch_path.unlink()

    def test_missing_metadata_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(exec_mode, "DATASET_DIR", Path(tmp)):
                self.assertIsNone(
                    exec_mode._test_patch(Task(id="nope-1", instruction="x", workspace=""))
                )

class MultiagentAccountingTest(unittest.TestCase):
    def test_result_carries_subagent_fields(self):
        from bench.common import TaskResult

        result = TaskResult(
            id="x",
            multiagent=True,
            subagents=2,
            sub_input=1000,
            sub_output=200,
        )
        payload = result.to_json()
        self.assertTrue(payload["multiagent"])
        self.assertEqual(payload["subagents"], 2)
        self.assertEqual(payload["sub_input"], 1000)

    def test_summary_sums_subagent_tokens(self):
        from bench.common import TaskResult, summarize

        summary = summarize(
            [
                TaskResult(id="a", sub_input=100, sub_output=10),
                TaskResult(id="b", sub_input=50, sub_output=5),
            ],
            total_tasks=2,
        )
        self.assertEqual(summary["sub_tokens"], 165)
        self.assertEqual(summary["subagents"], 0)
