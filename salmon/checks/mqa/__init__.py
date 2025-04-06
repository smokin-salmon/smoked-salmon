"""
MIT License

Copyright (c) 2018 redsudo

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import io
import struct
import wave

from bitstring import Bits

from . import flac

MAGIC = Bits("0xbe0498c88")


def twos_complement(n, bits):
    mask = 2 ** (bits - 1)
    return -(n & mask) + (n & ~mask)


def iter_i24_as_i32(data):
    for low_byte, h in struct.iter_unpack("<BH", data):
        yield twos_complement(h << 8 | low_byte, 24) << 8


def iter_i16_as_i32(data):
    for (x,) in struct.iter_unpack("<h", data):
        yield x << 16


def peek(f, n):
    o = f.tell()
    r = f.read(n)
    f.seek(o)
    return r


def check_mqa(path):
    with open(path, "rb") as f:
        magic = peek(f, 4)

        if magic == b"fLaC":
            with flac.BitInputStream(f) as bf:
                f = io.BytesIO()
                flac.decode_file(bf, f, seconds=1)
                f.seek(0)
        else:
            return False

        with wave.open(f) as wf:
            nchannels, sampwidth, framerate, *_ = wf.getparams()

            if nchannels != 2:
                raise ValueError("Input must be stereo")

            if sampwidth == 3:
                iter_data = iter_i24_as_i32
            elif sampwidth == 2:
                iter_data = iter_i16_as_i32
            else:
                raise ValueError("Input must be 16- or 24-bit")

            sound_data = wf.readframes(framerate)

    samples = list(iter_data(sound_data))
    streams = (
        Bits((x ^ y) >> p & 1 for x, y in zip(samples[::2], samples[1::2], strict=False))
        for p in range(16, 24)
    )

    return bool(any(s.find(MAGIC) for s in streams))
