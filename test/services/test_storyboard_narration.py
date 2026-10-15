"""Storyboard per-scene TTS tests, without external TTS calls or FFmpeg encoding."""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.models.schema import StoryboardScene, VideoParams
from app.services import storyboard_narration


def params(*scenes, voice_name="en-IE-EmilyNeural"):
    return VideoParams(
        video_subject="Taxes AI",
        video_source="storyboard",
        voice_name=voice_name,
        storyboard=list(scenes),
    )


def ai_scene(text="Hello", duration=None, duration_mode="auto"):
    return StoryboardScene(
        type="generated_image",
        prompt="An accountant at work",
        voiceover=text,
        duration=duration,
        duration_mode=duration_mode,
    )


def test_scene_script_joins_in_storyboard_order():
    settings = params(ai_scene("First"), ai_scene("Second"))
    assert storyboard_narration.scene_script(settings) == "First\n\nSecond"
    assert storyboard_narration.scene_script(params(ai_scene(""))) == ""


@pytest.mark.parametrize(
    "scene,audio,expected",
    [
        (ai_scene("A", duration=8, duration_mode="manual"), 3.2, 8.0),
        (ai_scene("A", duration=5, duration_mode="manual"), 6.2, 6.3),
        (ai_scene("A", duration=5, duration_mode="auto"), 6.2, 6.3),
        (ai_scene("", duration=7, duration_mode="manual"), 0.0, 7.0),
    ],
)
def test_scene_duration_never_truncates_speech(scene, audio, expected):
    assert storyboard_narration._duration_for_scene(scene, audio) == expected


def test_scene_duration_rejects_unvoiced_auto_and_too_long():
    with pytest.raises(storyboard_narration.StoryboardNarrationError, match="non-empty"):
        storyboard_narration._duration_for_scene(ai_scene(""), 0)
    with pytest.raises(storyboard_narration.StoryboardNarrationError, match="Shorten"):
        storyboard_narration._duration_for_scene(ai_scene("Too long"), 35)


def test_generate_scene_narration_produces_timeline_and_combined_audio(tmp_path):
    settings = params(
        ai_scene("A short introduction"),
        ai_scene("", duration=4, duration_mode="manual"),
        ai_scene("Final thought", duration=8, duration_mode="manual"),
    )
    tts_calls = []
    joined = []

    def fake_tts(**kwargs):
        tts_calls.append(kwargs)
        Path(kwargs["voice_file"]).write_bytes(b"mp3")
        return object()

    def fake_join(tracks, durations, output):
        joined.append((tracks, durations, output))
        Path(output).write_bytes(b"RIFF placeholder")

    def fake_duration(filename):
        name = Path(filename).name
        return {
            "storyboard-scene-01.mp3": 3.2,
            "storyboard-scene-03.mp3": 4.7,
            "audio.wav": 15.3,
        }[name]

    with (
        patch.object(storyboard_narration.utils, "task_dir", return_value=str(tmp_path)),
        patch.object(storyboard_narration.voice, "tts", side_effect=fake_tts),
        patch.object(storyboard_narration.voice, "get_audio_duration", side_effect=fake_duration),
        patch.object(storyboard_narration, "_join_scene_audio", side_effect=fake_join),
    ):
        audio_path, duration, maker = storyboard_narration.generate_scene_narration(
            "task-1", settings
        )

    assert audio_path == str(tmp_path / "audio.wav")
    assert duration == 15.3
    assert len(tts_calls) == 2
    assert [call["text"] for call in tts_calls] == [
        "A short introduction", "Final thought"
    ]
    assert joined[0][0][1] is None  # Silent manual scene
    assert joined[0][1] == [3.3, 4.0, 8.0]
    timeline = json.loads((tmp_path / "scene-timeline.json").read_text())
    assert timeline["total_duration"] == 15.3
    assert [scene["start"] for scene in timeline["scenes"]] == [0.0, 3.3, 7.3]
    assert timeline["scenes"][-1]["end"] == 15.3
    assert maker.subs
    assert maker.offset[0][0] == 0
    assert maker.offset[-1][1] == pytest.approx(
        (7.3 + 4.7) * 10_000_000, abs=2
    )


def test_auto_scene_without_voice_preflight_skips_paid_tts(tmp_path):
    settings = params(
        ai_scene("Valid paid scene"),
        ai_scene("", duration_mode="auto"),
    )
    with (
        patch.object(storyboard_narration.utils, "task_dir", return_value=str(tmp_path)),
        patch.object(storyboard_narration.voice, "tts") as synthesize,
    ):
        with pytest.raises(storyboard_narration.StoryboardNarrationError, match="scene 2"):
            storyboard_narration.generate_scene_narration("task-1", settings)
    synthesize.assert_not_called()


def test_tts_error_stops_before_join(tmp_path):
    settings = params(ai_scene("Text"))
    with (
        patch.object(storyboard_narration.utils, "task_dir", return_value=str(tmp_path)),
        patch.object(storyboard_narration.voice, "tts", return_value=None),
        patch.object(storyboard_narration, "_join_scene_audio") as combine,
    ):
        with pytest.raises(storyboard_narration.StoryboardNarrationError, match="scene 1"):
            storyboard_narration.generate_scene_narration("task-1", settings)
    combine.assert_not_called()


def test_empty_storyboard_or_invalid_voice_fails():
    with pytest.raises(storyboard_narration.StoryboardNarrationError, match="voiceover"):
        storyboard_narration.generate_scene_narration("task-1", params(ai_scene("")))
    with pytest.raises(storyboard_narration.StoryboardNarrationError, match="valid voice"):
        storyboard_narration.generate_scene_narration(
            "task-1", params(ai_scene("Hello"), voice_name="")
        )


def test_ffmpeg_join_uses_exact_durations_and_atomic_output(tmp_path):
    source = tmp_path / "input.mp3"
    source.write_bytes(b"fake")
    output = tmp_path / "audio.wav"
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"RIFF audio")
        return SimpleNamespace(returncode=0, stderr="")

    with (
        patch.object(storyboard_narration.utils, "get_ffmpeg_binary", return_value="ffmpeg"),
        patch.object(storyboard_narration.subprocess, "run", side_effect=fake_run),
    ):
        storyboard_narration._join_scene_audio(
            [str(source), None], [4.2, 3.0], str(output)
        )

    assert output.read_bytes() == b"RIFF audio"
    args = commands[0]
    assert args.count("-i") == 1
    filter_complex = args[args.index("-filter_complex") + 1]
    assert "apad=whole_dur=4.200" in filter_complex
    assert "anullsrc=r=48000:cl=mono" in filter_complex
    assert "concat=n=2:v=0:a=1" in filter_complex


def test_ffmpeg_failed_join_does_not_replace_prior_file(tmp_path):
    source = tmp_path / "input.mp3"
    source.write_bytes(b"mp3")
    output = tmp_path / "audio.wav"
    output.write_bytes(b"earlier audio")

    with (
        patch.object(storyboard_narration.utils, "get_ffmpeg_binary", return_value="ffmpeg"),
        patch.object(
            storyboard_narration.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=1, stderr="failure"),
        ),
    ):
        with pytest.raises(storyboard_narration.StoryboardNarrationError, match="concatenate"):
            storyboard_narration._join_scene_audio(
                [str(source)], [5.0], str(output)
            )
    assert output.read_bytes() == b"earlier audio"
