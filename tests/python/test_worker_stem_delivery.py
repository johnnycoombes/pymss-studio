"""Separator deferral and workflow rewiring for OGG and 4-stem DJ output."""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

if __package__:
    from . import _bootstrap as _worker_test_bootstrap
else:
    import _bootstrap as _worker_test_bootstrap

import numpy as np

import worker_infer
from stem_delivery import StemDeliveryError, decode_peak, write_stem_file
from workflow_delivery import apply_simple_dj_folds, rewrite_yaml_ogg


class _DeferSeparatorBase:
    def __init__(self, instruments=None, output_format="wav", **kwargs):
        self.received_format = output_format
        self.received_kwargs = kwargs
        self.config = SimpleNamespace(training=SimpleNamespace(instruments=list(instruments or [])))
        self.logger = mock.Mock()
        self.output_format = output_format

    def _stems_to_save(self):
        return self.config.training.instruments

    def _stem_batches_to_save(self):
        return [self.config.training.instruments]

    def process_folder(self, input_folder):
        path = Path(input_folder)
        rate = 44100
        frames = rate
        time = np.arange(frames, dtype=np.float32) / float(rate)
        def channel(frequency, amplitude):
            wave = (amplitude * np.sin(2.0 * np.pi * frequency * time)).astype(np.float32)
            return np.stack([wave, wave], axis=1)

        parts = {
            "vocals": channel(440.0, 0.2),
            "other": channel(660.0, 0.05),
            "bass": channel(80.0, 0.3),
            "drums": channel(120.0, 1.7),
        }
        out = self._test_output
        for stem in self.config.training.instruments:
            self._save_output(stem, parts[stem], rate, path.stem, str(out))
        return [path.name]


class SeparatorDeliveryTests(unittest.TestCase):
    def _separator(self, **kwargs):
        with mock.patch.dict(sys.modules, {"pymss": SimpleNamespace(MSSeparator=_DeferSeparatorBase)}):
            separator_type = worker_infer._studio_separator_type()
        return separator_type(instruments=["vocals", "other", "bass", "drums"], **kwargs)

    def test_ogg_and_dj_keep_pymss_on_wav_then_write_named_ogg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mix_dir = root / "in"
            out = root / "out"
            mix_dir.mkdir()
            out.mkdir()
            rate = 44100
            frames = rate
            time = np.arange(frames, dtype=np.float32) / float(rate)
            vocals = 0.2 * np.sin(2.0 * np.pi * 440.0 * time)
            bass = 0.3 * np.sin(2.0 * np.pi * 80.0 * time)
            drums = 1.7 * np.sin(2.0 * np.pi * 120.0 * time)
            melody = 0.4 * np.sin(2.0 * np.pi * 660.0 * time)
            mix = np.stack([vocals + bass + drums + melody, vocals + bass + drums + melody], axis=0).astype(np.float32)
            source = mix_dir / "song.wav"
            write_stem_file(source, mix, rate, "wav")
            separator = self._separator(
                output_format="ogg",
                dj_output=True,
                audio_params={"ogg_quality": 6, "ogg_min_bitrate": 0},
                output_naming={"enabled": True, "template": "%index%_%filename%_%stem%"},
                output_model="bs-roformer",
            )
            separator._test_output = out
            self.assertEqual(separator.received_format, "wav")
            self.assertTrue(separator._studio_defer_delivery)
            separator.process_folder(str(source))
            names = sorted(path.name for path in out.iterdir())
            self.assertEqual(names, [
                "song - Bass.ogg",
                "song - Drums.ogg",
                "song - Melody.ogg",
                "song - Vocals.ogg",
            ])
            peaks = {}
            for path in out.iterdir():
                peak, sample_rate, _frames = decode_peak(path)
                peaks[path.name] = peak
                self.assertEqual(sample_rate, 44100)
                self.assertLess(peak, 1.0)
            print("SEPARATOR_OGG_EVIDENCE " + " ".join(f"{name}={peak:.4f}" for name, peak in sorted(peaks.items())))
            self.assertGreater(peaks["song - Drums.ogg"], peaks["song - Vocals.ogg"])


