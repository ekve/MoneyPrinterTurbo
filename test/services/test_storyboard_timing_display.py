"""Measured storyboard timing report tests, without TTS or video encoding."""

import json
from uuid import uuid4

import pytest

from app.services import storyboard_timing_display as timing


def example_report(*, voiceover="Taxes AI makes paperwork easier"):
    return {
        "version": 1,
        "total_duration": 11.31,
        "scenes": [
            {
                "index": 1, "start": 0.0, "end": 4.2,
                "duration": 4.2, "voice_duration": 4.1,
                "voiceover": voiceover, "duration_mode": "auto",
            },
            {
                "index": 2, "start": 4.2, "end": 11.3,
                "duration": 7.1, "voice_duration": 5.8,
                "voiceover": "For taxpayers and accountants",
                "duration_mode": "manual",
            },
        ],
    }


def matching_scenes():
    return [
        {"voiceover": "Taxes AI makes paperwork easier", "duration_mode": "auto"},
        {"voiceover": "For taxpayers and accountants", "duration_mode": "manual"},
    ]


def save_report(root, task_id, content):
    folder = root / task_id
    folder.mkdir(parents=True)
    (folder / "scene-timeline.json").write_text(
        json.dumps(content), encoding="utf-8"
    )


def test_parse_report_returns_real_scene_and_audio_durations():
    report = timing.parse_timing_report(example_report())
    assert report["audio_duration"] == pytest.approx(11.31)
    assert report["timeline_duration"] == pytest.approx(11.3)
    assert [scene["duration"] for scene in report["scenes"]] == [4.2, 7.1]
    assert [scene["voice_duration"] for scene in report["scenes"]] == [4.1, 5.8]
    assert report["scenes"][1]["start"] == 4.2


def test_matching_requires_exact_narration_order_and_modes():
    report = timing.parse_timing_report(example_report())
    assert timing.matches_scene_voiceovers(report, matching_scenes())
    swapped = matching_scenes()[::-1]
    assert not timing.matches_scene_voiceovers(report, swapped)
    changed = matching_scenes()
    changed[0]["voiceover"] = "Different scene"
    assert not timing.matches_scene_voiceovers(report, changed)
    changed = matching_scenes()
    changed[1]["duration_mode"] = "auto"
    assert not timing.matches_scene_voiceovers(report, changed)


@pytest.mark.parametrize("field,value", [
    ("total_duration", -1.0),
    ("total_duration", float("inf")),
    ("total_duration", "11.3"),
])
def test_rejects_invalid_total_duration(field, value):
    document = example_report()
    document[field] = value
    with pytest.raises(timing.StoryboardTimingError):
        timing.parse_timing_report(document)


@pytest.mark.parametrize("field,value", [
    ("index", 2),
    ("start", 2.0),
    ("duration", 32.0),
    ("end", 12.0),
    ("voice_duration", 99.0),
    ("duration_mode", "unknown"),
])
def test_rejects_invalid_scene_values(field, value):
    document = example_report()
    document["scenes"][0][field] = value
    with pytest.raises(timing.StoryboardTimingError):
        timing.parse_timing_report(document)


def test_load_rejects_non_uuid_task_id(tmp_path, monkeypatch):
    monkeypatch.setattr(timing.utils, "task_dir", lambda: str(tmp_path))
    assert timing.load_task_timing("../secret") is None
    assert timing.load_task_timing("any folder") is None


def test_load_report_by_uuid(tmp_path, monkeypatch):
    monkeypatch.setattr(timing.utils, "task_dir", lambda: str(tmp_path))
    task_id = str(uuid4())
    save_report(tmp_path, task_id, example_report())
    result = timing.load_task_timing(task_id)
    assert result is not None
    assert result["task_id"] == task_id
    assert result["scenes"][0]["duration"] == pytest.approx(4.2)


def test_corrupt_or_large_file_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(timing.utils, "task_dir", lambda: str(tmp_path))
    task_id = str(uuid4())
    folder = tmp_path / task_id
    folder.mkdir()
    file_path = folder / "scene-timeline.json"
    file_path.write_text("{invalid")
    assert timing.load_task_timing(task_id) is None
    file_path.write_bytes(b"x" * (timing.MAX_REPORT_BYTES + 1))
    assert timing.load_task_timing(task_id) is None


def test_find_last_render_matching_current_scene_text(tmp_path, monkeypatch):
    monkeypatch.setattr(timing.utils, "task_dir", lambda: str(tmp_path))
    older = str(uuid4())
    latest = str(uuid4())
    save_report(tmp_path, older, example_report())
    save_report(tmp_path, latest, example_report(voiceover="unrelated project"))
    last = timing.latest_matching_timing(matching_scenes())
    assert last is not None
    assert last["task_id"] == older


def test_no_matching_timeline_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(timing.utils, "task_dir", lambda: str(tmp_path))
    assert timing.latest_matching_timing(matching_scenes()) is None
