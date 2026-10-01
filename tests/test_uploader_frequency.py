"""Frequency analysis (#540): the two marks of a lossy encoder, measured on synthetic FLACs.

Ported from chodeus's fork (uploader/frequency.py).
"""

import math

import av
import av.audio.stream
import msgspec
import numpy as np
import pytest

from salmon.uploader import frequency as fq

RATE = 44100


def write_flac(path, samples, rate=RATE) -> None:
    """Write mono float samples in [-1, 1] as a 16-bit FLAC, as a rip or a store download is."""
    pcm = np.clip(np.asarray(samples) * 32767, -32768, 32767).astype(np.int16)
    with av.open(str(path), "w") as out:
        stream = out.add_stream("flac", rate=rate, layout="mono", format="s16")
        assert isinstance(stream, av.audio.stream.AudioStream)
        for start in range(0, len(pcm), 4096):
            frame = av.AudioFrame.from_ndarray(pcm[None, start : start + 4096], format="s16", layout="mono")
            frame.rate = rate
            out.mux(stream.encode(frame))
        out.mux(stream.encode(None))


def noise(seconds, level=0.2, tilt_db_per_octave=0.0, seed=0, rate=RATE):
    """White noise, optionally tilted downwards like a dark master."""
    rng = np.random.default_rng(seed)
    samples = rng.normal(0, level, int(rate * seconds))
    if not tilt_db_per_octave:
        return samples
    spectrum = np.fft.rfft(samples)
    freqs = np.fft.rfftfreq(len(samples), 1 / rate)
    gain = 10 ** (tilt_db_per_octave * np.log2(np.maximum(freqs, 100) / 100) / 20)
    return np.fft.irfft(spectrum * gain, len(samples))


def lowpass(samples, cutoff_hz, rate=RATE):
    spectrum = np.fft.rfft(samples)
    spectrum[np.fft.rfftfreq(len(samples), 1 / rate) > cutoff_hz] = 0
    return np.fft.irfft(spectrum, len(samples))


def _band(samples, lo, hi):
    spectrum = np.fft.rfft(samples)
    freqs = np.fft.rfftfreq(len(samples), 1 / RATE)
    spectrum[(freqs < lo) | (freqs > hi)] = 0
    return np.fft.irfft(spectrum, len(samples))


