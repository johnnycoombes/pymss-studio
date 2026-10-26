from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

if __package__:
    from . import _bootstrap as _worker_test_bootstrap
else:
    import _bootstrap as _worker_test_bootstrap  # noqa: F401

import worker_infer
import worker_paths
import worker_workflows


class WorkerOutputPathTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.default = self.root / "data" / "outputs"
        env = mock.patch.dict(os.environ, {"PYMSS_STUDIO_DEFAULT_OUTPUT_DIR": str(self.default)})
        env.start()
        self.addCleanup(env.stop)

    def test_all_modes_resolve_relative_paths_under_the_application_data_root(self) -> None:
        for module in (worker_infer, worker_workflows):
            for value in ("delivery", "  delivery  ", "结果目录"):
                with self.subTest(module=module.__name__, value=value):
                    expected = (self.default.parent / value).resolve()
                    self.assertEqual(Path(module._normalize_output_dir(value)), expected)

    def test_absolute_paths_override_the_worker_default(self) -> None:
        target = self.root / "custom" / "结果目录"
        for module in (worker_infer, worker_workflows):
            self.assertEqual(Path(module._normalize_output_dir(str(target))), target.resolve())

    def test_missing_and_empty_paths_use_the_worker_default(self) -> None:
        for module in (worker_infer, worker_workflows):
            for value in (None, ""):
                with self.subTest(module=module.__name__, value=value):
                    self.assertEqual(Path(module._normalize_output_dir(value)), self.default.resolve())

    def test_standalone_workers_resolve_relative_paths_from_the_working_directory(self) -> None:
        with mock.patch.dict(os.environ, {"PYMSS_STUDIO_DEFAULT_OUTPUT_DIR": ""}):
            for module in (worker_infer, worker_workflows):
                self.assertEqual(Path(module._normalize_output_dir(None)), Path("results").resolve())
                self.assertEqual(Path(module._normalize_output_dir("delivery")), Path("delivery").resolve())

    def test_linked_default_outputs_keep_relative_paths_under_the_logical_data_root(self) -> None:
        self.default.parent.mkdir(parents=True)
        target = self.root / "external" / "outputs"
        target.mkdir(parents=True)
        if os.name == "nt":
            subprocess.run(
                [os.environ["COMSPEC"], "/c", "mklink", "/J", str(self.default), str(target)],
                check=True, capture_output=True,
            )
        else:
            self.default.symlink_to(target, target_is_directory=True)

        for module in (worker_infer, worker_workflows):
            with self.subTest(module=module.__name__):
                self.assertEqual(
                    Path(module._normalize_output_dir("delivery")),
                    (self.default.parent / "delivery").resolve(),
                )
                self.assertEqual(Path(module._normalize_output_dir(None)), target.resolve())

    def test_single_model_command_uses_the_same_relative_path_resolution(self) -> None:
        source = self.root / "song.wav"
        source.write_bytes(b"input")
        target = (self.default.parent / " delivery ").resolve()
        pymss = ModuleType("pymss")
        pymss.get_separation_logger = mock.Mock(return_value=logging.Logger("output-path-test"))
        separator = SimpleNamespace(
            process_folder=mock.Mock(return_value=[source.name]),
            studio_outputs=mock.Mock(return_value=[]), close=mock.Mock(),
        )
        payload = {
            "taskId": "output-path", "model": "model-a", "input": str(source),
            "output": " delivery ", "outputLayout": "flat",
        }
        with mock.patch.dict(sys.modules, {"pymss": pymss}), \
             mock.patch.object(worker_infer, "_resolve_separator_device"), \
             mock.patch.object(worker_infer, "_prepare_separator", return_value=separator) as prepare, \
             mock.patch.object(worker_infer, "emit"), \
             mock.patch.object(worker_infer, "emit_error"):
            self.assertEqual(worker_infer.cmd_infer(payload), 0)
        self.assertEqual(Path(prepare.call_args.kwargs["payload"]["output"]), target)
        self.assertTrue(target.is_dir())

    def test_workflow_single_and_batch_commands_pass_the_resolved_output_root(self) -> None:
        expected = (self.default.parent / "delivery").resolve()
        for batch in (False, True):
            with self.subTest(batch=batch):
                payload = {"taskId": "workflow-path", "output": "delivery", "workflow": {"nodes": []}}
                if batch:
                    payload["tasks"] = [{"taskId": "workflow-path-1"}, {"taskId": "workflow-path-2"}]
                with mock.patch.object(worker_workflows, "_run_pymss", return_value={}) as run, \
                     mock.patch.object(worker_workflows, "emit") as emit, \
                     mock.patch.object(worker_workflows, "emit_error"):
                    self.assertEqual(worker_workflows.cmd_infer_workflow(payload), 0)
                self.assertEqual(run.call_count, 2 if batch else 1)
                for call in run.call_args_list:
                    self.assertEqual(Path(call.kwargs["output_dir"]), expected)
                completed = [call for call in emit.call_args_list if call.args[0] == "task_done"]
                self.assertEqual(len(completed), 2 if batch else 1)
                for call in completed:
                    self.assertEqual(Path(call.args[1]["outputRoot"]), expected)

    def test_native_output_path_reaches_the_path_parser_without_trimming(self) -> None:
        native_path = str(self.root / "physical-root ")
        with mock.patch.object(worker_paths, "Path", wraps=Path) as parser:
            worker_paths.normalize_output_dir(native_path)
        parser.assert_called_once_with(native_path)

    def test_model_completion_records_the_root_before_resolving_a_linked_input_folder(self) -> None:
        self.default.mkdir(parents=True)
        target = self.root / "archive" / "renamed-song"
        target.mkdir(parents=True)
        alias = self.default / "song"
        if os.name == "nt":
            subprocess.run(
                [os.environ["COMSPEC"], "/c", "mklink", "/J", str(alias), str(target)],
                check=True, capture_output=True,
            )
        else:
            alias.symlink_to(target, target_is_directory=True)
        source = self.root / "song.wav"
        source.touch()
        pymss = ModuleType("pymss")
        pymss.get_separation_logger = mock.Mock(return_value=logging.Logger("output-path-test"))
        separator = SimpleNamespace(
            process_folder=mock.Mock(return_value=[source.name]),
            studio_outputs=mock.Mock(return_value=[]), close=mock.Mock(),
        )
        for batch in (False, True):
            with self.subTest(batch=batch):
                payload = {"taskId": "linked-folder", "model": "model-a", "output": str(self.default), "outputLayout": "folders"}
                if batch:
                    payload["tasks"] = [{"taskId": "linked-folder-1", "input": str(source)}]
                else:
                    payload["input"] = str(source)
                with mock.patch.dict(sys.modules, {"pymss": pymss}), \
                     mock.patch.object(worker_infer, "_resolve_separator_device"), \
                     mock.patch.object(worker_infer, "_prepare_separator", return_value=separator), \
                     mock.patch.object(worker_infer, "emit") as emit:
                    self.assertEqual(worker_infer.cmd_infer(payload), 0)
                completed = [call for call in emit.call_args_list if call.args[0] == "task_done"]
                self.assertEqual(len(completed), 1)
                result = completed[0].args[1]
                self.assertEqual(Path(result["outputDir"]), target.resolve())
                self.assertEqual(Path(result["outputRoot"]), self.default.resolve())


if __name__ == "__main__":
    unittest.main()
