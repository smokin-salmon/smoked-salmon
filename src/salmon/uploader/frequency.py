"""Averaged-spectrum plots, and the two marks a lossy encoder leaves that a master does not.

A brick-wall lowpass where MP3 and AAC encoders cut, and highs that flip between
content and the bit-depth floor as the encoder runs short of bits. A master rolls
off gently or cuts hard only near Nyquist, as sample-rate conversion does, and its
quiet is noise, never digital silence. PyAV decodes, numpy measures, Pillow draws.

This module loads numpy, PyAV and Pillow: import it when the analysis runs, not when salmon starts.
"""

import contextlib
import math
import os
from typing import Any, NamedTuple

import anyio.to_thread
import av
import msgspec
import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageDraw

from salmon.common.files import process_files

FFT_SIZE = 8192
HOP = FFT_SIZE // 2
MAX_WINDOWS = 6000  # ~9 min at 44.1 kHz with 50% overlap; plenty for an average
MAX_SAMPLES = MAX_WINDOWS * HOP
TEXTURE_FFT = 2048  # 46 ms frames: short enough to see an encoder gate the highs
TEXTURE_HOP = TEXTURE_FFT // 2
# Digital silence averages to the epsilon floor (-300 dB). Any real audio, even
# a single LSB of dither, lands far above this.
SILENCE_DB = -250.0

_WIDTH, _HEIGHT = 1400, 620
_PAD_L, _PAD_R, _PAD_T, _PAD_B = 70, 20, 34, 46
_DB_MIN, _DB_MAX = -140.0, 0.0
_BG = (16, 16, 20)
_GRID = (48, 48, 56)
_AXIS = (90, 90, 100)
_LABEL = (150, 150, 160)
_CURVE = (255, 170, 60)
_MARK = (90, 200, 255)

# These thresholds are signal heuristics measured on synthetic and real encodes, not tracker rules.
#
# The wall. Energy "reaches" as far as the average stays above -100 dB, about where a
# spectrogram goes dark. A wall is a step down to the band under Nyquist, so it is
# searched from where the curve last clears that band by _TOP_MARGIN_DB.
_REACH_FLOOR_DB = -100.0
_TOP_MARGIN_DB = 10.0
# How far the band under a wall must sit above the band above it for the step to be measured at all.
_WALL_MEASURE_DEPTH_DB = 15.0
# A lossy wall: a step at least this deep, falling at least this fast. A mastering roll-off is
# shallower or slower.
_WALL_MIN_DEPTH_DB = 20.0
_WALL_MIN_SLOPE_DB_PER_KHZ = 20.0
# Where a wall's floor lands for MP3/AAC lowpasses; 20.6 kHz keeps LAME 320 (<= 20.4 kHz)
# apart from sample-rate-conversion walls (>= 20.9 kHz). Both tables key on floor_hz.
_LOSSY_WALL_HZ = (12_800.0, 20_600.0)
_ENCODER_LOWPASSES = (
    ("MP3 at ~80 kbps", 12_800.0, 13_700.0),
    ("MP3 at 96-112 kbps", 15_000.0, 15_600.0),
    ("MP3 at 128 kbps", 16_500.0, 17_100.0),
    ("MP3 at ~160 kbps or AAC at ~128 kbps", 17_200.0, 18_700.0),
    ("MP3 at 192 kbps or V2", 18_750.0, 19_100.0),
    ("MP3 at 224-256 kbps or V0", 19_300.0, 19_800.0),
    ("MP3 at 320 kbps", 20_100.0, 20_600.0),
)
# The gate. The high bands watched for switching between content and the floor.
_TEXTURE_BANDS = (
    (16_000.0, 17_000.0),
    (17_000.0, 18_000.0),
    (18_000.0, 19_000.0),
    (19_000.0, 20_000.0),
    (20_000.0, 21_000.0),
)
# A frame is "loud" when its 2-8 kHz level is within this of the track's 90th percentile, and
# gating is only measured over at least this many loud frames (about 2 s).
_LOUD_WINDOW_DB = 20.0
_MIN_LOUD_FRAMES = 40
# 16-bit rounding noise averages -132 dB per bin at TEXTURE_FFT, TPDF dither
# -128. A quiet band that sits here was empty, not merely quiet.
_DIGITAL_FLOOR_DB = -126.5
# A band is "off" within this of the quietest any band gets, and "on" at least this far above it.
_OFF_MARGIN_DB = 6.0
_ON_MARGIN_DB = 20.0
# Share of loud frames a band must spend both on and off: enough with a wall, enough alone to
# ask for a look, and enough alone to count as a lossy mark; the _ABRUPT values are the share
# of on/off switches that must happen between adjacent frames, as an encoder's do.
_GATED_WITH_WALL = 0.08
_GATED_LOOK, _GATED_LOOK_ABRUPT = 0.12, 0.25
_GATED_STRONG, _GATED_STRONG_ABRUPT = 0.25, 0.40
_MAX_TRACK_NOTES = 8