def gated(seconds=6, floor=None, seed=1):
    """Loud mids all the way through; highs that switch on and off every 90 ms.

    With floor=None the off state is digital silence, as a decoded lossy file
    has it. A floor level makes the off state quiet noise instead, which is
    what a clean production falls to.
    """
    mids = _band(noise(seconds, 0.3, seed=seed), 2000, 8000)
    highs = _band(noise(seconds, 0.05, seed=seed + 1), 16_000, 21_000)
    gate = (np.arange(RATE * seconds) // (RATE * 90 // 1000)) % 2 == 0
    if floor is not None:
        quiet = _band(noise(seconds, floor, seed=seed + 2), 16_000, 21_000)
        return mids + np.where(gate, highs, quiet)
    return mids + highs * gate


def _analyse(tmp_path, name, samples):
    write_flac(tmp_path / name, samples)
    return fq._analyse_one(str(tmp_path), name, str(tmp_path / "01 Spectrum.png"))


def mp3_decode(tmp_path, samples, bit_rate, name=None):
    """Encode samples as MP3 with LAME, then decode it into a FLAC, as a transcode is made: its file name."""
    lossy = tmp_path / f"t{bit_rate}.mp3"
    pcm = np.clip(np.asarray(samples) * 32767, -32768, 32767).astype(np.int16)
    with av.open(str(lossy), "w") as out:
        # Stereo: LAME picks its lowpass from the bitrate per channel, and a mono file gets twice the stereo one.
        stream = out.add_stream("libmp3lame", rate=RATE, layout="stereo")
        assert isinstance(stream, av.audio.stream.AudioStream)
        stream.bit_rate = bit_rate
        for start in range(0, len(pcm), 4096):
            chunk = pcm[start : start + 4096]
            frame = av.AudioFrame.from_ndarray(np.stack([chunk, chunk]), format="s16p", layout="stereo")
            frame.rate = RATE
            out.mux(stream.encode(frame))
        out.mux(stream.encode(None))
    decoded, _rate = fq.decode_mono(str(lossy))
    name = name or f"transcode{bit_rate}.flac"
    write_flac(tmp_path / name, decoded)
    return name


def _result(
    name,
    reach=21_800.0,
    cutoff=0.0,
    floor_hz=0.0,
    drop=0.0,
    slope=0.0,
    gating=0.0,
    abrupt=0.0,
    digital=False,
    rate=RATE,
    error=None,
):
    return fq.SpectrumResult(
        file=name,
        image="x.png",
        sample_rate=rate,
        windows=10,
        reach_hz=reach,
        cutoff_hz=cutoff,
        knee_hz=cutoff - 150 if cutoff else 0.0,
        floor_hz=floor_hz or (cutoff + 150 if cutoff else 0.0),
        drop_db=drop,
        slope_db_per_khz=slope,
        gating=gating,
        gated_band="19-20 kHz" if gating else "",
        gating_abrupt=abrupt,
        digital_floor=digital,
        error=error,
    )


# The wall: measured against the floor above it, not the track's peak


def test_a_wall_is_measured_where_the_energy_meets_the_floor(tmp_path):
    result = _analyse(tmp_path, "lowpassed.flac", lowpass(noise(3), 16_500))
    assert 16_300 < result.cutoff_hz < 16_900
    assert result.drop_db > 30
    assert result.slope_db_per_khz > 50
    assert fq.has_lossy_wall(result)


def test_a_dark_master_with_a_wall_still_measures_the_wall_not_the_slope(tmp_path):
    """A cutoff at "peak minus 60 dB" lands on the slope of a dark track, well below its wall."""
    result = _analyse(tmp_path, "dark.flac", lowpass(noise(3, tilt_db_per_octave=-6), 20_200))
    assert 19_950 < result.cutoff_hz < 20_500
    assert result.slope_db_per_khz > 50


def test_full_bandwidth_audio_has_no_wall_and_reaches_nyquist(tmp_path):
    result = _analyse(tmp_path, "full.flac", noise(3))
    assert result.cutoff_hz == 0.0
    assert result.reach_hz > 20_000
    assert fq.classify(result) == "clean"


def test_a_gentle_roll_off_is_not_a_wall(tmp_path):
    result = _analyse(tmp_path, "gentle.flac", noise(3, tilt_db_per_octave=-12))
    assert not fq.has_lossy_wall(result)
    assert fq.classify(result) == "clean"


def test_a_master_that_rolls_off_near_nyquist_is_not_flagged(tmp_path):
    """Sample-rate conversion cuts hard, but above where any MP3 or AAC encoder does."""
    result = _analyse(tmp_path, "src.flac", lowpass(noise(4, tilt_db_per_octave=-3), 21_300))
    assert not fq.has_lossy_wall(result)
    assert fq.classify(result) == "clean"
    level, notes = fq.assess([result])
    assert level == "ok", notes


def test_a_wall_above_the_encoder_range_is_read_as_sample_rate_conversion():
    result = _result("srx.flac", reach=21_000, cutoff=21_100, drop=35, slope=55)
    assert not fq.has_lossy_wall(result)
    assert fq.classify(result) == "clean"
    assert "sample-rate conversion" in fq.describe(result)


def test_the_encoder_whose_lowpass_floors_out_there_is_named():
    assert "128 kbps" in fq.encoder_hint(16_800)
    assert "320 kbps" in fq.encoder_hint(20_300)
    assert fq.encoder_hint(21_000) == ""
    assert fq.encoder_hint(fq._LOSSY_WALL_HZ[0]), "the accepted range must start on a named setting"


# The gate: highs that flip between content and the bit-depth floor


def test_gated_highs_over_digital_silence_are_the_mark_of_an_encoder(tmp_path):
    result = _analyse(tmp_path, "gated.flac", gated())
    assert result.digital_floor
    assert result.gating >= fq._GATED_STRONG
    assert result.gating_abrupt >= fq._GATED_STRONG_ABRUPT
    assert fq.classify(result) == "lossy"
    assert fq.assess([result])[0] == "suspect"


def test_the_same_gating_over_a_noise_floor_is_not(tmp_path):
    """A clean production can fall to its own floor; only digital silence counts."""
    result = _analyse(tmp_path, "natural.flac", gated(floor=0.002))
    assert not result.digital_floor
    assert fq.classify(result) == "clean"


def test_highs_that_stay_on_do_not_gate(tmp_path):
    mids = _band(noise(6, 0.3), 2000, 8000)
    highs = _band(noise(6, 0.05, seed=2), 16_000, 21_000)
    result = _analyse(tmp_path, "steady.flac", mids + highs)
    assert result.gating < fq._GATED_WITH_WALL
    assert fq.classify(result) == "clean"


# A real encoder: the known answer the analysis exists for


@pytest.mark.parametrize(("bit_rate", "hint"), [(128_000, "128 kbps"), (320_000, "320 kbps")])
def test_a_decoded_mp3_shows_the_wall_where_lame_puts_it_and_the_original_does_not(tmp_path, bit_rate, hint):
    original = noise(8, tilt_db_per_octave=-4)
    clean = _analyse(tmp_path, "original.flac", original)
    assert fq.classify(clean) == "clean"
    transcode = fq._analyse_one(str(tmp_path), mp3_decode(tmp_path, original, bit_rate), None)
    assert fq.has_lossy_wall(transcode), transcode
    assert hint in fq.describe(transcode)
    assert fq.classify(transcode) in ("look", "lossy")


def music_like(seconds=10, high_level=0.0005):
    """Loud mids under a slow envelope, and quiet highs whose level wanders, as in a mix."""
    rng = np.random.default_rng(3)
    envelope = np.repeat(rng.uniform(0.3, 1.0, seconds * 10), RATE // 10)[: RATE * seconds]
    mids = _band(noise(seconds, 0.3, seed=1), 200, 6000) * envelope
    high_envelope = np.repeat(rng.uniform(0.1, 1.0, seconds * 20), RATE // 20)[: RATE * seconds]
    return mids + _band(noise(seconds, high_level, seed=2), 6000, 22_000) * high_envelope


def test_a_128_kbps_transcode_of_quiet_highs_carries_both_marks_and_its_original_none(tmp_path):
    """LAME at 128 kbps lowpasses at ~16.5 kHz and, short of bits, drops the quiet highs to digital silence."""
    original = music_like()
    clean = _analyse(tmp_path, "original.flac", original)
    assert fq.classify(clean) == "clean"
    transcode = fq._analyse_one(str(tmp_path), mp3_decode(tmp_path, original, 128_000), None)
    assert fq.has_lossy_wall(transcode), transcode
    assert transcode.digital_floor, transcode
    assert fq.classify(transcode) == "lossy"
    level, notes = fq.assess([transcode])
    assert level == "suspect"
    assert "both marks of a lossy encoder" in notes[0]
    assert "128 kbps" in notes[0]


# What a folder is told


def test_clean_tracks_that_stop_at_different_frequencies_are_not_read_as_mixed_sources():
    """A compilation from one store can stop at 17 kHz on one track and 22 kHz on the next."""
    level, notes = fq.assess(
        [_result("01.flac", reach=17_100), _result("02.flac", reach=22_100), _result("03.flac", reach=19_000)]
    )
    assert level == "ok"
    joined = " ".join(notes).lower()
    assert "source" not in joined.replace("where the files came from", "")
    assert "normal for compilations" in joined


def test_both_marks_make_the_folder_suspect_and_name_the_setting():
    lossy = _result("01.flac", reach=18_800, cutoff=18_800, drop=40, slope=150, gating=0.3, abrupt=0.8, digital=True)
    level, notes = fq.assess([lossy, _result("02.flac")])
    assert level == "suspect"
    joined = " ".join(notes)
    assert "192 kbps" in joined
    assert "1 of 2 tracks carries" in joined
    assert "mixed sources" in joined


def test_every_track_lossy_says_transcode_or_lossy_master():
    lossy = _result("01.flac", reach=16_600, cutoff=16_600, drop=40, slope=150, gating=0.2, abrupt=0.6, digital=True)
    level, notes = fq.assess([lossy, lossy])
    assert level == "suspect"
    assert any("Every track" in n and "lossy master" in n for n in notes)


def test_a_wall_alone_asks_for_a_look_and_does_not_call_it_lossy():
    wall_only = _result("01.flac", reach=20_100, cutoff=20_150, drop=44, slope=140)
    level, notes = fq.assess([wall_only])
    assert level == "look"
    joined = " ".join(notes)
    assert "320 kbps" in joined and "not gated" in joined
    assert "both marks" not in joined


def test_gating_alone_needs_to_be_abrupt_before_it_asks_for_a_look():
    slow = _result("01.flac", gating=0.2, abrupt=0.1, digital=True)
    abrupt = _result("02.flac", gating=0.2, abrupt=0.5, digital=True)
    assert fq.classify(slow) == "clean"
    assert fq.classify(abrupt) == "look"


def test_strong_abrupt_gating_is_lossy_even_without_a_wall():
    result = _result("01.flac", gating=0.4, abrupt=0.7, digital=True)
    assert fq.classify(result) == "lossy"
    assert "no lowpass in the encoder range" in fq.describe(result)


def test_per_track_notes_are_capped():
    lossy = [
        _result(f"{i:02d}.flac", reach=16_600, cutoff=16_600, drop=40, slope=150, gating=0.2, abrupt=0.6, digital=True)
        for i in range(12)
    ]
    _level, notes = fq.assess(lossy)
    assert sum(".flac:" in n for n in notes) == fq._MAX_TRACK_NOTES
    assert any("4 more tracks" in n for n in notes)


def test_a_file_that_could_not_be_analysed_is_named_rather_than_skewing_the_folder():
    level, notes = fq.assess([_result("01.flac"), _result("02.flac", reach=0.0, rate=0, error="boom")])
    assert level == "ok"
    assert any("1 of 2 files could not be analysed" in n and "02.flac" in n and "boom" in n for n in notes)


def test_nothing_analysable_says_so():
    level, notes = fq.assess([_result("01.flac", reach=0.0, rate=0, error="boom")])
    assert level == "none"
    assert notes == ["No file could be analysed, e.g. 01.flac: could not be analysed (boom)"]


def test_a_silent_track_does_not_fake_anything():
    assert fq.assess([_result("01.flac"), _result("02.flac", reach=0.0)])[0] == "ok"
    assert fq.assess([_result("02.flac", reach=0.0)]) == ("none", ["No track carried enough signal to measure."])


# Degenerate input: a measurement that cannot be made must not become a claim


def test_a_file_too_short_to_average_reports_no_measurement(tmp_path):
    result = _analyse(tmp_path, "tiny.flac", noise(0.05))
    assert result.windows == 0
    assert result.error
    assert fq.assess([result])[0] == "none"
    assert not (tmp_path / "01 Spectrum.png").exists()


def test_silence_measures_no_reach_rather_than_the_whole_spectrum(tmp_path):
    """A flat spectrum sits within any margin of its own top: silence would read as energy to Nyquist."""
    result = _analyse(tmp_path, "silent.flac", np.zeros(RATE * 3))
    assert result.windows > 0
    assert result.reach_hz == 0.0
    assert fq.classify(result) == "silent"
    assert fq.assess([result])[0] == "none"


def test_a_file_that_is_not_audio_gives_an_error_not_an_exception(tmp_path):
    (tmp_path / "broken.flac").write_bytes(b"fLaC not really")
    result = fq._analyse_one(str(tmp_path), "broken.flac", str(tmp_path / "01 Spectrum.png"))
    assert result.error
    assert fq.assess([result])[0] == "none"


def test_a_corrupt_spectrum_never_yields_a_non_finite_measurement():
    freqs = np.fft.rfftfreq(fq.FFT_SIZE, 1 / RATE)
    wall = fq.measure_wall(freqs, np.full(len(freqs), np.nan))
    assert not wall.found and wall.reach_hz == 0.0
    assert all(math.isfinite(v) for v in wall)
    assert fq._finite(float("nan")) == 0.0


def test_a_failed_plot_costs_only_its_own_file(tmp_path, monkeypatch):
    def boom(_freqs, _db, _rate, out_path, *_args, **_kwargs):
        # A half-written file is what a real failure leaves; the cleanup has to remove it.
        with open(out_path, "wb") as partial:
            partial.write(b"\x89PNG\r\n\x1a\n")
        raise OSError("no space left on device")

    monkeypatch.setattr(fq, "render_plot", boom)
    result = _analyse(tmp_path, "full.flac", noise(3))
    assert result.error == "no space left on device"
    assert result.image == ""
    assert not (tmp_path / "01 Spectrum.png").exists(), "a half-written plot would still show in the viewer"


def test_a_plot_is_written_where_it_is_asked_for_and_only_there(tmp_path):
    write_flac(tmp_path / "03 c.flac", lowpass(noise(3), 16_500))
    result = fq._analyse_one(str(tmp_path), "03 c.flac", str(tmp_path / "01 Spectrum.png"))
    assert result.image == "01 Spectrum.png"
    assert (tmp_path / "01 Spectrum.png").read_bytes().startswith(b"\x89PNG")

    (tmp_path / "01 Spectrum.png").unlink()
    unplotted = fq._analyse_one(str(tmp_path), "03 c.flac", None)
    assert unplotted.image == ""
    assert unplotted == fq.SpectrumResult(**{**msgspec.structs.asdict(result), "image": ""})
    assert not list(tmp_path.glob("*.png"))


def test_plotting_an_impossible_sample_rate_fails_clearly():
    freqs = np.fft.rfftfreq(fq.FFT_SIZE, 1 / RATE)
    with pytest.raises(ValueError, match="0 Hz"):
        fq.render_plot(freqs, np.zeros(len(freqs)), 0, "unused.png", "title")


def test_a_result_without_a_sample_rate_is_not_measured():
    unusable = fq.SpectrumResult(file="x.flac", image="i", sample_rate=0, windows=1, reach_hz=100.0)
    assert fq.assess([unusable])[0] == "none"


def test_a_long_track_is_decoded_only_up_to_the_sample_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(fq, "MAX_SAMPLES", RATE)
    write_flac(tmp_path / "long.flac", noise(5))
    samples, rate = fq.decode_mono(str(tmp_path / "long.flac"))
    assert rate == RATE
    assert len(samples) == RATE
