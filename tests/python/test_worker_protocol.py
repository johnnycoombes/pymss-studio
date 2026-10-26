from __future__ import annotations

import ast
import io
import json
import logging
import os
import tempfile
import threading
import time
import unittest
import warnings
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

if __package__:
    from . import _bootstrap as _worker_test_bootstrap
else:
    import _bootstrap as _worker_test_bootstrap

import worker_protocol


class WorkerProtocolTests(unittest.TestCase):
    def test_emit_keeps_json_on_protocol_stream_during_stdout_redirect(self) -> None:
        protocol_stdout = io.StringIO()
        incidental_stdout = io.StringIO()

        with (
            redirect_stdout(protocol_stdout),
            redirect_stderr(incidental_stdout),
        ):
            with worker_protocol.isolate_protocol_stdout():
                print("third-party diagnostic")
                worker_protocol.emit("audio_tool_progress", {"phase": "loading_model"})

        self.assertEqual(incidental_stdout.getvalue(), "third-party diagnostic\n")
        event = json.loads(protocol_stdout.getvalue())
        self.assertEqual(event["type"], "audio_tool_progress")
        self.assertEqual(event["payload"], {"phase": "loading_model"})

    def test_emit_retries_transient_windows_pipe_errors(self) -> None:
        class FlakyStream(io.StringIO):
            failures = 1

            def write(self, value):
                if self.failures:
                    self.failures -= 1
                    raise OSError(22, "Invalid argument")
                return super().write(value)

        stream = FlakyStream()
        with mock.patch.object(worker_protocol, "_PROTOCOL_STDOUT", stream), \
             mock.patch.object(worker_protocol.time, "sleep"):
            worker_protocol.emit("runtime_install_log", {"stage": "common"})

        event = json.loads(stream.getvalue())
        self.assertEqual(event["type"], "runtime_install_log")
        self.assertEqual(event["payload"]["stage"], "common")
        self.assertEqual(event["payload"]["context"]["stage"], "common")
        self.assertIsNone(event["payload"]["level"])

    def test_hosted_lifecycle_has_one_protocol_record_and_does_not_write_files_or_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory) / "session.log"
            persistent = Path(directory) / "persistent.log"
            output = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.dict(os.environ, {
                "PYMSS_STUDIO_LOG_TRANSPORT": "protocol",
                "PYMSS_STUDIO_DEBUG_LOG": "1",
                "PYMSS_STUDIO_TERMINAL_LOG": "1",
                "PYMSS_STUDIO_SESSION_LOG": str(session),
                "PYMSS_STUDIO_PERSISTENT_LOG": str(persistent),
            }), redirect_stdout(output), redirect_stderr(stderr):
                worker_protocol.debug_log("worker.start", command="infer", token="private-value")
            event = json.loads(output.getvalue())
            self.assertEqual(event["type"], "worker_log")
            self.assertEqual(event["payload"]["source"], "worker")
            self.assertEqual(event["payload"]["context"]["token"], "<redacted>")
            self.assertEqual(stderr.getvalue(), "")
            self.assertFalse(session.exists())
            self.assertFalse(persistent.exists())

    def test_legacy_explicit_debug_environment_keeps_file_and_terminal_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory) / "session.log"
            output = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.dict(os.environ, {
                "PYMSS_STUDIO_LOG_TRANSPORT": "",
                "PYMSS_STUDIO_DEBUG_LOG": "1",
                "PYMSS_STUDIO_TERMINAL_LOG": "1",
                "PYMSS_STUDIO_SESSION_LOG": str(session),
                "PYMSS_STUDIO_PERSISTENT_LOG": "",
            }), redirect_stdout(output), redirect_stderr(stderr):
                worker_protocol.debug_log("worker.start", command="infer", token="private-value")
            self.assertIn("PY worker.start command=infer", session.read_text(encoding="utf-8"))
            self.assertNotIn("private-value", session.read_text(encoding="utf-8"))
            self.assertTrue(stderr.getvalue().startswith(worker_protocol.TERMINAL_LOG_PREFIX))
            self.assertEqual(output.getvalue(), "")

    def test_parallel_emit_writes_complete_json_lines(self) -> None:
        class FragmentedStream(io.StringIO):
            def write(self, value):
                # Exercise the lock with a stream which releases execution mid-line.
                halfway = len(value) // 2
                super().write(value[:halfway])
                time.sleep(0.001)
                super().write(value[halfway:])
                return len(value)

        stream = FragmentedStream()
        with mock.patch.object(worker_protocol, "_PROTOCOL_STDOUT", stream):
            threads = [threading.Thread(target=worker_protocol.emit, args=("progress", {"index": index})) for index in range(16)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
        events = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual(sorted(item["payload"]["index"] for item in events), list(range(16)))

    def test_hosted_root_logging_and_python_warnings_are_structured_once_and_restored(self) -> None:
        root = logging.getLogger()
        previous_level = root.level
        previous_handlers = root.handlers[:]
        previous_showwarning = warnings.showwarning
        console = logging.StreamHandler(io.StringIO())
        root.addHandler(console)
        self.addCleanup(root.removeHandler, console)
        output = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"PYMSS_STUDIO_LOG_TRANSPORT": "protocol"}), redirect_stdout(output), redirect_stderr(stderr):
            try:
                with worker_protocol.protocol_logging("infer", task_id="file-one"):
                    logging.getLogger("library.model").error("Load failed")
                    with warnings.catch_warnings():
                        warnings.simplefilter("always")
                        warnings.warn("Experimental dtype", UserWarning)
                    print("native print")
                    raise ValueError("stop")
            except ValueError:
                pass
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([item["payload"]["message"] for item in events], ["Load failed", "Experimental dtype"])
        self.assertEqual([item["payload"]["level"] for item in events], ["error", "warn"])
        self.assertTrue(all(item["payload"]["source"] == "third_party" for item in events))
        self.assertTrue(all(item["payload"]["context"]["command"] == "infer" for item in events))
        self.assertEqual(console.stream.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "native print\n")
        self.assertEqual(root.level, previous_level)
        self.assertEqual(root.handlers, [*previous_handlers, console])
        self.assertIs(warnings.showwarning, previous_showwarning)
        self.assertEqual(console.filters, [])
        root.removeHandler(console)

    def test_hosted_debug_logging_follows_startup_flag_and_restores_root_level(self) -> None:
        root = logging.getLogger()
        previous_level = root.level
        self.addCleanup(root.setLevel, previous_level)
        for enabled in ("0", "1"):
            with self.subTest(enabled=enabled):
                output = io.StringIO()
                root.setLevel(logging.WARNING)
                with mock.patch.dict(os.environ, {
                    "PYMSS_STUDIO_LOG_TRANSPORT": "protocol",
                    "PYMSS_STUDIO_DEBUG_LOG": enabled,
                }), redirect_stdout(output), redirect_stderr(io.StringIO()):
                    with worker_protocol.protocol_logging("env_info"):
                        root.debug("Environment probe started")
                        root.info("Environment probe completed")
                records = [json.loads(line)["payload"] for line in output.getvalue().splitlines()]
                expected_levels = ["debug", "info"] if enabled == "1" else ["info"]
                self.assertEqual([record["level"] for record in records], expected_levels)
                self.assertTrue(all(record["context"]["command"] == "env_info" for record in records))
                self.assertEqual(root.level, logging.WARNING)

    def test_protocol_logging_suppresses_noisy_compiler_debug_and_restores_levels(self) -> None:
        numba = logging.getLogger("numba.core.ssa")
        llvmlite = logging.getLogger("llvmlite.binding")
        numba_parent = logging.getLogger("numba")
        llvmlite_parent = logging.getLogger("llvmlite")
        previous_levels = {
            numba_parent: numba_parent.level,
            llvmlite_parent: llvmlite_parent.level,
            numba: numba.level,
            llvmlite: llvmlite.level,
        }
        self.addCleanup(lambda: [logger.setLevel(level) for logger, level in previous_levels.items()])
        numba_parent.setLevel(logging.DEBUG)
        llvmlite_parent.setLevel(logging.INFO)
        numba.setLevel(logging.NOTSET)
        llvmlite.setLevel(logging.NOTSET)
        levels_at_context_entry = {
            numba_parent: numba_parent.level,
            llvmlite_parent: llvmlite_parent.level,
        }
        output = io.StringIO()

        with mock.patch.dict(os.environ, {
            "PYMSS_STUDIO_LOG_TRANSPORT": "protocol",
            "PYMSS_STUDIO_DEBUG_LOG": "1",
        }), redirect_stdout(output), redirect_stderr(io.StringIO()):
            with worker_protocol.protocol_logging("export_editor_mix", task_id="export-one"):
                self.assertEqual(numba_parent.level, logging.WARNING)
                self.assertEqual(llvmlite_parent.level, logging.WARNING)
                numba.debug("compiler detail")
                llvmlite.info("compiler summary")
                numba.warning("compiler warning")
                # The handler remains a boundary if a dependency changes itself
                # back to DEBUG during the active protocol context.
                numba.setLevel(logging.DEBUG)
                numba.debug("late compiler detail")
                logging.getLogger("worker.export").debug("studio detail")

        records = [json.loads(line)["payload"] for line in output.getvalue().splitlines()]
        self.assertEqual(
            [(record["logger"], record["level"], record["message"]) for record in records],
            [
                ("numba.core.ssa", "warn", "compiler warning"),
                ("worker.export", "debug", "studio detail"),
            ],
        )
        self.assertEqual(numba_parent.level, levels_at_context_entry[numba_parent])
        self.assertEqual(llvmlite_parent.level, levels_at_context_entry[llvmlite_parent])

    def test_logging_transport_failures_and_recursive_records_do_not_escape(self) -> None:
        logger = logging.Logger("third-party-test")
        calls = []

        def failing_emit(*args, **kwargs):
            calls.append(args)
            logger.warning("Transport failed")
            raise BrokenPipeError("closed")

        handler = worker_protocol.ProtocolLogHandler(emitter=failing_emit)
        logger.addHandler(handler)
        logger.error("Original failure")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["message"], "Original failure")

    def test_file_log_scope_restores_its_parent_after_an_exception(self) -> None:
        output = io.StringIO()
        previous_task = worker_protocol.current_log_task_id()
        with mock.patch.dict(os.environ, {"PYMSS_STUDIO_LOG_TRANSPORT": "protocol"}), \
             redirect_stdout(output), redirect_stderr(io.StringIO()):
            with worker_protocol.protocol_logging("infer", task_id="first", batch=True):
                logging.warning("Before files")
                with worker_protocol.task_logging("second"):
                    logging.warning("Outer file")
                    with self.assertRaises(ValueError):
                        with worker_protocol.task_logging("third"):
                            logging.warning("Nested file")
                            raise ValueError("stop")
                    logging.warning("Outer file restored")
                logging.warning("After files")
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([event["taskId"] for event in events], [None, "second", "third", "second", None])
        for event in (events[0], events[-1]):
            self.assertEqual(event["payload"]["context"]["taskScope"], "operation")
        self.assertTrue(all("taskScope" not in event["payload"]["context"] for event in events[1:-1]))
        self.assertEqual(worker_protocol.current_log_task_id(), previous_task)

    def test_text_credentials_keep_quoted_boundaries_and_hide_the_entire_value(self) -> None:
        for secret in ("prefix private-suffix", 'prefix "quoted" private-suffix', "prefix \\ private-suffix"):
            with self.subTest(secret=secret):
                message = "Request rejected: " + json.dumps({"password": secret, "status": "failed"})
                record = worker_protocol.log_record(message)
                safe = json.loads(record["message"].removeprefix("Request rejected: "))
                self.assertEqual(safe, {"password": "<redacted>", "status": "failed"})
                self.assertNotIn("private-suffix", json.dumps(record))
        for message, expected in (
            ("password='prefix private-suffix' status=failed", "password='<redacted>' status=failed"),
            (r"password='prefix \'quoted\' private-suffix' status=failed", "password='<redacted>' status=failed"),
            ('password="prefix \\"quoted\\" private-suffix" status=failed', 'password="<redacted>" status=failed'),
        ):
            with self.subTest(message=message):
                record = worker_protocol.log_record(message)
                self.assertEqual(record["message"], expected)
                self.assertNotIn("private-suffix", json.dumps(record))

    def test_actual_handler_redacts_repr_and_nested_serialized_credentials(self) -> None:
        document = {
            "password": 'prefix "quoted" private-suffix-A',
            "token": "prefix 'quoted' \\ private-suffix-B",
            "status": "failed", "attempt": 3,
        }
        expected = {**document, "password": "<redacted>", "token": "<redacted>"}
        for encoders in ((repr,), (json.dumps,), (repr, repr), (json.dumps, repr, json.dumps), (repr, json.dumps, repr, json.dumps)):
            with self.subTest(encoders=[encoder.__name__ for encoder in encoders]):
                value = json.dumps(document)
                for encoder in encoders:
                    value = encoder(value)
                output = io.StringIO()
                with mock.patch.dict(os.environ, {"PYMSS_STUDIO_LOG_TRANSPORT": "protocol"}), \
                     redirect_stdout(output), redirect_stderr(io.StringIO()):
                    with worker_protocol.protocol_logging("infer", task_id="task-one"):
                        logger = logging.getLogger("library.http")
                        if encoders == (repr,):
                            logger.error("Request rejected: %r", json.dumps(document))
                        else:
                            logger.error("Request rejected: %s", value)
                events = [json.loads(line) for line in output.getvalue().splitlines()]
                self.assertEqual(len(events), 1)
                message = events[0]["payload"]["message"].removeprefix("Request rejected: ")
                self.assertNotIn("private-suffix", message)
                for encoder in reversed(encoders):
                    message = ast.literal_eval(message) if encoder is repr else json.loads(message)
                self.assertEqual(json.loads(message), expected)

    def test_serialized_redaction_has_a_depth_limit_and_preserves_plain_apostrophes(self) -> None:
        for encoder in (repr, json.dumps):
            message = json.dumps({"password": 'prefix "quoted" private-suffix', "status": "failed"})
            for nesting in range(worker_protocol.MAX_LOG_TEXT_NESTING + 3):
                with self.subTest(encoder=encoder.__name__, nesting=nesting):
                    output = io.StringIO()
                    with mock.patch.dict(os.environ, {"PYMSS_STUDIO_LOG_TRANSPORT": "protocol"}), \
                         redirect_stdout(output), redirect_stderr(io.StringIO()):
                        with worker_protocol.protocol_logging("infer", task_id="task-one"):
                            logging.getLogger("library.http").error("Request rejected: %s", message)
                    events = [json.loads(line) for line in output.getvalue().splitlines()]
                    self.assertEqual(len(events), 1)
                    clean = events[0]["payload"]["message"].removeprefix("Request rejected: ")
                    self.assertFalse("private-suffix" in clean)
                    if nesting <= worker_protocol.MAX_LOG_TEXT_NESTING:
                        for _ in range(nesting):
                            clean = ast.literal_eval(clean) if encoder is repr else json.loads(clean)
                        self.assertEqual(json.loads(clean), {"password": "<redacted>", "status": "failed"})
                message = encoder(message)
        self.assertEqual(
            worker_protocol.log_record("Can't process password='prefix private-suffix' status=failed")["message"],
            "Can't process password='<redacted>' status=failed",
        )
        self.assertEqual(
            worker_protocol.log_record("Can't parse 'public value' status=failed")["message"],
            "Can't parse 'public value' status=failed",
        )
        document = json.dumps({"password": 'prefix "quoted" private-suffix', "status": "failed"})
        clean = worker_protocol.log_record("payload" + repr(document))["message"].removeprefix("payload")
        self.assertEqual(json.loads(ast.literal_eval(clean)), {"password": "<redacted>", "status": "failed"})

    def test_sensitive_key_variants_keep_public_fields_after_serialized_redaction(self) -> None:
        keys = ("password", "passwd", "access_token", "refresh_token", "token", "secret", "credential", "authorization", "api_key", "api-key", "apikey", "proxy_pass", "proxy-pass", "proxy-authorization")
        document = {key: 'prefix "quoted" private-suffix' for key in keys}
        document["status"] = "failed"
        message = worker_protocol.log_record("Rejected: " + repr(json.dumps(document)))["message"]
        clean = json.loads(ast.literal_eval(message.removeprefix("Rejected: ")))
        self.assertEqual(clean, {**dict.fromkeys(keys, "<redacted>"), "status": "failed"})

    def test_actual_handler_redacts_bytes_repr_and_preserves_utf8_public_fields(self) -> None:
        document = {"password": 'prefix "quoted" private-suffix', "status": "failed", "label": "公开字段"}
        for valid_utf8, body in ((True, json.dumps(document, ensure_ascii=False).encode("utf-8")), (False, b'{"password":"prefix \\"quoted\\" private-suffix","label":"\xff"}')):
            with self.subTest(valid_utf8=valid_utf8):
                output = io.StringIO()
                with mock.patch.dict(os.environ, {"PYMSS_STUDIO_LOG_TRANSPORT": "protocol"}), \
                     redirect_stdout(output), redirect_stderr(io.StringIO()):
                    with worker_protocol.protocol_logging("infer", task_id="task-one"):
                        logging.getLogger("library.http").error("Request rejected: %r", body)
                events = [json.loads(line) for line in output.getvalue().splitlines()]
                self.assertEqual(len(events), 1)
                message = events[0]["payload"]["message"].removeprefix("Request rejected: ")
                self.assertNotIn("private-suffix", message)
                clean = ast.literal_eval(message)
                self.assertIsInstance(clean, bytes)
                if valid_utf8:
                    self.assertEqual(json.loads(clean.decode("utf-8")), {**document, "password": "<redacted>"})
                else:
                    self.assertEqual(clean, b"<redacted>")


if __name__ == "__main__":
    unittest.main()
