"""Helpers of tests/test_uploader_frequency.py that other test modules use too."""

import av
import av.audio.stream
import numpy as np

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


def music_like(seconds=10, high_level=0.0005):
    """Loud mids under a slow envelope, and quiet highs whose level wanders, as in a mix."""
    rng = np.random.default_rng(3)
    envelope = np.repeat(rng.uniform(0.3, 1.0, seconds * 10), RATE // 10)[: RATE * seconds]
    mids = _band(noise(seconds, 0.3, seed=1), 200, 6000) * envelope
    high_envelope = np.repeat(rng.uniform(0.1, 1.0, seconds * 20), RATE // 20)[: RATE * seconds]
    return mids + _band(noise(seconds, high_level, seed=2), 6000, 22_000) * high_envelope
