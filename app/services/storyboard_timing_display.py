"""Read persisted storyboard TTS timing reports for Streamlit.

Only task-owned JSON is accepted. Never treat user-authored text or an arbitrary
filesystem path as a task directory. Timings describe synthesized narration and
planned scene cuts, not independently measured final MP4 mux duration.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from uuid import UUID

from loguru import logger

from app.utils import utils

MAX_REPORT_BYTES = 512 * 1024
MAX_SCENES = 50
MAX_SCENE_SECONDS = 30.0
TIMING_TOLERANCE = 0.03
AUDIO_TOLERANCE = 0.5


class StoryboardTimingError(ValueError):
    """The task timeline cannot be trusted for display."""


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StoryboardTimingError(f"invalid {name}")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise StoryboardTimingError(f"invalid {name}")
    return parsed


def parse_timing_report(document: object) -> dict:
    """Validate the measured TTS timeline and return display-only fields."""
    if not isinstance(document, dict) or document.get("version") != 1:
        raise StoryboardTimingError("unsupported timeline version")
    source_scenes = document.get("scenes")
    if (
        not isinstance(source_scenes, list)
        or not 1 <= len(source_scenes) <= MAX_SCENES
    ):
        raise StoryboardTimingError("timeline has an invalid scene count")

    scenes = []
    previous_end = 0.0
    for index, item in enumerate(source_scenes, start=1):
        if not isinstance(item, dict) or type(item.get("index")) is not int:
            raise StoryboardTimingError("invalid scene entry")
        if item["index"] != index:
            raise StoryboardTimingError("scene order is inconsistent")
        start = _number(item.get("start"), "scene start")
        end = _number(item.get("end"), "scene end")
        duration = _number(item.get("duration"), "scene duration")
        voice_duration = _number(item.get("voice_duration"), "voice duration")
        if (
            start < 0
            or not 0 < duration <= MAX_SCENE_SECONDS
            or not 0 <= voice_duration <= duration + TIMING_TOLERANCE
            or abs(start - previous_end) > TIMING_TOLERANCE
            or abs(end - start - duration) > TIMING_TOLERANCE
        ):
            raise StoryboardTimingError("scene timing is inconsistent")
        voiceover = item.get("voiceover", "")
        mode = item.get("duration_mode")
        if not isinstance(voiceover, str) or len(voiceover) > 5000:
            raise StoryboardTimingError("invalid narration text")
        if mode not in {"auto", "manual"}:
            raise StoryboardTimingError("invalid scene timing mode")
        scenes.append({
            "index": index,
            "start": start,
            "end": end,
            "duration": duration,
            "voice_duration": voice_duration,
            "voiceover": voiceover.strip(),
            "duration_mode": mode,
        })
        previous_end = end

    audio_duration = _number(document.get("total_duration"), "total audio duration")
    if audio_duration <= 0 or abs(audio_duration - previous_end) > AUDIO_TOLERANCE:
        raise StoryboardTimingError("audio and video timeline do not match")
    return {
        "scenes": scenes,
        "audio_duration": audio_duration,
        "timeline_duration": previous_end,
    }


def load_task_timing(task_id: str) -> dict | None:
    """Load a UUID task's report, returning None when absent or malformed."""
    try:
        canonical_id = str(UUID(str(task_id)))
    except (ValueError, TypeError, AttributeError):
        return None
    root = os.path.realpath(utils.task_dir())
    task_directory = os.path.realpath(os.path.join(root, canonical_id))
    try:
        if os.path.commonpath([root, task_directory]) != root:
            return None
    except ValueError:
        return None

    report_path = os.path.join(task_directory, "scene-timeline.json")
    try:
        if os.path.getsize(report_path) > MAX_REPORT_BYTES:
            raise StoryboardTimingError("scene timeline is too large")
        with open(report_path, "rb") as stream:
            content = stream.read(MAX_REPORT_BYTES + 1)
        if len(content) > MAX_REPORT_BYTES:
            raise StoryboardTimingError("scene timeline is too large")
        document = json.loads(content.decode("utf-8"))
        report = parse_timing_report(document)
        report["task_id"] = canonical_id
        return report
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        logger.warning(f"cannot read storyboard timing for task {canonical_id}: {exc}")
        return None


def matches_scene_voiceovers(report: dict, scenes: list) -> bool:
    """Only display a report beside the project whose narration it describes."""
    entries = report.get("scenes", [])
    if len(entries) != len(scenes):
        return False
    for report_scene, scene in zip(entries, scenes):
        if isinstance(scene, dict):
            text = scene.get("voiceover", "")
            mode = scene.get("duration_mode", "manual")
        else:
            text = getattr(scene, "voiceover", "")
            mode = getattr(scene, "duration_mode", "manual")
        if (report_scene["voiceover"] != str(text).strip()
                or report_scene["duration_mode"] != mode):
            return False
    return True


def latest_matching_timing(scenes: list, max_candidates: int = 30) -> dict | None:
    """Find the latest completed or partial TTS report matching scene narration.

    This is a fallback for a browser session that no longer remembers its last
    task ID. The report remains explicitly labeled as a previous render.
    """
    if not scenes:
        return None
    root = utils.task_dir()
    try:
        with os.scandir(root) as iterator:
            candidates = []
            for entry in iterator:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    UUID(entry.name)
                except ValueError:
                    continue
                report = os.path.join(entry.path, "scene-timeline.json")
                try:
                    candidates.append((os.path.getmtime(report), entry.name))
                except OSError:
                    continue
    except OSError:
        return None

    for _, task_id in sorted(candidates, reverse=True)[:max_candidates]:
        timing = load_task_timing(task_id)
        if timing and matches_scene_voiceovers(timing, scenes):
            return timing
    return None
