"""The lossy-master question (#540): the frequency analysis is printed before it and sets its default.

Ported from chodeus's fork, then fitted to this tree's prompt: yes_all still answers "no" unasked when no mark is found.
"""

import os
from functools import partial

import anyio
import pytest
from test_uploader_frequency import (  # pyright: ignore[reportMissingImports]
    RATE,
    lowpass,
    mp3_decode,
    music_like,
    noise,
    write_flac,
)

from salmon.uploader import frequency
from salmon.uploader import spectrals as sp
from salmon.uploader.frequency import SpectrumResult


def run(func, *args, **kwargs):
    return anyio.run(partial(func, *args, **kwargs))


def _lossy(name):
    return SpectrumResult(
        file=name,
        image="01 Spectrum.png",
        sample_rate=44100,
        windows=10,
        reach_hz=16_600,
        cutoff_hz=16_600,
        knee_hz=16_450,
        floor_hz=16_750,
        drop_db=40,
        slope_db_per_khz=150,
        gating=0.3,
        gated_band="16-17 kHz",
        gating_abrupt=0.7,
        digital_floor=True,
    )


def _wall_only(name):
    return SpectrumResult(
        file=name,
        image="01 Spectrum.png",
        sample_rate=44100,
        windows=10,
        reach_hz=16_600,
        cutoff_hz=16_600,
        knee_hz=16_450,
        floor_hz=16_750,
        drop_db=40,
        slope_db_per_khz=150,
    )


def _clean(name):
    return SpectrumResult(file=name, image="01 Spectrum.png", sample_rate=44100, windows=10, reach_hz=21_000)


@pytest.fixture(autouse=True)
def _interactive(monkeypatch) -> None:
    monkeypatch.setattr(sp.cfg.upload, "yes_all", False)


@pytest.fixture
def flow(monkeypatch, tmp_path):
    """check_spectrals with every slow or interactive step replaced by a recorder."""
    events: list[str] = []
    printed: list[str] = []
    state: dict = {"spectra": [_lossy("01.flac")], "answer": False}

    async def generate_all(path, spectrals_path, audio_info):
        events.append("spectrograms-all")
        return {1: "01.flac"}

    async def measure(path, files, plot_paths):
        events.append("measure")
        return state["spectra"]

    async def view(spectrals_path, ids):
        events.append("view")

    async def prompt(force_prompt_lossy_master=False, offer_deletion=True, marks_found=False):
        events.append("prompt")
        state["forced"] = force_prompt_lossy_master
        state["offer_deletion"] = offer_deletion
        state["marks_found"] = marks_found
        return state["answer"]

    async def prompt_spectrals(*_a, **_k):
        return {}

    async def generate_ids(path, track_ids, spectrals_path, audio_info):
        events.append("spectrograms-ids")
        return {i: f"{i:02d}.flac" for i in track_ids}

    monkeypatch.setattr(sp, "create_specs_folder", lambda path: str(tmp_path))
    monkeypatch.setattr(sp, "generate_spectrals_all", generate_all)
    monkeypatch.setattr(sp, "generate_spectrals_ids", generate_ids)
    monkeypatch.setattr(frequency, "generate_frequency_plots", measure)
    monkeypatch.setattr(sp, "get_audio_files", lambda path, *_a: ["01.flac"])
    monkeypatch.setattr(sp, "view_spectrals", view)
    monkeypatch.setattr(sp, "prompt_lossy_master", prompt)
    monkeypatch.setattr(sp, "prompt_spectrals", prompt_spectrals)
    monkeypatch.setattr(sp.click, "secho", lambda message="", **_kw: printed.append(str(message)))
    monkeypatch.setattr(sp.click, "echo", lambda message="", **_kw: printed.append(str(message)))
    return events, printed, state


def test_the_measurements_are_printed_before_the_spectrals_and_the_question(flow):
    events, printed, state = flow
    run(sp.check_spectrals, "/album", {"01.flac": {}}, None, None)
    assert events == ["spectrograms-all", "measure", "view", "prompt"]
    joined = "\n".join(printed)
    assert "Frequency analysis: the marks of a lossy encoder" in joined
    assert "01.flac: brick-wall at 16.6 kHz" in joined
    assert '"NN Spectrum.png"' in joined
    assert "A measurement, not a verdict" in joined
    assert state["marks_found"] is True


def test_preselected_ids_are_generated_before_the_measurement_and_the_question(flow):
    events, _printed, state = flow
    lossy, ids = run(sp.check_spectrals, "/album", {"01.flac": {}}, None, (1,))
    assert events == ["spectrograms-ids", "measure", "prompt"]
    assert state["marks_found"] is True
    assert lossy is False
    assert ids == {1: "01.flac"}


