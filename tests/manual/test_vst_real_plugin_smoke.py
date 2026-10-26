"""Portable checks for the manual compatibility harness; no plugins are loaded."""
import unittest

from vst_real_plugin_smoke import rms_difference, scan_target, select_plugin


class CompatibilityHarnessTests(unittest.TestCase):
    def test_au_registry_targets_are_not_converted_to_paths(self):
        for identity in ("au:aufx:dely:appl", "au3:aufx:dely:appl", "au:system"):
            self.assertEqual(scan_target(identity), identity)

    def test_shell_selection_keeps_unknown_category_and_binary_reference(self):
        children = [{"id": f"vst2:{index:08X}", "format": "vst2", "name": "Effect",
                     "path": "shell.dll", "isInstrument": None} for index in (1, 2)]
        with self.assertRaisesRegex(ValueError, "Select --plugin-id"):
            select_plugin(children, None)
        self.assertIs(select_plugin(children, "vst2:00000002"), children[1])
        with self.assertRaises(ValueError):
            select_plugin(children, "vst2:00000003")

    def test_known_instruments_and_unsupported_formats_are_rejected(self):
        plugin = {"id": "one", "path": "plugin", "format": "vst3", "isInstrument": True}
        with self.assertRaises(ValueError):
            select_plugin([plugin], "one")
        plugin.update(format="aax", isInstrument=False)
        with self.assertRaisesRegex(ValueError, "unsupported"):
            select_plugin([plugin], "one")

    def test_stereo_comparison_detects_right_channel_and_length_regressions(self):
        self.assertGreater(rms_difference([(0.0, 0.0)], [(0.0, 1.0)], same_length=True), 0)
        with self.assertRaisesRegex(AssertionError, "length"):
            rms_difference([(0.0, 0.0)], [(0.0, 0.0)] * 2, same_length=True)
        with self.assertRaisesRegex(AssertionError, "no frames"):
            rms_difference([], [], same_length=False)


if __name__ == "__main__":
    unittest.main()
