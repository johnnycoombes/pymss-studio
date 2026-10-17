"""Float-domain DJ residual, shared gain, and Vorbis encode checks."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

if __package__:
    from . import _bootstrap as _worker_test_bootstrap
else:
    import _bootstrap as _worker_test_bootstrap

import numpy as np

from stem_delivery import (
    DJ_OGG_FILE_STEMS,
    DJ_STEM_NAMES,
    OGG_SAMPLE_RATE,
    StemDeliveryError,
    conform_44k_stereo,
    decode_peak,
    dj_ogg_file_stem,
    encode_stem_group,
    ffprobe_bitrate_kbps,
    fold_dj_stems,
    linear_resample,
    missing_dj_roles,
    naming_template_for_dj,
    open_xdj_stage,
    peak_ceiling,
    publish_xdj_stage,
    quality_to_compression_level,
    resolve_ogg_quality,
    shared_gain,
    xdj_track_folder_name,
)


def _tone(channels: int, frames: int, frequency: float, amplitude: float, rate: int) -> np.ndarray:
    time = np.arange(frames, dtype=np.float32) / float(rate)
    wave = (amplitude * np.sin(2.0 * np.pi * frequency * time)).astype(np.float32)
    return np.repeat(wave.reshape(1, -1), channels, axis=0)


def _probe_stream(path: Path) -> dict:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    payload = json.loads(completed.stdout or "{}")
    streams = payload.get("streams") or [{}]
    return streams[0]


class StemDeliveryTests(unittest.TestCase):
    def test_quality_mapping_and_preset(self) -> None:
        self.assertAlmostEqual(quality_to_compression_level(6), 0.4, places=6)
        self.assertAlmostEqual(quality_to_compression_level(8), 0.2, places=6)
        self.assertEqual(resolve_ogg_quality({"ogg_quality": 5, "ogg_min_bitrate": 1}), 8)
        self.assertEqual(resolve_ogg_quality({"ogg_quality": 7}, "q5"), 5)
        self.assertEqual(resolve_ogg_quality({}, "min"), 8)

    def test_shared_gain_uses_one_multiplier(self) -> None:
        loud = np.array([[2.0, -1.5]], dtype=np.float32)
        quiet = np.array([[0.25, -0.1]], dtype=np.float32)
        gain = shared_gain([loud, quiet])
        self.assertIsNotNone(gain)
        assert gain is not None
        self.assertAlmostEqual(gain, peak_ceiling() / 2.0, places=6)
        scaled_loud = loud * gain
        scaled_quiet = quiet * gain
        self.assertLessEqual(float(np.max(np.abs(scaled_loud))), peak_ceiling() + 1e-6)
        self.assertAlmostEqual(float(scaled_quiet[0, 0] / scaled_loud[0, 0]), 0.25 / 2.0, places=5)
        self.assertIsNone(shared_gain([np.array([[0.2]], dtype=np.float32)]))

    def test_resample_preserves_a_sum(self) -> None:
        rate = 48000
        frames = 4800
        left = _tone(2, frames, 220.0, 0.4, rate)
        right = _tone(2, frames, 330.0, 0.25, rate)
        together = linear_resample(left + right, rate, OGG_SAMPLE_RATE)
        apart = linear_resample(left, rate, OGG_SAMPLE_RATE) + linear_resample(right, rate, OGG_SAMPLE_RATE)
        np.testing.assert_allclose(together, apart, atol=1e-5)

    def test_conform_makes_44100_stereo(self) -> None:
        mono = _tone(1, 4800, 440.0, 0.5, 48000)
        audio = conform_44k_stereo(mono, 48000, layout="channels")
        self.assertEqual(audio.shape[0], 2)
        self.assertEqual(audio.shape[1], 4410)
        np.testing.assert_allclose(audio[0], audio[1], atol=1e-6)

    def test_four_stem_residual_replaces_other_and_sums_to_the_mix(self) -> None:
        rate = 44100
        frames = 2048
        vocals = _tone(2, frames, 440.0, 0.3, rate)
        bass = _tone(2, frames, 80.0, 0.4, rate)
        drums = _tone(2, frames, 120.0, 0.5, rate)
        guitar = _tone(2, frames, 660.0, 0.2, rate)
        mix = vocals + bass + drums + guitar
        folded = fold_dj_stems(
            mix,
            {"vocals": vocals, "other": guitar * 0.0, "bass": bass, "drums": drums, "guitar": guitar},
        )
        self.assertEqual(tuple(folded), DJ_STEM_NAMES)
        np.testing.assert_allclose(folded["Vocals"], vocals, atol=1e-6)
        np.testing.assert_allclose(folded["Melody"], guitar, atol=1e-6)
        total = folded["Vocals"] + folded["Melody"] + folded["Bass"] + folded["Drums"]
        np.testing.assert_allclose(total, mix, atol=1e-5)

    def test_six_stem_model_folds_guitar_piano_and_other_into_melody(self) -> None:
        rate = 44100
        frames = 1024
        vocals = _tone(1, frames, 200.0, 0.2, rate)
        bass = _tone(1, frames, 60.0, 0.3, rate)
        drums = _tone(1, frames, 90.0, 0.15, rate)
        guitar = _tone(1, frames, 500.0, 0.1, rate)
        piano = _tone(1, frames, 700.0, 0.12, rate)
        other = _tone(1, frames, 1000.0, 0.05, rate)
        mix = vocals + bass + drums + guitar + piano + other
        folded = fold_dj_stems(mix, {
            "Vocals": vocals,
            "Guitar": guitar,
            "Piano": piano,
            "Bass": bass,
            "Drums": drums,
            "Other": other,
        })
        np.testing.assert_allclose(folded["Melody"], guitar + piano + other, atol=1e-5)
        total = sum(folded.values())
        np.testing.assert_allclose(total, mix, atol=1e-5)

    def test_missing_required_role_is_rejected(self) -> None:
        self.assertEqual(missing_dj_roles(["vocals", "other"]), ["bass", "drums"])
        mix = np.zeros((2, 8), dtype=np.float32)
        with self.assertRaises(StemDeliveryError):
            fold_dj_stems(mix, {"vocals": mix, "bass": mix})

    def test_stock_names_become_the_dj_pattern(self) -> None:
        self.assertEqual(naming_template_for_dj("%index%_%filename%_%stem%", True), ("%filename% - %stem%", True))
        self.assertEqual(naming_template_for_dj("Live %stem%", True), ("Live %stem%", True))

    def test_dj_ogg_names_match_xdj_az_tokens(self) -> None:
        self.assertEqual(
            [dj_ogg_file_stem(name) for name in DJ_STEM_NAMES],
            ["vocals", "other", "bass", "drums"],
        )
        for display, file_stem in DJ_OGG_FILE_STEMS.items():
            self.assertNotIn("inst", file_stem)
            if display != "Melody":
                self.assertNotIn("other", file_stem)
            if display != "Vocals":
                self.assertNotIn("vocal", file_stem)
        self.assertEqual(DJ_OGG_FILE_STEMS["Melody"], "other")

    def test_xdj_folder_names_escape_windows_devices(self) -> None:
        self.assertEqual(xdj_track_folder_name("song.wav"), "song")
        self.assertEqual(xdj_track_folder_name("My Track.flac"), "My Track")
        self.assertEqual(xdj_track_folder_name("song."), "song._")
        self.assertEqual(xdj_track_folder_name("song "), "song _")
        self.assertEqual(xdj_track_folder_name("CON"), "CON_")
        self.assertEqual(xdj_track_folder_name("nul.wav"), "nul_")
        self.assertEqual(xdj_track_folder_name("COM1."), "COM1._")
        self.assertEqual(xdj_track_folder_name("folder/COM1.live.wav"), "COM1.live_")
        self.assertEqual(xdj_track_folder_name("AUX"), "AUX_")
        self.assertEqual(xdj_track_folder_name("PRN.wav"), "PRN_")
        self.assertEqual(xdj_track_folder_name("LPT1"), "LPT1_")

    def test_publish_replaces_the_folder_and_drops_azstem_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "STEMS" / "song"
            target.mkdir(parents=True)
            (target / "azstem.env").write_text("old", encoding="utf-8")
            (target / "vocals.ogg").write_bytes(b"old-vocals")
            stage, final = open_xdj_stage(root, "song.wav")
            self.assertEqual(final, target)
            self.assertTrue(stage.name.startswith(".song-"))
            self.assertTrue(stage.name.startswith("."))
            (stage / "vocals.ogg").write_bytes(b"new-vocals")
            (stage / "other.ogg").write_bytes(b"melody")
            (stage / "bass.ogg").write_bytes(b"bass")
            (stage / "drums.ogg").write_bytes(b"drums")
            published = publish_xdj_stage(stage, final)
            self.assertEqual(published, target)
            self.assertFalse(stage.exists())
            self.assertFalse((target / "azstem.env").exists())
            self.assertEqual((target / "vocals.ogg").read_bytes(), b"new-vocals")
            self.assertEqual((target / "other.ogg").read_bytes(), b"melody")
            self.assertFalse(list((root / "STEMS").glob(".*")))

    def test_ogg_group_is_44100_stereo_with_one_peak_ceiling(self) -> None:
        rate = 48000
        frames = rate * 2
        rng = np.random.default_rng(11)
        vocals = (rng.standard_normal((1, frames)) * 0.08).astype(np.float32)
        bass = (rng.standard_normal((1, frames)) * 0.12).astype(np.float32)
        drums = (rng.standard_normal((2, frames)) * 1.4).astype(np.float32)
        melody = (rng.standard_normal((2, frames)) * 0.35).astype(np.float32)
        mix = (
            np.repeat(vocals, 2, axis=0)
            + np.repeat(bass, 2, axis=0)
            + drums
            + melody
        )
        folded = fold_dj_stems(mix, {
            "vocals": vocals,
            "bass": bass,
            "drums": drums,
            "other": np.zeros_like(drums),
        }, stem_layout="channels", mix_layout="channels")
        np.testing.assert_allclose(sum(folded.values()), mix, atol=1e-4)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stems = [(name, folded[name], rate) for name in DJ_STEM_NAMES]
            paths = [root / f"track - {name}.ogg" for name in DJ_STEM_NAMES]
            encoder = encode_stem_group(stems, paths, output_format="ogg", audio_params={"ogg_quality": 8})
            print(f"OGG_EVIDENCE encoder={encoder}")
            peaks = []
            for path in paths:
                info = _probe_stream(path)
                peak, sample_rate, decoded_frames = decode_peak(path)
                bitrate = ffprobe_bitrate_kbps(path)
                peaks.append(peak)
                print(
                    f"OGG_EVIDENCE file={path.name} codec={info.get('codec_name')} "
                    f"rate={info.get('sample_rate')} channels={info.get('channels')} "
                    f"bitrate_kbps={bitrate} peak={peak:.4f} frames={decoded_frames}"
                )
                self.assertEqual(info.get("codec_name"), "vorbis")
                self.assertEqual(int(info.get("sample_rate")), OGG_SAMPLE_RATE)
                self.assertEqual(int(info.get("channels")), 2)
                self.assertEqual(sample_rate, OGG_SAMPLE_RATE)
                self.assertLess(peak, 1.0)
            self.assertGreater(max(peaks), 0.5)
            self.assertLessEqual(max(peaks), 0.98)


if __name__ == "__main__":
    unittest.main()