class SpectrumResult(msgspec.Struct, frozen=True):
    """One track's measurements."""

    file: str
    image: str
    sample_rate: int
    windows: int
    reach_hz: float = 0.0  # highest frequency still carrying energy
    cutoff_hz: float = 0.0  # brick-wall midpoint; 0 when there is no wall
    knee_hz: float = 0.0
    floor_hz: float = 0.0
    drop_db: float = 0.0  # how far the band above the wall sits below the band under it
    slope_db_per_khz: float = 0.0
    floor_db: float = 0.0  # level of the band above the wall
    gating: float = 0.0  # share of loud frames with a high band at its floor, given it carries content in others
    gated_band: str = ""
    gating_abrupt: float = 0.0  # share of on/off switches that happen between adjacent frames
    digital_floor: bool = False  # the quiet state of that band is the bit-depth floor, not noise
    error: str | None = None


class Wall(NamedTuple):
    reach_hz: float
    knee_hz: float
    floor_hz: float
    depth_db: float
    slope_db_per_khz: float
    floor_db: float

    @property
    def found(self) -> bool:
        return self.floor_hz > 0

    @property
    def cutoff_hz(self) -> float:
        return (self.knee_hz + self.floor_hz) / 2 if self.found else 0.0


class Gating(NamedTuple):
    share: float
    band: str
    abrupt: float
    off_level_db: float

    @property
    def digital(self) -> bool:
        return self.band != "" and self.off_level_db <= _DIGITAL_FLOOR_DB


_NO_GATING = Gating(0.0, "", 0.0, 0.0)


def decode_mono(path: str) -> tuple[NDArray[np.float32], int]:
    """Decode up to MAX_SAMPLES of the track, downmixed to mono float32."""
    container = av.open(path)
    try:
        stream = container.streams.audio[0]
        sample_rate = stream.rate or 44100
        # Downmix: a cutoff that exists in one channel only still shows here.
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=sample_rate)
        chunks: list[NDArray[Any]] = []
        total = 0
        for frame in container.decode(stream):
            for resampled in resampler.resample(frame):
                chunk = resampled.to_ndarray().reshape(-1)
                chunks.append(chunk)
                total += len(chunk)
            if total >= MAX_SAMPLES:
                break
        # The resampler buffers; without a flush its last partial frame is lost.
        for resampled in resampler.resample(None):
            chunks.append(resampled.to_ndarray().reshape(-1))
    finally:
        container.close()
    samples = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
    return samples[:MAX_SAMPLES].astype(np.float32, copy=False), sample_rate


def _frame_powers(samples: NDArray[np.floating], size: int, hop: int, chunk: int = 256):
    """Yield Hann-windowed power spectra in blocks; a full-scale sine reads ~0 dB."""
    window = np.hanning(size).astype(np.float32)
    scale = (size * 0.5) ** 2
    count = (len(samples) - size) // hop + 1
    for start in range(0, max(count, 0), chunk):
        stop = min(start + chunk, count)
        index = np.arange(size)[None, :] + hop * np.arange(start, stop)[:, None]
        yield np.abs(np.fft.rfft(samples[index] * window, axis=1)) ** 2 / scale


def _db(power: NDArray[np.floating]) -> NDArray[np.floating]:
    return 10 * np.log10(np.maximum(power, 1e-30))


