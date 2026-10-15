"""Generate and concatenate a separate TTS track for each storyboard scene.

The explicit scene-timeline.json is consumed by future scene-accurate video
composition. Existing stock-video audio generation remains unchanged.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
from pathlib import Path

from loguru import logger

from app.models.schema import VideoParams
from app.services import voice
from app.utils import utils

SAMPLE_RATE = 48000
MAX_SCENE_SECONDS = 30.0
AUDIO_TAIL_SECONDS = 0.10


class StoryboardNarrationError(ValueError):
    """A scene could not be narrated without cutting off speech."""


def scene_script(params: VideoParams) -> str:
    """Scene narration is authoritative when any scene has voiceover."""
    scenes = list(params.storyboard or [])
    if not scenes or not any(scene.voiceover.strip() for scene in scenes):
        return ""
    return "\n\n".join(scene.voiceover.strip() for scene in scenes if scene.voiceover.strip())


def _duration_for_scene(scene, audio_seconds: float) -> float:
    """Never trim narration; pad manual scenes if necessary."""
    if not math.isfinite(audio_seconds) or audio_seconds < 0:
        raise StoryboardNarrationError("invalid generated scene audio duration")

    minimum = audio_seconds + AUDIO_TAIL_SECONDS if audio_seconds else 0.0
    if scene.duration_mode == "auto":
        if not audio_seconds:
            raise StoryboardNarrationError(
                "auto-timed scenes require non-empty voiceover"
            )
        duration = minimum
    else:
        duration = max(float(scene.duration or 5), minimum)

    if duration > MAX_SCENE_SECONDS:
        raise StoryboardNarrationError(
            f"scene narration needs {duration:.2f}s, exceeds {MAX_SCENE_SECONDS:.0f}s. "
            "Shorten the voiceover or split it into two scenes."
        )
    return round(duration, 3)


def _build_subtitles(timeline: list[dict]):
    """Sentence-level caption offsets derived from measured scene audio.

    Word boundaries are not approximated as exact. Whisper and original provider
    word-level behavior remain available in existing non-storyboard flows.
    """
    sub_maker = voice.ensure_legacy_submaker_fields(voice.SubMaker())
    sub_maker.cues = []
    sub_maker.subs = []
    sub_maker.offset = []

    for entry in timeline:
        text = entry["voiceover"]
        if not text:
            continue
        maker = voice.populate_legacy_submaker_with_full_text(
            voice.SubMaker(),
            text,
            entry["voice_duration"],
        )
        shift = int(round(entry["start"] * 10_000_000))
        for sentence, (start, end) in zip(maker.subs, maker.offset):
            sub_maker.subs.append(sentence)
            sub_maker.offset.append((start + shift, end + shift))
    sub_maker.duration = timeline[-1]["end"]
    return sub_maker


def _join_scene_audio(
    tracks: list[str | None],
    durations: list[float],
    output_file: str,
) -> None:
    """Use FFmpeg's sample-level concat for TTS from different providers."""
    if len(tracks) != len(durations) or not tracks:
        raise StoryboardNarrationError("invalid scene audio inputs")

    command = [utils.get_ffmpeg_binary(), "-nostdin", "-hide_banner",
               "-loglevel", "error", "-y"]
    for path in tracks:
        if path is not None:
            command.extend(["-i", path])

    filters = []
    labels = []
    input_index = 0
    for index, (track, duration) in enumerate(zip(tracks, durations)):
        label = f"s{index}"
        labels.append(f"[{label}]")
        if track is None:
            filters.append(
                f"anullsrc=r={SAMPLE_RATE}:cl=mono,"
                f"atrim=duration={duration:.3f},asetpts=PTS-STARTPTS[{label}]"
            )
        else:
            filters.append(
                f"[{input_index}:a]aresample={SAMPLE_RATE},"
                f"aformat=sample_fmts=s16:sample_rates={SAMPLE_RATE}:channel_layouts=mono,"
                f"apad=whole_dur={duration:.3f},atrim=duration={duration:.3f},"
                f"asetpts=PTS-STARTPTS[{label}]"
            )
            input_index += 1

    filters.append(
        "".join(labels) + f"concat=n={len(labels)}:v=0:a=1[outa]"
    )
    directory = os.path.dirname(output_file)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".storyboard-audio-", suffix=".wav", dir=directory
    )
    os.close(descriptor)
    try:
        command.extend(
            ["-filter_complex", ";".join(filters), "-map", "[outa]",
             "-ar", str(SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le", temporary]
        )
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=600, check=False
        )
        if completed.returncode != 0 or not os.path.isfile(temporary) or os.path.getsize(temporary) == 0:
            raise StoryboardNarrationError(
                "FFmpeg could not concatenate the storyboard narration"
                + (f": {completed.stderr[-500:]}" if completed.stderr else "")
            )
        os.replace(temporary, output_file)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StoryboardNarrationError(
            "FFmpeg failed while combining storyboard scene narration"
        ) from exc
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def generate_scene_narration(
    task_id: str,
    params: VideoParams,
    *,
    voxcpm_reference_audio: bytes | None = None,
    voxcpm_prompt_audio: bytes | None = None,
    voxcpm_prompt_text: str = "",
) -> tuple[str, float, object]:
    """Return audio.wav, its measured duration and a combined subtitle maker."""
    scenes = list(params.storyboard or [])
    if not scenes or not any(scene.voiceover.strip() for scene in scenes):
        raise StoryboardNarrationError("storyboard scenes do not contain voiceover text")
    if not (params.voice_name or "").strip() or voice.is_no_voice(params.voice_name):
        raise StoryboardNarrationError("select a valid voice for storyboard narration")

    task_directory = utils.task_dir(task_id)
    tracks: list[str | None] = []
    durations: list[float] = []
    timeline: list[dict] = []
    offset = 0.0

    for index, scene in enumerate(scenes, start=1):
        narration = scene.voiceover.strip()
        track_path = None
        audio_seconds = 0.0

        if narration:
            track_path = os.path.join(task_directory, f"storyboard-scene-{index:02d}.mp3")
            tts_kwargs = {
                "text": narration,
                "voice_name": voice.parse_voice_name(params.voice_name),
                "voice_rate": params.voice_rate,
                "voice_file": track_path,
            }
            if voxcpm_reference_audio is not None:
                tts_kwargs["voxcpm_reference_audio"] = voxcpm_reference_audio
            if voxcpm_prompt_audio is not None:
                tts_kwargs["voxcpm_prompt_audio"] = voxcpm_prompt_audio
                tts_kwargs["voxcpm_prompt_text"] = voxcpm_prompt_text

            if voice.tts(**tts_kwargs) is None:
                raise StoryboardNarrationError(f"scene {index}: TTS returned no audio")
            audio_seconds = voice.get_audio_duration(track_path)
            if not math.isfinite(audio_seconds) or audio_seconds <= 0:
                raise StoryboardNarrationError(
                    f"scene {index}: TTS audio file is empty or unreadable"
                )

        try:
            scene_seconds = _duration_for_scene(scene, audio_seconds)
        except StoryboardNarrationError as exc:
            raise StoryboardNarrationError(f"scene {index}: {exc}") from exc

        logger.info(
            f"storyboard audio scene {index}: narration={audio_seconds:.2f}s, "
            f"scene={scene_seconds:.2f}s, mode={scene.duration_mode}"
        )
        timeline.append({
            "index": index,
            "start": round(offset, 3),
            "end": round(offset + scene_seconds, 3),
            "duration": scene_seconds,
            "voice_duration": round(audio_seconds, 3),
            "voiceover": narration,
            "duration_mode": scene.duration_mode,
        })
        offset += scene_seconds
        tracks.append(track_path)
        durations.append(scene_seconds)

    output = os.path.join(task_directory, "audio.wav")
    _join_scene_audio(tracks, durations, output)
    measured_duration = voice.get_audio_duration(output)
    if not math.isfinite(measured_duration) or measured_duration <= 0:
        raise StoryboardNarrationError("combined narration audio is empty or unreadable")

    # Do not store absolute host paths in the reusable timeline metadata.
    data = {"version": 1, "total_duration": measured_duration, "scenes": timeline}
    timeline_file = os.path.join(task_directory, "scene-timeline.json")
    descriptor, temporary = tempfile.mkstemp(
        prefix=".scene-timeline-", suffix=".json", dir=task_directory
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, timeline_file)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

    return output, measured_duration, _build_subtitles(timeline)