class WorkflowDeliveryTests(unittest.TestCase):
    def test_yaml_ogg_is_rewritten_to_wav_and_remembered(self) -> None:
        data = {
            "defaults": {"output_format": "ogg"},
            "steps": [
                {"id": "keep", "output_format": "flac"},
                {"id": "inherit"},
                {"id": "explicit", "output_format": "ogg"},
            ],
        }
        plan = rewrite_yaml_ogg(data, {"ogg_quality": 7, "ogg_min_bitrate": 0}, "wav")
        self.assertEqual(data["defaults"]["output_format"], "wav")
        self.assertEqual(data["steps"][0]["output_format"], "flac")
        self.assertNotIn("output_format", data["steps"][1])
        self.assertEqual(data["steps"][2]["output_format"], "wav")
        self.assertEqual(plan.step_formats, {"inherit": "ogg", "explicit": "ogg"})
        self.assertTrue(plan.run_wants_ogg)
        self.assertEqual(plan.node_quality["step:inherit"], 7)
        self.assertNotIn("keep", plan.step_formats)

    def test_simple_dj_fold_rewires_saves_and_rejects_incomplete_models(self) -> None:
        graph = self._graph_module()
        with mock.patch.dict(sys.modules, {"pymss": graph.pymss, "pymss.graph": graph}):
            dag = self._dag(graph)
            definition = {
                "steps": [{
                    "id": "sep1",
                    "dj_output": True,
                    "save": {"vocals": "Default", "other": "Default"},
                    "output_names": {
                        "vocals": "%filename%_%stem%",
                        "other": "Club %filename% %stem%",
                    },
                }],
            }
            overrides = apply_simple_dj_folds(dag, definition)
            ids = {node.id for node in dag.nodes}
            self.assertIn("studio:dj:sep1", ids)
            self.assertTrue({"save:sep1:Vocals", "save:sep1:Melody", "save:sep1:Bass", "save:sep1:Drums"} <= ids)
            self.assertNotIn("save:sep1:other", ids)
            self.assertEqual(overrides["sep1.other"], ("studio:dj:sep1", 1))
            self.assertEqual(overrides["sep1.vocals"], ("studio:dj:sep1", 0))
            self.assertEqual(definition["steps"][0]["output_names"]["Vocals"], "%filename% - %stem%")
            self.assertEqual(definition["steps"][0]["output_names"]["Melody"], "Club %filename% %stem%")
            broken = {
                "steps": [{
                    "id": "sep1",
                    "dj_output": True,
                    "save": {},
                }],
            }
            with self.assertRaises(StemDeliveryError):
                apply_simple_dj_folds(self._dag(graph, stems=["vocals", "other"]), broken)

    def _graph_module(self):
        graph = types.ModuleType("pymss.graph")

        class DAGLink:
            def __init__(self, link_id, source_node_id, source_slot, target_node_id, target_slot, type):
                self.link_id = link_id
                self.source_node_id = source_node_id
                self.source_slot = source_slot
                self.target_node_id = target_node_id
                self.target_slot = target_slot
                self.type = type

        class DAGNode:
            def __init__(self, id, type, inputs, data, title, signature=None):
                self.id = id
                self.type = type
                self.inputs = inputs
                self.data = data
                self.title = title
                self.signature = signature

        class PortSpec:
            def __init__(self, name, type):
                self.name = name
                self.type = type

        class NodeSignature:
            def __init__(self, inputs, output_names, output_types):
                self.inputs = inputs
                self.output_names = output_names
                self.output_types = output_types

        graph.DAGLink = DAGLink
        graph.DAGNode = DAGNode
        graph.PortSpec = PortSpec
        graph.NodeSignature = NodeSignature
        graph.AUDIO = "AUDIO"
        pymss = types.ModuleType("pymss")
        pymss.graph = graph
        graph.pymss = pymss
        return graph

    def _dag(self, graph, stems=None):
        stems = stems or ["vocals", "other", "bass", "drums"]

        class Dag:
            def __init__(self):
                self.nodes = []

        dag = Dag()
        sep = graph.DAGNode(
            id="step:sep1",
            type="mss_separate",
            inputs=[graph.DAGLink(1, "load", 0, "step:sep1", 0, graph.AUDIO)],
            data={"outputs": [{"name": f"{stem} (Audio)", "type": "AUDIO"} for stem in stems]},
            title="step:sep1",
        )
        dag.nodes.append(sep)
        for index, stem in enumerate(["vocals", "other"] if "other" in stems else stems[:1]):
            save = graph.DAGNode(
                id=f"save:sep1:{stem}",
                type="pymss_save_audio",
                inputs=[graph.DAGLink(10 + index, "step:sep1", index * 2, f"save:sep1:{stem}", 0, graph.AUDIO)],
                data={"widgets_values": ["wav", "Default", "44100", "FLOAT", "PCM_24", "320k"]},
                title=f"save:sep1:{stem}",
            )
            dag.nodes.append(save)
        return dag


if __name__ == "__main__":
    unittest.main()