def average_spectrum(
    samples: NDArray[np.floating], sample_rate: int
) -> tuple[NDArray[np.floating], NDArray[np.floating], int]:
    """Welch-average the track into one magnitude curve: (frequencies, dB, windows)."""
    acc = np.zeros(FFT_SIZE // 2 + 1, dtype=np.float64)
    windows = 0
    for power in _frame_powers(samples, FFT_SIZE, HOP):
        acc += power.sum(axis=0)
        windows += len(power)
    return np.fft.rfftfreq(FFT_SIZE, 1 / sample_rate), _db(acc / max(windows, 1)), windows


def _smooth(values: NDArray[np.floating], bins: int = 21) -> NDArray[np.floating]:
    # Edge padding: zero padding would drag the last bins towards 0 dB.
    padded = np.pad(values, bins // 2, mode="edge")
    return np.convolve(padded, np.ones(bins) / bins, mode="valid")


def measure_wall(freqs: NDArray[np.floating], db: NDArray[np.floating]) -> Wall:
    """Where the energy stops, and whether it stops at a wall or slides away.

    Everything is measured against the band under Nyquist, never the peak: a
    peak-relative cutoff moves with tonal balance and made dark masters look filtered.
    """
    if not np.isfinite(db).all() or db.max() <= SILENCE_DB:
        return Wall(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    nyquist = float(freqs[-1])
    curve = _smooth(db)
    top_band = (freqs >= nyquist - 600) & (freqs <= nyquist - 100)
    if not top_band.any():
        top_band = freqs >= nyquist * 0.95
    top = float(np.median(curve[top_band]))
    visible = np.nonzero(curve >= _REACH_FLOOR_DB)[0]
    if not len(visible):
        # Nothing clears the floor: silence, or so quiet the plot is dark throughout.
        return Wall(0.0, 0.0, 0.0, 0.0, 0.0, top)
    reach = float(freqs[visible[-1]])
    above = np.nonzero(curve >= top + _TOP_MARGIN_DB)[0]
    # A curve that never rises above its own top is flat to Nyquist: no wall.
    edge = float(freqs[above[-1]]) if len(above) else nyquist
    if edge >= nyquist - 800:
        return Wall(reach, 0.0, 0.0, 0.0, 0.0, top)
    under = (freqs >= edge - 1500) & (freqs <= edge - 500)
    if not under.any():
        return Wall(reach, 0.0, 0.0, 0.0, 0.0, top)
    level_under = float(np.median(curve[under]))
    depth = level_under - top
    if depth < _WALL_MEASURE_DEPTH_DB:
        return Wall(reach, 0.0, 0.0, depth, 0.0, top)
    knee = float(freqs[np.nonzero(curve >= level_under - 6)[0][-1]])
    floored = np.nonzero((freqs > knee) & (curve <= top + 3))[0]
    floor_hz = float(freqs[floored[0]]) if len(floored) else nyquist
    width = max(floor_hz - knee, 40.0)
    return Wall(reach, knee, floor_hz, depth, depth / (width / 1000), top)


def measure_gating(samples: NDArray[np.floating], sample_rate: int) -> Gating:
    """How often a high band sits at its floor while the music is loud.

    The floor is the quietest the bands ever get; whether that quiet is digital
    silence is decided separately, since a clean production falls to its own noise too.
    """
    freqs = np.fft.rfftfreq(TEXTURE_FFT, 1 / sample_rate)
    nyquist = sample_rate / 2
    mid_mask = (freqs >= 2000) & (freqs < 8000)
    bands = [(lo, hi) for lo, hi in _TEXTURE_BANDS if hi <= nyquist]
    if not bands or not mid_mask.any():
        return _NO_GATING
    masks = [(freqs >= lo) & (freqs < hi) for lo, hi in bands]
    mids: list[NDArray[np.floating]] = []
    levels: list[list[NDArray[np.floating]]] = [[] for _ in bands]
    for power in _frame_powers(samples, TEXTURE_FFT, TEXTURE_HOP):
        mids.append(power[:, mid_mask].mean(axis=1))
        for i, mask in enumerate(masks):
            levels[i].append(power[:, mask].mean(axis=1))
    if not mids:
        return _NO_GATING
    mid = _db(np.concatenate(mids))
    loud = mid >= np.percentile(mid, 90) - _LOUD_WINDOW_DB
    if loud.sum() < _MIN_LOUD_FRAMES:
        return _NO_GATING
    stacked = np.stack([_db(np.concatenate(level))[loud] for level in levels])
    finite = stacked[stacked > SILENCE_DB]
    if not len(finite):
        return _NO_GATING
    floor = float(np.percentile(finite, 0.5))
    best = _NO_GATING
    for (lo, hi), band in zip(bands, stacked, strict=True):
        off = band <= floor + _OFF_MARGIN_DB
        on = band >= floor + _ON_MARGIN_DB
        share = float(min(off.mean(), on.mean()))
        if share <= best.share:
            continue
        label = f"{lo / 1000:g}-{hi / 1000:g} kHz"
        best = Gating(share, label, _abruptness(on, off), float(np.median(band[off])))
    return best


def _abruptness(on: NDArray[np.bool_], off: NDArray[np.bool_]) -> float:
    """Share of on/off switches with no in-between frame: an encoder gates, a decay slides."""
    state = np.full(len(on), -1)
    state[on] = 1
    state[off] = 0
    switches = abrupt = 0
    previous, gap = -1, 0
    for current in state:
        if current == -1:
            gap += 1
            continue
        if previous != -1 and current != previous:
            switches += 1
            abrupt += gap == 0
        previous, gap = current, 0
    return abrupt / switches if switches else 0.0


def encoder_hint(floor_hz: float) -> str:
    """Name the encoder setting whose lowpass floors out here, if one does."""
    for name, lo, hi in _ENCODER_LOWPASSES:
        if lo <= floor_hz <= hi:
            return name
    return ""


def render_plot(
    freqs: NDArray[np.floating],
    db: NDArray[np.floating],
    sample_rate: int,
    out_path: str,
    title: str,
    wall: Wall | None = None,
) -> None:
    """Draw the curve with a labelled kHz/dB grid, and mark the wall the notes talk about."""
    if sample_rate <= 0:
        raise ValueError(f"cannot plot a spectrum at {sample_rate} Hz")
    img = Image.new("RGB", (_WIDTH, _HEIGHT), _BG)
    draw = ImageDraw.Draw(img)
    plot_w = _WIDTH - _PAD_L - _PAD_R
    plot_h = _HEIGHT - _PAD_T - _PAD_B
    nyquist = sample_rate / 2

    def x_of(hz: float) -> float:
        return _PAD_L + plot_w * (hz / nyquist)

    def y_of(value: float) -> float:
        clamped = min(max(value, _DB_MIN), _DB_MAX)
        return _PAD_T + plot_h * (1 - (clamped - _DB_MIN) / (_DB_MAX - _DB_MIN))

    draw.text((_PAD_L, 10), title, fill=_LABEL)
    for khz in range(0, int(nyquist / 1000) + 1, 2):
        x = x_of(khz * 1000)
        draw.line([(x, _PAD_T), (x, _PAD_T + plot_h)], fill=_GRID)
        draw.text((x - 8, _PAD_T + plot_h + 6), f"{khz}k", fill=_LABEL)
    for value in range(int(_DB_MIN), int(_DB_MAX) + 1, 20):
        y = y_of(value)
        draw.line([(_PAD_L, y), (_PAD_L + plot_w, y)], fill=_GRID)
        draw.text((8, y - 6), f"{value} dB", fill=_LABEL)
    draw.rectangle([_PAD_L, _PAD_T, _PAD_L + plot_w, _PAD_T + plot_h], outline=_AXIS)

    draw.line([(x_of(f), y_of(v)) for f, v in zip(freqs, db, strict=True)], fill=_CURVE, width=2)
    if wall is not None and wall.found:
        x = x_of(wall.cutoff_hz)
        draw.line([(x, _PAD_T), (x, _PAD_T + plot_h)], fill=_MARK, width=1)
        draw.text((x + 4, _PAD_T + 4), f"wall {wall.cutoff_hz / 1000:.1f}k", fill=_MARK)
        y = y_of(wall.floor_db)
        draw.line([(x, y), (_PAD_L + plot_w, y)], fill=_MARK, width=1)
    img.save(out_path)


def plot_filename(index: int) -> str:
    """The name of the plot of the file at this zero-based index.

    It sorts between the file's "NN Full.png" and "NN Zoom.png" spectrals, so a viewer that opens a folder's
    first image still opens a spectral.
    """
    return f"{index + 1:02d} Spectrum.png"


def _analyse_one(album_path: str, filename: str, out_dir: str, index: int) -> SpectrumResult:
    try:
        samples, sample_rate = decode_mono(os.path.join(album_path, filename))
        freqs, db, windows = average_spectrum(samples, sample_rate)
    except Exception as e:
        # One unreadable file must not cost the measurements of the others.
        return SpectrumResult(file=filename, image="", sample_rate=0, windows=0, error=str(e) or type(e).__name__)
    if not windows:
        return SpectrumResult(
            file=filename, image="", sample_rate=sample_rate, windows=0, error="too little audio to average a spectrum"
        )
    wall = measure_wall(freqs, db)
    gating = measure_gating(samples, sample_rate)
    image = plot_filename(index)
    out_path = os.path.join(out_dir, image)
    try:
        render_plot(freqs, db, sample_rate, out_path, filename, wall)
    except Exception as e:
        # A half-written PNG would still show in the spectrals viewer.
        with contextlib.suppress(OSError):
            os.remove(out_path)
        return SpectrumResult(
            file=filename, image="", sample_rate=sample_rate, windows=windows, error=str(e) or type(e).__name__
        )
    return SpectrumResult(
        file=filename,
        image=image,
        sample_rate=sample_rate,
        windows=windows,
        reach_hz=wall.reach_hz,
        cutoff_hz=wall.cutoff_hz,
        knee_hz=wall.knee_hz,
        floor_hz=wall.floor_hz,
        drop_db=_finite(wall.depth_db),
        slope_db_per_khz=_finite(wall.slope_db_per_khz),
        floor_db=_finite(wall.floor_db),
        gating=gating.share,
        gated_band=gating.band,
        gating_abrupt=gating.abrupt,
        digital_floor=gating.digital,
    )


def _finite(value: float) -> float:
    # A corrupt decode gives NaN, which no threshold compares true against and no note should print.
    return value if math.isfinite(value) else 0.0


def has_lossy_wall(result: SpectrumResult) -> bool:
    """A steep, deep step that ends inside the band where encoders cut."""
    return (
        result.cutoff_hz > 0
        and _LOSSY_WALL_HZ[0] <= result.floor_hz <= _LOSSY_WALL_HZ[1]
        and result.drop_db >= _WALL_MIN_DEPTH_DB
        and result.slope_db_per_khz >= _WALL_MIN_SLOPE_DB_PER_KHZ
    )


def classify(result: SpectrumResult) -> str:
    """One of silent, clean, look, lossy: from the two marks, never from the cutoff alone."""
    if result.error or result.sample_rate <= 0 or not result.reach_hz:
        return "silent"
    wall = has_lossy_wall(result)
    gated = result.digital_floor and result.gating >= _GATED_WITH_WALL
    strong = result.digital_floor and result.gating >= _GATED_STRONG and result.gating_abrupt >= _GATED_STRONG_ABRUPT
    if (wall and gated) or strong:
        return "lossy"
    look = result.digital_floor and result.gating >= _GATED_LOOK and result.gating_abrupt >= _GATED_LOOK_ABRUPT
    if wall or look:
        return "look"
    return "clean"


def describe(result: SpectrumResult) -> str:
    """One sentence of measurements for a track."""
    if result.error:
        return f"{result.file}: could not be analysed ({result.error})"
    verdict = classify(result)
    if verdict == "silent":
        return f"{result.file}: too quiet to measure."
    khz = f"{result.cutoff_hz / 1000:.1f} kHz"
    hint = encoder_hint(result.floor_hz)
    where = f", where {hint} cuts" if hint else ""
    gate = (
        f"the {result.gated_band} band drops to the bit-depth floor in {result.gating:.0%} of loud frames"
        if result.digital_floor
        else ""
    )
    hires = f" in a {result.sample_rate / 1000:g} kHz file" if result.sample_rate > 50_000 else ""
    if verdict == "lossy":
        if has_lossy_wall(result):
            return (
                f"{result.file}: brick-wall at {khz}{hires} ({result.drop_db:.0f} dB down over "
                f"{result.floor_hz - result.knee_hz:.0f} Hz{where}) and {gate}: both marks of a lossy encoder."
            )
        return (
            f"{result.file}: no lowpass in the encoder range, but {gate}, switching abruptly. "
            f"A lossy encoder running short of bits does this."
        )
    if verdict == "look":
        if has_lossy_wall(result):
            return (
                f"{result.file}: brick-wall at {khz}{hires} ({result.drop_db:.0f} dB down over "
                f"{result.floor_hz - result.knee_hz:.0f} Hz{where}), but the highs are not gated. A high-bitrate "
                f"transcode looks like this, and so does a mastering lowpass; read the zoom."
            )
        return (
            f"{result.file}: {gate} with no lowpass in the encoder range. "
            f"A lossy source can do this; so can a very clean production."
        )
    reach = f"energy to {result.reach_hz / 1000:.1f} kHz"
    if result.cutoff_hz > 0 and result.floor_hz > _LOSSY_WALL_HZ[1]:
        return (
            f"{result.file}: {reach}, then a steep fall at {khz} ending above the band where encoders cut, "
            f"typical of sample-rate conversion; not a lossy mark."
        )
    if result.cutoff_hz > 0:
        return (
            f"{result.file}: {reach}, then a steep fall at {khz} ending below the band where encoders cut; "
            f"not a lossy mark."
        )
    return f"{result.file}: {reach}; no lossy signature."


def summarize(results: list[SpectrumResult]) -> tuple[str, list[str]]:
    """The folder's level and the lines that speak for it as a whole, per-track lines aside.

    The level is "suspect" (a track carries a lossy encoder's marks), "look" (one mark, not both), "ok", or
    "none" when nothing could be measured.
    """
    analysed = [r for r in results if not r.error and r.sample_rate > 0]
    failed = [r for r in results if r not in analysed]
    unread = (
        [f"{len(failed)} of {len(results)} files could not be analysed, e.g. {describe(failed[0])}"]
        if failed and analysed
        else []
    )
    if not analysed:
        return "none", [f"No file could be analysed, e.g. {describe(failed[0])}"] if failed else []
    verdicts = {r.file: classify(r) for r in analysed}
    measured = [r for r in analysed if verdicts[r.file] != "silent"]
    if not measured:
        return "none", ["No track carried enough signal to measure.", *unread]
    lossy = [r for r in measured if verdicts[r.file] == "lossy"]
    look = [r for r in measured if verdicts[r.file] == "look"]
    clean = [r for r in measured if verdicts[r.file] == "clean"]
    if lossy:
        if len(lossy) == len(measured):
            return "suspect", [
                "Every track carries the marks of a lossy encoder. Read the spectrals with that in mind: if it "
                "holds, this is a transcode or a lossy master.",
                *unread,
            ]
        carry = "carries" if len(lossy) == 1 else "carry"
        rest = (
            f"; the other {len(clean)} measure clean, which is what a folder with mixed sources looks like."
            if clean and not look
            else "."
        )
        return "suspect", [
            f"{len(lossy)} of {len(measured)} tracks {carry} the marks of a lossy encoder{rest}",
            *unread,
        ]
    if look:
        return "look", [
            f"{len(look)} of {len(measured)} tracks show one of the two marks, not both. "
            "Read the spectrogram and the zoom before deciding.",
            *unread,
        ]
    reaches = [r.reach_hz for r in measured]
    low, high = min(reaches) / 1000, max(reaches) / 1000
    span = f"{low:.1f} kHz" if high - low < 0.5 else f"{low:.1f}-{high:.1f} kHz"
    notes = [f"Nothing here has the shape of a lossy encoder. Energy reaches {span} across the tracks."]
    if high - low >= 1.5:
        notes.append(
            "Cutoffs that differ between tracks are normal for compilations and for masters made in "
            "different sessions; they say nothing about where the files came from."
        )
    return "ok", [*notes, *unread]


def assess(results: list[SpectrumResult]) -> tuple[str, list[str]]:
    """What the numbers show per flagged track, then what they add up to for the folder: (level, notes).

    See summarize for the levels.
    """
    if not results:
        return "none", []
    flagged = [r for r in results if classify(r) in ("lossy", "look")]
    notes = [describe(r) for r in flagged[:_MAX_TRACK_NOTES]]
    if len(flagged) > _MAX_TRACK_NOTES:
        notes.append(f"... and {len(flagged) - _MAX_TRACK_NOTES} more tracks like these.")
    level, summary = summarize(results)
    return level, notes + summary


async def generate_frequency_plots(album_path: str, files: list[str], out_dir: str) -> list[SpectrumResult]:
    """Measure each file and write its averaged-spectrum plot into out_dir, an existing folder.

    Each file is decoded once, up to MAX_SAMPLES. A file that cannot be measured gets a result with its error.
    """
    return await process_files(
        files,
        lambda filename, index: anyio.to_thread.run_sync(_analyse_one, album_path, filename, out_dir, index),
        "Frequency analysis",
    )