def test_the_answer_is_the_persons_not_the_measurements(flow):
    _events, _printed, state = flow
    lossy, _ids = run(sp.check_spectrals, "/album", {"01.flac": {}}, None, None)
    assert lossy is False, "a suspect measurement must not answer the question"


@pytest.mark.parametrize("spectrum", [_clean, _wall_only])
def test_without_both_marks_the_default_stays_no(flow, spectrum):
    _events, printed, state = flow
    state["spectra"] = [spectrum("01.flac")]
    run(sp.check_spectrals, "/album", {"01.flac": {}}, None, None)
    assert state["marks_found"] is False
    assert any(line.startswith("\nFrequency analysis: ") for line in printed)


def test_the_check_after_upload_is_measured_too_and_never_offers_deletion(flow):
    _events, _printed, state = flow
    # As post_upload_spectral_check calls it.
    run(sp.check_spectrals, "/album", {"01.flac": {}}, None, None, force_prompt_lossy_master=True, offer_deletion=False)
    assert state["marks_found"] is True
    assert state["forced"] is True
    assert state["offer_deletion"] is False


def test_without_the_lossy_check_the_measurement_is_printed_and_nothing_is_asked(flow):
    events, printed, _state = flow
    lossy, _ids = run(sp.check_spectrals, "/album", {"01.flac": {}}, None, None, check_lma=False)
    assert lossy is None
    assert events == ["spectrograms-all", "measure", "view"]
    assert any(line.startswith("\nFrequency analysis: ") for line in printed)


def test_a_pre_answered_question_skips_the_measurement(flow):
    events, _printed, _state = flow
    lossy, _ids = run(sp.check_spectrals, "/album", {"01.flac": {}}, True, None)
    assert lossy is True
    assert "measure" not in events


def test_a_failed_measurement_prints_one_line_and_asks_as_before(flow, monkeypatch):
    events, printed, state = flow

    async def boom(path, files, plot_paths):
        raise RuntimeError("decoder exploded")

    monkeypatch.setattr(frequency, "generate_frequency_plots", boom)
    lossy, _ids = run(sp.check_spectrals, "/album", {"01.flac": {}}, None, None)
    assert lossy is False
    assert events == ["spectrograms-all", "view", "prompt"]
    assert state["marks_found"] is False
    assert [line for line in printed if "Frequency analysis" in line] == [
        "\nFrequency analysis failed, so it says nothing about this release: RuntimeError('decoder exploded')"
    ]


# The question itself


@pytest.fixture
def asked(monkeypatch):
    """Answer the lossy-master question with its default, and record the prompts and defaults."""
    record: list[tuple[str, str | None]] = []

    async def fake_prompt(text, default=None, **_k):
        record.append((text, default))
        return default

    monkeypatch.setattr(sp.click, "prompt", fake_prompt)
    monkeypatch.setattr(sp, "flush_stdin", lambda: None)
    return record


def test_the_default_follows_the_analysis(asked):
    answer = run(sp.prompt_lossy_master, marks_found=True)
    assert answer is True
    answer = run(sp.prompt_lossy_master, marks_found=False)
    assert answer is False
    assert [default for _text, default in asked] == ["y", "n"]
    assert "[Y]es, [n]o" in asked[0][0]
    assert "[y]es, [N]o" in asked[1][0]


def test_yes_all_asks_when_the_marks_are_found_and_answers_no_unasked_otherwise(monkeypatch, asked):
    monkeypatch.setattr(sp.cfg.upload, "yes_all", True)
    answer = run(sp.prompt_lossy_master)
    assert answer is False
    assert asked == []
    answer = run(sp.prompt_lossy_master, marks_found=True)
    assert answer is True
    assert len(asked) == 1


def test_after_upload_the_question_offers_no_deletion_even_with_marks(asked):
    run(sp.prompt_lossy_master, True, offer_deletion=False, marks_found=True)
    assert "[d]elete" not in asked[0][0]


def test_yes_all_asks_for_the_lossy_comment_when_there_is_no_source_url(monkeypatch):
    """With yes_all, a "yes" to the question led to a lossy comment it refused forever without a source URL."""
    monkeypatch.setattr(sp.cfg.upload, "yes_all", True)
    refusals: list[str] = []

    def refuse(message="", **_kw):
        refusals.append(message)
        if len(refusals) > 3:
            raise AssertionError("the empty comment is refused again and again")

    async def fake_prompt(*_a, **_k):
        return "Bought on the label's site."

    monkeypatch.setattr(sp.click, "secho", refuse)
    monkeypatch.setattr(sp.click, "prompt", fake_prompt)
    comment = run(sp.generate_lossy_approval_comment, None, ["01.flac"])
    assert comment == "Bought on the label's site."
    comment = run(sp.generate_lossy_approval_comment, "https://store.test/album", ["01.flac"])
    assert comment == ""


