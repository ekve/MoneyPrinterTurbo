"""Storyboard visual timeline: render every scene exactly once in authored order.

The normal stock-video compositor intentionally loops materials to fill the
voiceover. Storyboards instead have an explicit per-scene soundtrack timeline.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path

from loguru import logger

from app.models.schema import VideoAspect, VideoFitMode, VideoParams
from app.services import video, voice
from app.utils import utils

SCENE_AUDIO_TOLERANCE = 0.25


class StoryboardTimelineError(ValueError):
    """The scene timing cannot be rendered without shifting the narration."""


def scene_durations(task_id: str, params: VideoParams) -> list[float]:
    """Read generated TTS timeline, otherwise use strictly manual durations."""
    scenes = list(params.storyboard or [])
    if not scenes:
        raise StoryboardTimelineError("storyboard requires at least one scene")

    timeline_path = Path(utils.task_dir(task_id)) / "scene-timeline.json"
    if not timeline_path.is_file():
        if any(scene.duration_mode == "auto" for scene in scenes):
            raise StoryboardTimelineError(
                "Auto scene timing requires per-scene TTS. "
                "Select Automatic Voiceover, or set all scenes to Manual."
            )
        durations = [float(scene.duration or params.video_clip_duration) for scene in scenes]
    else:
        try:
            document = json.loads(timeline_path.read_text(encoding="utf-8"))
            entries = document["scenes"]
            if document.get("version") != 1 or len(entries) != len(scenes):
                raise ValueError("scene count or version mismatch")
            durations = []
            offset = 0.0
            for index, entry in enumerate(entries, start=1):
                duration = float(entry["duration"])
                start = float(entry["start"])
                end = float(entry["end"])
                if entry["index"] != index or abs(start - offset) > 0.02:
                    raise ValueError("scene order or offset mismatch")
                if abs(end - start - duration) > 0.02:
                    raise ValueError("inconsistent scene duration")
                durations.append(duration)
                offset = end
        except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            raise StoryboardTimelineError("invalid generated scene-timeline.json") from exc

    if any(not math.isfinite(value) or not 0 < value <= 30.0 for value in durations):
        raise StoryboardTimelineError("each storyboard scene must last 0-30 seconds")
    return durations


def combine_storyboard_videos(
    *,
    combined_video_path: str,
    video_paths: list[str],
    scene_durations: list[float],
    audio_file: str,
    video_aspect: VideoAspect = VideoAspect.landscape,
    video_fit_mode: VideoFitMode = VideoFitMode.cover,
    threads: int = 2,
    used_video_paths: list[str] | None = None,
    progress_callback=None,
) -> str:
    """Render each image/video once with a scene-accurate cut, no cycling.

    Inputs from storyboard.build_storyboard_materials() are verified local
    videos. The builder creates extra source frames to cover fractional cuts.
    """
    if not video_paths or len(video_paths) != len(scene_durations):
        raise StoryboardTimelineError(
            "scene count does not match prepared visual material count"
        )
    if any(not math.isfinite(d) or not 0 < d <= 30 for d in scene_durations):
        raise StoryboardTimelineError("invalid scene duration")

    expected_duration = sum(scene_durations)
    audio_duration = voice.get_audio_duration(audio_file)
    if not math.isfinite(audio_duration) or audio_duration <= 0:
        raise StoryboardTimelineError("combined narration is not readable")
    if abs(audio_duration - expected_duration) > SCENE_AUDIO_TOLERANCE:
        raise StoryboardTimelineError(
            f"storyboard images total {expected_duration:.2f}s, "
            f"but narration is {audio_duration:.2f}s. "
            "Use per-scene TTS or adjust manual scene timings."
        )

    width, height = VideoAspect(video_aspect).to_resolution()
    directory = os.path.dirname(os.path.abspath(combined_video_path))
    temporary_clips: list[str] = []

    try:
        for index, (source_path, target_duration) in enumerate(
            zip(video_paths, scene_durations), start=1
        ):
            source = None
            output_clip = None
            temporary_name = None
            try:
                source = video._open_video_clip_quietly(source_path)
                if not math.isfinite(float(source.duration)) or source.duration < target_duration:
                    raise StoryboardTimelineError(
                        f"scene {index} visual is too short: "
                        f"{source.duration:.2f}s available, {target_duration:.2f}s required"
                    )
                output_clip = source.subclipped(0, target_duration)
                if output_clip.size != (width, height):
                    output_clip = video._fit_clip_to_canvas(
                        output_clip,
                        target_width=width,
                        target_height=height,
                        fit_mode=video_fit_mode,
                    )
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f"storyboard-{index:02d}-",
                    suffix=".mp4",
                    dir=directory,
                )
                os.close(descriptor)
                video._write_videofile_with_codec_fallback(
                    output_clip,
                    temporary_name,
                    codec=video._get_configured_video_codec(),
                    logger=None,
                    fps=video.fps,
                )
                temporary_clips.append(temporary_name)
                temporary_name = None
                logger.info(
                    f"storyboard visual scene {index}/{len(scene_durations)}: "
                    f"{target_duration:.3f}s"
                )
                if progress_callback is not None:
                    video._report_clip_progress(
                        progress_callback,
                        sum(scene_durations[:index]),
                        expected_duration,
                    )
            finally:
                video.close_clip(output_clip if output_clip is not None else source)
                if temporary_name:
                    video.delete_files(temporary_name)

        # No repeat, random shuffling, or stock-video duration safety margin.
        video.concat_video_clips_with_ffmpeg(
            clip_files=temporary_clips,
            output_file=combined_video_path,
            threads=threads,
            output_dir=directory,
            max_duration=expected_duration,
        )
        if used_video_paths is not None:
            used_video_paths.extend(video_paths)
        return combined_video_path
    finally:
        video.delete_files(temporary_clips)
