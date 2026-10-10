"""Portable, bounded and validated Storyboard ZIP projects.

Format v1 keeps each scene's narration text as metadata. Actual per-scene TTS and
automatic timing are implemented in a later pipeline stage, not by this module.
"""

from __future__ import annotations

import io
import json
import os
import stat
import zipfile
from pathlib import Path
from uuid import uuid4

from app.services import material_upload
from app.utils import file_security, utils

FORMAT = "moneyprinterturbo-storyboard"
VERSION = 1
MAX_ARCHIVE_BYTES = 60 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_SCENES = 50
MAX_ASSETS = 50
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}


class StoryboardProjectError(ValueError):
    """An imported project is malformed, unsafe or incompatible."""


def _asset_path(value: str) -> str:
    if not isinstance(value, str) or "\\" in value or not value.startswith("assets/"):
        raise StoryboardProjectError("local scene file must be an assets/<image> path")
    parts = value.split("/")
    if len(parts) != 2 or not parts[1] or parts[1] in {".", ".."}:
        raise StoryboardProjectError("nested or unsafe asset paths are not supported")
    if Path(parts[1]).suffix.lower() not in IMAGE_EXTENSIONS:
        raise StoryboardProjectError("Storyboard v1 accepts PNG and JPG assets only")
    try:
        material_upload.sanitize_material_filename(parts[1])
    except material_upload.MaterialUploadError as exc:
        raise StoryboardProjectError("invalid asset filename") from exc
    return value