# The real analysis, on files


def test_the_real_analysis_writes_a_plot_per_track_and_finds_a_transcode(tmp_path, capsys):
    album = tmp_path / "album"
    album.mkdir()
    specs = tmp_path / "specs"
    specs.mkdir()
    mp3_decode(album, music_like(), 128_000, name="01 a.flac")
    write_flac(album / "02 b.flac", lowpass(noise(4, tilt_db_per_octave=-3), 21_300))
    (album / "t128000.mp3").unlink()

    marks_found = run(sp.print_frequency_analysis, str(album), str(specs), {1: "01 a.flac", 2: "02 b.flac"})
    assert marks_found is True

    assert sorted(p.name for p in specs.iterdir()) == ["01 Spectrum.png", "02 Spectrum.png"]
    out = capsys.readouterr().out
    assert "Frequency analysis: the marks of a lossy encoder" in out
    assert "01 a.flac: brick-wall at" in out
    assert "02 b.flac" not in out, "a clean track gets no line of its own"


def test_with_spectral_ids_given_each_plot_is_named_after_its_own_tracks_spectrals(monkeypatch, tmp_path):
    """-s 3 makes "01 Full.png" of track 3: its plot must be "01 Spectrum.png" too, not track 1's."""
    album = tmp_path / "album"
    album.mkdir()
    names = ["01 a.flac", "02 b.flac", "03 c.flac"]
    for name in names:
        write_flac(album / name, noise(2))

    async def fake_sox(args, **_kwargs):
        for i, arg in enumerate(args):
            if arg == "-o":
                with open(args[i + 1], "w") as image:
                    image.write(os.path.basename(args[2]))  # the image says which track it is of

    async def not_lossy(*_a, **_k):
        return False

    plotted: list = []
    real_generate = frequency.generate_frequency_plots

    async def spy(path, files, plot_paths):
        results = await real_generate(path, files, plot_paths)
        plotted.extend((r.file, r.image) for r in results if r.image)
        return results

    monkeypatch.setattr(sp.anyio, "run_process", fake_sox)
    monkeypatch.setattr(sp.cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(sp.cfg.upload.compression, "compress_spectrals", False)
    monkeypatch.setattr(sp, "prompt_lossy_master", not_lossy)
    monkeypatch.setattr(frequency, "generate_frequency_plots", spy)

    _lossy, ids = run(sp.check_spectrals, str(album), {name: {"duration": 2} for name in names}, None, (3,))

    assert ids == {1: "03 c.flac"}
    specs = album / "Spectrals"
    assert (specs / "01 Full.png").read_text() == "03 c.flac"
    assert plotted == [("03 c.flac", "01 Spectrum.png")]
    assert sorted(p.name for p in specs.iterdir()) == ["01 Full.png", "01 Spectrum.png", "01 Zoom.png"]


def test_the_real_analysis_of_a_clean_album_keeps_the_default(tmp_path, capsys):
    write_flac(tmp_path / "01 a.flac", noise(4, tilt_db_per_octave=-3, rate=RATE))
    specs = tmp_path / "Spectrals"
    specs.mkdir()

    marks_found = run(sp.print_frequency_analysis, str(tmp_path), str(specs), {1: "01 a.flac"})
    assert marks_found is False
    assert "Frequency analysis: no mark of a lossy encoder" in capsys.readouterr().out


def test_the_real_analysis_of_unreadable_files_does_not_raise(tmp_path, capsys):
    (tmp_path / "01 a.flac").write_bytes(b"")
    (tmp_path / "02 b.flac").write_bytes(b"fLaC")
    specs = tmp_path / "Spectrals"
    specs.mkdir()

    marks_found = run(sp.print_frequency_analysis, str(tmp_path), str(specs), {1: "01 a.flac", 2: "02 b.flac"})
    assert marks_found is False
    out = capsys.readouterr().out
    assert "Frequency analysis: nothing could be measured" in out
    assert "No file could be analysed" in out
    assert list(specs.iterdir()) == []


def test_the_plots_are_never_uploaded(monkeypatch, tmp_path):
    for name in ("01 Full.png", "01 Zoom.png", "01 Spectrum.png"):
        (tmp_path / name).write_bytes(b"png")
    uploaded: list[str] = []

    async def upload(spectrals_list, tracker=None):
        uploaded.extend(path for _sid, _filename, paths in spectrals_list for path in paths)
        return {}

    monkeypatch.setattr(sp, "upload_spectral_imgs", upload)
    run(sp.upload_spectrals, str(tmp_path), {1: "01 a.flac"})
    assert sorted(os.path.basename(path) for path in uploaded) == ["01 Full.png", "01 Zoom.png"]
