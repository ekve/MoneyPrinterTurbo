"""Storyboard scene-accurate visual assembly regression tests.

Tests do not call remote image services or perform real encodes.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.models.schema import StoryboardScene, VideoParams
from app.services import storyboard_timeline


def scene(duration=5, mode="manual"):
    return StoryboardScene(
        type="generated_image",
        prompt="Irish tax paperwork",
        duration=duration,
        duration_mode=mode,
        voiceover="An Irish taxpayer",
    )


def config(*scenes):
    return VideoParams(
        video_subject="Taxes AI",
        video_source="storyboard",
        storyboard=list(scenes),
    )


def test_manual_timing_without_tts_timeline(tmp_path):
    with patch.object(storyboard_timeline.utils, "task_dir", return_value=str(tmp_path)):
        durations = storyboard_timeline.scene_durations(
            "task-1", config(scene(duration=4), scene(duration=9))
        )
    assert durations == [4.0, 9.0]


def test_auto_mode_needs_generated_tts_timeline(tmp_path):
    with patch.object(storyboard_timeline.utils, "task_dir", return_value=str(tmp_path)):
        with pytest.raises(storyboard_timeline.StoryboardTimelineError, match="Auto"):
            storyboard_timeline.scene_durations("task-1", config(scene(mode="auto")))


def test_generated_timeline_overrides_placeholder_scene_durations(tmp_path):
    path = tmp_path / "scene-timeline.json"
    path.write_text(json.dumps({
        "version": 1,
        "scenes": [
            {"index": 1, "start": 0, "end": 4.15, "duration": 4.15},
            {"index": 2, "start": 4.15, "end": 10.85, "duration": 6.70},
        ],
    }), encoding="utf-8")
    with patch.object(storyboard_timeline.utils, "task_dir", return_value=str(tmp_path)):
        durations = storyboard_timeline.scene_durations(
            "task-1", config(scene(mode="auto"), scene(mode="auto"))
        )
    assert durations == [4.15, 6.70]


@pytest.mark.parametrize("bad_entry", [
    {"index": 2, "start": 0, "end": 5, "duration": 5},
    {"index": 1, "start": 2, "end": 7, "duration": 5},
    {"index": 1, "start": 0, "end": 8, "duration": 5},
])
def test_timeline_rejects_corrupted_offsets(tmp_path, bad_entry):
    (tmp_path / "scene-timeline.json").write_text(json.dumps({
        "version": 1, "scenes": [bad_entry],
    }))
    with patch.object(storyboard_timeline.utils, "task_dir", return_value=str(tmp_path)):
        with pytest.raises(storyboard_timeline.StoryboardTimelineError, match="invalid"):
            storyboard_timeline.scene_durations("task-1", config(scene()))


class _FakeClip:
    def __init__(self, duration=12, size=(1920, 1080)):
        self.duration = duration
        self.size = size
        self.cuts = []

    def subclipped(self, start, end):
        self.cuts.append((start, end))
        clip = _FakeClip(end - start, self.size)
        return clip


def test_composer_uses_each_visual_once_without_repeating(tmp_path):
    source_a = _FakeClip()
    source_b = _FakeClip()
    outputs = []

    def write_fake(clip, file_name, **kwargs):
        Path(file_name).write_bytes(b"encoded")
        outputs.append((file_name, clip.duration))

    def concat_fake(**kwargs):
        assert len(kwargs["clip_files"]) == 2
        assert [Path(name).read_bytes() for name in kwargs["clip_files"]] == [
            b"encoded", b"encoded",
        ]
        Path(kwargs["output_file"]).write_bytes(b"merged")
        assert kwargs["max_duration"] == pytest.approx(10.7)

    target = tmp_path / "combined.mp4"
    used = []
    with (
        patch.object(storyboard_timeline.voice, "get_audio_duration", return_value=10.7),
        patch.object(
            storyboard_timeline.video, "_open_video_clip_quietly",
            side_effect=[source_a, source_b],
        ) as opened,
        patch.object(
            storyboard_timeline.video, "_write_videofile_with_codec_fallback",
            side_effect=write_fake,
        ),
        patch.object(
            storyboard_timeline.video, "_get_configured_video_codec",
            return_value="libx264",
        ),
        patch.object(storyboard_timeline.video, "concat_video_clips_with_ffmpeg",
                     side_effect=concat_fake) as concat,
        patch.object(storyboard_timeline.video, "close_clip"),
    ):
        result = storyboard_timeline.combine_storyboard_videos(
            combined_video_path=str(target),
            video_paths=["scene-a.mp4", "scene-b.mp4"],
            scene_durations=[4.2, 6.5],
            audio_file="audio.wav",
            used_video_paths=used,
        )

    assert result == str(target)
    assert target.read_bytes() == b"merged"
    assert opened.call_count == 2
    assert source_a.cuts == [(0, 4.2)]
    assert source_b.cuts == [(0, 6.5)]
    assert [duration for _, duration in outputs] == [4.2, 6.5]
    assert used == ["scene-a.mp4", "scene-b.mp4"]
    assert concat.call_count == 1
    assert not list(tmp_path.glob("storyboard-*.mp4"))


def test_composer_rejects_audio_mismatch_before_opening_visuals(tmp_path):
    with (
        patch.object(storyboard_timeline.voice, "get_audio_duration", return_value=13),
        patch.object(storyboard_timeline.video, "_open_video_clip_quietly") as opened,
    ):
        with pytest.raises(storyboard_timeline.StoryboardTimelineError, match="narration"):
            storyboard_timeline.combine_storyboard_videos(
                combined_video_path=str(tmp_path / "combined.mp4"),
                video_paths=["a.mp4", "b.mp4"],
                scene_durations=[4.2, 6.5],
                audio_file="audio.wav",
            )
    opened.assert_not_called()


def test_composer_rejects_short_source_without_repetition(tmp_path):
    with (
        patch.object(storyboard_timeline.voice, "get_audio_duration", return_value=8),
        patch.object(
            storyboard_timeline.video,
            "_open_video_clip_quietly",
            return_value=_FakeClip(duration=5),
        ),
        patch.object(storyboard_timeline.video, "close_clip"),
        patch.object(storyboard_timeline.video, "concat_video_clips_with_ffmpeg") as concat,
    ):
        with pytest.raises(storyboard_timeline.StoryboardTimelineError, match="too short"):
            storyboard_timeline.combine_storyboard_videos(
                combined_video_path=str(tmp_path / "combined.mp4"),
                video_paths=["scene.mp4"],
                scene_durations=[8],
                audio_file="audio.wav",
            )
    concat.assert_not_called()