def _string(value, name: str, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise StoryboardProjectError(f"{name} must be a string of at most {limit} characters")
    return value


def _duration(value) -> tuple[int, str]:
    if value == "auto":
        return 5, "auto"  # Placeholder only; exact TTS durations come in stage 2.
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 30:
        raise StoryboardProjectError("scene duration must be 1-30 seconds or 'auto'")
    return value, "manual"


def _validate_manifest(manifest: object, names: set[str]) -> dict:
    if not isinstance(manifest, dict):
        raise StoryboardProjectError("storyboard.json must contain a JSON object")
    if manifest.get("format") != FORMAT or manifest.get("version") != VERSION:
        raise StoryboardProjectError("unsupported storyboard project format or version")
    title = _string(manifest.get("title", ""), "title", 200)
    aspect = manifest.get("video_aspect", "16:9")
    if aspect not in ("16:9", "9:16", "1:1"):
        raise StoryboardProjectError("video_aspect must be 16:9, 9:16 or 1:1")
    voice = _string(manifest.get("voice", ""), "voice", 150)
    raw_scenes = manifest.get("scenes")
    if not isinstance(raw_scenes, list) or not 1 <= len(raw_scenes) <= MAX_SCENES:
        raise StoryboardProjectError("a project requires 1-50 scenes")
    scenes = []
    for index, raw in enumerate(raw_scenes, start=1):
        if not isinstance(raw, dict):
            raise StoryboardProjectError(f"scene {index} must be a JSON object")
        scene_type = raw.get("type")
        if scene_type not in ("local", "generated_image"):
            raise StoryboardProjectError(f"scene {index} has an unsupported type")
        prompt = _string(raw.get("prompt", ""), f"scene {index} prompt", 2000)
        narration = _string(raw.get("voiceover", ""), f"scene {index} voiceover", 5000)
        seconds, duration_mode = _duration(raw.get("duration", 5))
        file_name = ""
        if scene_type == "generated_image":
            if not prompt.strip():
                raise StoryboardProjectError(f"scene {index} requires an image prompt")
        else:
            file_name = _asset_path(raw.get("file", ""))
            if file_name not in names:
                raise StoryboardProjectError(f"scene {index} references a missing asset")
        scenes.append({
            "id": uuid4().hex,
            "type": scene_type,
            "prompt": prompt,
            "file": file_name,
            "duration": seconds,
            "duration_mode": duration_mode,
            "voiceover": narration,
        })
    return {"title": title, "video_aspect": aspect, "voice": voice, "scenes": scenes}


def import_project(archive: bytes) -> dict:
    """Import one ZIP, validate every member, persist images, roll back on failure."""
    if not isinstance(archive, bytes) or not archive or len(archive) > MAX_ARCHIVE_BYTES:
        raise StoryboardProjectError("Storyboard ZIP exceeds the 60 MB limit or is empty")

    try:
        container = zipfile.ZipFile(io.BytesIO(archive))
    except (zipfile.BadZipFile, OSError) as exc:
        raise StoryboardProjectError("invalid Storyboard ZIP archive") from exc

    with container:
        infos = container.infolist()
        if not infos or len(infos) > MAX_ASSETS + 3:
            raise StoryboardProjectError("too many ZIP entries")
        names = set()
        total = 0
        for info in infos:
            name = info.filename
            if (not name or "\\" in name or name.startswith("/")
                    or name.casefold() in names):
                raise StoryboardProjectError("duplicate or unsafe ZIP entry")
            names.add(name.casefold())
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise StoryboardProjectError("ZIP symlinks are not permitted")
            if info.is_dir():
                if name != "assets/":
                    raise StoryboardProjectError("unsupported ZIP directory")
                continue
            if name == "storyboard.json":
                size_limit = MAX_MANIFEST_BYTES
            else:
                _asset_path(name)
                size_limit = material_upload.MAX_IMAGE_MATERIAL_UPLOAD_BYTES
            if info.file_size > size_limit or info.file_size < 0:
                raise StoryboardProjectError("ZIP member is too large")
            total += info.file_size
            if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
                raise StoryboardProjectError("uncompressed ZIP content exceeds 100 MB")
            if info.file_size > 1000 * max(1, info.compress_size):
                raise StoryboardProjectError("suspicious ZIP compression ratio")
        if "storyboard.json" not in {info.filename for info in infos}:
            raise StoryboardProjectError("storyboard.json is missing")

        try:
            raw_json = container.read("storyboard.json")
            manifest = json.loads(raw_json.decode("utf-8"))
        except (ValueError, UnicodeError, RuntimeError, zipfile.BadZipFile) as exc:
            raise StoryboardProjectError("invalid storyboard.json") from exc

        actual_names = {info.filename for info in infos if not info.is_dir()}
        project = _validate_manifest(manifest, actual_names)

        saved = []
        filenames = {}
        try:
            for scene in project["scenes"]:
                if scene["type"] != "local":
                    continue
                archive_name = scene["file"]
                if archive_name not in filenames:
                    content = container.read(archive_name)
                    stored = material_upload.save_material_upload(
                        archive_name.split("/")[-1], io.BytesIO(content)
                    )
                    saved.append(stored)
                    filenames[archive_name] = stored
                scene["file"] = filenames[archive_name]
        except Exception as exc:
            directory = material_upload.uploaded_material_dir(create=True)
            for name in saved:
                try:
                    os.unlink(os.path.join(directory, name))
                except OSError:
                    pass
            if isinstance(exc, StoryboardProjectError):
                raise
            raise StoryboardProjectError(f"could not import project asset: {exc}") from exc
    return project


def export_project(title: str, video_aspect: str, voice: str, scenes: list[dict]) -> bytes:
    """Export editor scenes with validated local images into a portable ZIP."""
    if not isinstance(scenes, list) or not 1 <= len(scenes) <= MAX_SCENES:
        raise StoryboardProjectError("a project requires 1-50 scenes")
    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "title": _string(title, "title", 200),
        "video_aspect": video_aspect,
        "voice": _string(voice, "voice", 150),
        "scenes": [],
    }
    if video_aspect not in ("16:9", "9:16", "1:1"):
        raise StoryboardProjectError("invalid video aspect")
    asset_payloads = []
    material_dir = material_upload.uploaded_material_dir(create=True)
    total = 0
    for index, entry in enumerate(scenes, start=1):
        scene_type = entry.get("type")
        prompt = _string(entry.get("prompt", ""), "prompt", 2000)
        narration = _string(entry.get("voiceover", ""), "voiceover", 5000)
        duration = ("auto" if entry.get("duration_mode") == "auto"
                    else entry.get("duration", 5))
        _duration(duration)
        scene = {
            "id": f"scene-{index:02d}",
            "type": scene_type,
            "voiceover": narration,
            "duration": duration,
        }
        if scene_type == "generated_image":
            if not prompt.strip():
                raise StoryboardProjectError(f"scene {index} needs a prompt")
            scene["prompt"] = prompt
        elif scene_type == "local":
            original = entry.get("file", "")
            if not isinstance(original, str) or not original:
                raise StoryboardProjectError(f"scene {index} needs an image")
            if Path(original).suffix.lower() not in IMAGE_EXTENSIONS:
                raise StoryboardProjectError(f"scene {index} has unsupported image type")
            try:
                safe_path = file_security.resolve_path_within_directory(material_dir, original)
            except ValueError as exc:
                raise StoryboardProjectError("local asset is outside uploads directory") from exc
            if not os.path.isfile(safe_path):
                raise StoryboardProjectError(f"scene {index} image no longer exists")
            size = os.path.getsize(safe_path)
            if size > material_upload.MAX_IMAGE_MATERIAL_UPLOAD_BYTES:
                raise StoryboardProjectError("scene image exceeds upload limit")
            total += size
            if total > MAX_ARCHIVE_BYTES:
                raise StoryboardProjectError("project assets exceed export limit")
            archive_name = f"assets/scene-{index:02d}{Path(safe_path).suffix.lower()}"
            scene["file"] = archive_name
            with open(safe_path, "rb") as asset:
                asset_payloads.append((archive_name, asset.read()))
        else:
            raise StoryboardProjectError(f"scene {index} has an unsupported type")
        manifest["scenes"].append(scene)
    blob = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    if len(blob) > MAX_MANIFEST_BYTES:
        raise StoryboardProjectError("storyboard.json is too large")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as container:
        container.writestr("storyboard.json", blob)
        for name, payload in asset_payloads:
            container.writestr(name, payload)
    if stream.tell() > MAX_ARCHIVE_BYTES:
        raise StoryboardProjectError("export ZIP exceeds 60 MB")
    return stream.getvalue()
