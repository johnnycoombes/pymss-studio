from __future__ import annotations

import io
import logging
import sys
import threading
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from . import _bootstrap as _worker_test_bootstrap  # noqa: F401
import worker_infer
from worker_workflow_logs import workflow_logging


class WorkflowNodeLogTests(unittest.TestCase):
    def setUp(self):
        self.logger = logging.Logger("workflow-log-test", level=logging.INFO)
        self.console = logging.StreamHandler(io.StringIO())
        self.console.setLevel(logging.INFO)
        self.logger.console_handler = self.console
        self.logger.addHandler(self.console)
        self.pymss = types.ModuleType("pymss")
        self.pymss.get_separation_logger = lambda: self.logger

    def test_node_logs_use_explicit_execution_scope_and_do_not_leak_between_nodes(self):
        with patch.dict(sys.modules, {"pymss": self.pymss}), patch.object(worker_infer, "emit") as emit:
            with workflow_logging("file-one") as scope:
                scope.track({"node_id": "step:one", "node_fraction": 0})
                self.logger.info("Loading model")
                scope.track({"node_id": "step:one", "node_fraction": 1, "audio_index": 1, "audio_count": 1})
                self.logger.info("Collecting model outputs")
                scope.track({"node_id": "step:one", "node_fraction": 1})
                self.logger.info("Between nodes")
                scope.track({"node_id": "step:two", "node_fraction": 0})
                self.logger.info("Second model")
        payloads = [call.args[1] for call in emit.call_args_list]
        self.assertEqual([item.get("nodeId") for item in payloads], ["step:one", "step:one", None, "step:two"])
        self.assertTrue(all(call.kwargs["task_id"] == "file-one" for call in emit.call_args_list))
        self.assertEqual(self.console.stream.getvalue(), "")
        self.assertEqual(self.logger.handlers, [self.console])
        self.assertEqual(self.console.filters, [])

    def test_native_lifecycle_does_not_end_the_node_on_full_audio_progress(self):
        with patch.dict(sys.modules, {"pymss": self.pymss}), patch.object(worker_infer, "emit") as emit:
            with workflow_logging("advanced") as scope:
                scope.track({"event": "node_started", "node_id": "split", "node_fraction": 0})
                scope.track({"event": "node_progress", "node_id": "split", "node_fraction": 1})
                self.logger.info("Finishing current node")
                scope.track({"event": "node_completed", "node_id": "split", "node_fraction": 1})
                self.logger.info("Between nodes")
                scope.track({"event": "node_failed", "node_id": "save", "node_fraction": 0})
                self.logger.error("Save failed")
                scope.track({"event": "execution_failed", "node_id": "save"})
                self.logger.info("Releasing model")
        self.assertEqual([call.args[1].get("nodeId") for call in emit.call_args_list], ["split", None, "save", None])

    def test_background_thread_logs_do_not_inherit_a_foreground_node(self):
        with patch.dict(sys.modules, {"pymss": self.pymss}), patch.object(worker_infer, "emit") as emit:
            with workflow_logging("file-one") as scope:
                scope.track({"node_id": "one", "node_fraction": 0})
                thread = threading.Thread(target=lambda: self.logger.warning("Background log"))
                thread.start()
                thread.join()
        self.assertNotIn("nodeId", emit.call_args.args[1])
        self.assertEqual(emit.call_args.args[1]["taskId"], "file-one")
        self.assertNotIn("taskScope", emit.call_args.args[1]["context"])

    def test_batch_background_logs_keep_operation_scope_without_a_file_guess(self):
        with patch.dict(sys.modules, {"pymss": self.pymss}), patch.object(worker_infer, "emit") as emit:
            with workflow_logging("file-one", batch=True) as scope:
                scope.track({"node_id": "one", "node_fraction": 0})
                thread = threading.Thread(target=lambda: self.logger.warning("Background log"))
                thread.start()
                thread.join()
        payload = emit.call_args.args[1]
        self.assertNotIn("nodeId", payload)
        self.assertNotIn("taskId", payload)
        self.assertEqual(payload["context"]["taskScope"], "operation")
        self.assertIsNone(emit.call_args.kwargs["task_id"])

    def test_failure_restores_handlers_and_next_file_starts_without_a_node(self):
        with patch.dict(sys.modules, {"pymss": self.pymss}), patch.object(worker_infer, "emit") as emit:
            with self.assertRaises(ValueError):
                with workflow_logging("file-one") as scope:
                    scope.track({"node_id": "one", "node_fraction": 0})
                    raise ValueError("Invalid model")
            self.assertEqual(self.logger.handlers, [self.console])
            self.assertEqual(self.console.filters, [])
            with workflow_logging("file-two"):
                self.logger.info("Preparing file")
        self.assertNotIn("nodeId", emit.call_args.args[1])
        self.assertEqual(emit.call_args.kwargs["task_id"], "file-two")

    def test_node_log_level_tracks_runtime_changes_to_the_sdk_console_level(self):
        self.logger.setLevel(logging.DEBUG)
        with patch.dict(sys.modules, {"pymss": self.pymss}), patch.object(worker_infer, "emit") as emit:
            with workflow_logging("workflow") as scope:
                scope.track({"node_id": "3", "node_fraction": 0})
                self.logger.debug("Debug disabled")
                self.console.setLevel(logging.DEBUG)
                self.logger.debug("Loading model")
                self.console.setLevel(logging.WARNING)
                self.logger.info("Info disabled")
                self.logger.warning("Model warning")
                self.console.setLevel(logging.INFO)
                self.logger.debug("Debug disabled again")
                self.logger.info("Processing audio")
        payloads = [call.args[1] for call in emit.call_args_list]
        self.assertEqual([item["level"] for item in payloads], ["debug", "warn", "info"])
        self.assertEqual([item["message"] for item in payloads], ["Loading model", "Model warning", "Processing audio"])
        self.assertTrue(all(item["nodeId"] == "3" for item in payloads))
        self.assertEqual(self.console.stream.getvalue(), "")
        self.assertEqual(self.logger.handlers, [self.console])
        self.assertEqual(self.console.filters, [])

    def test_non_workflow_handlers_keep_their_existing_level_behavior(self):
        self.logger.setLevel(logging.DEBUG)
        with patch.object(worker_infer, "emit") as emit:
            handler = worker_infer.JsonLogHandler("model")
            self.logger.addHandler(handler)
            self.logger.debug("Debug disabled")
            self.logger.info("Model loaded")
            handler.setLevel(logging.DEBUG)
            self.logger.debug("Debug enabled")
            self.logger.removeHandler(handler)
        self.assertEqual([call.args[1]["level"] for call in emit.call_args_list], ["info", "debug"])

    def test_original_record_metadata_and_multiline_exception_survive_the_bridge(self):
        record = logging.LogRecord("logger", logging.ERROR, "separator.py", 1741, "Cannot load %s", ("model",), None, func="load_model")
        record.created = 1_700_000_000.25
        try:
            raise ValueError("Invalid model")
        except ValueError:
            record.exc_info = sys.exc_info()
        with patch.dict(sys.modules, {"pymss": self.pymss}), patch.object(worker_infer, "emit") as emit:
            with workflow_logging("file") as scope:
                scope.track({"event": "node_started", "node_id": "split"})
                self.logger.handle(record)
        payload = emit.call_args.args[1]
        self.assertEqual(payload["schemaVersion"], 1)
        self.assertTrue(payload["id"])
        self.assertEqual(payload["source"], "pymss")
        self.assertEqual(payload["level"], "error")
        self.assertEqual(payload["message"], "Cannot load model")
        self.assertEqual(payload["timestamp"], datetime.fromtimestamp(record.created, timezone.utc).isoformat().replace("+00:00", "Z"))
        self.assertEqual(payload["location"], {"file": "separator.py", "line": 1741, "function": "load_model"})
        self.assertEqual(payload["logger"], "logger")
        self.assertEqual(payload["nodeId"], "split")
        self.assertEqual(payload["taskId"], "file")
        self.assertIn("Traceback", payload["detail"])
        self.assertIn("\nValueError: Invalid model", payload["detail"])
        self.assertEqual(self.console.stream.getvalue(), "")

    def test_single_and_batch_scopes_use_the_same_bridge_and_restore_console_on_failure(self):
        active_task = "first"
        previous_propagate = self.logger.propagate
        with patch.dict(sys.modules, {"pymss": self.pymss}), patch.object(worker_infer, "emit") as emit:
            with self.assertRaises(ValueError):
                with worker_infer.separation_logging("batch", task_id_provider=lambda: active_task):
                    self.logger.info("First file")
                    active_task = "second"
                    self.logger.critical("Second file")
                    raise ValueError("stop")
            self.assertEqual([call.kwargs["task_id"] for call in emit.call_args_list], ["first", "second"])
            self.assertEqual([call.args[1]["level"] for call in emit.call_args_list], ["info", "fatal"])
            self.logger.info("After bridge")
        self.assertEqual(self.console.stream.getvalue(), "After bridge\n")
        self.assertEqual(self.logger.handlers, [self.console])
        self.assertEqual(self.logger.filters, [])
        self.assertEqual(self.logger.propagate, previous_propagate)
