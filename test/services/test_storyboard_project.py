"""Portable Storyboard project tests: security, round-trip and error handling."""

import io
import json
import os
import stat
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from app.services import storyboard_project
from app.utils import utils


def png_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (640, 480), "navy").save(buffer, format="PNG")
    return buffer.getvalue()


def manifest(scenes=None):
    return {
        "format": storyboard_project.FORMAT,
        "version": 1,
        "title": "Taxes AI",
        "video_aspect": "16:9",
        "voice": "en-IE-EmilyNeural",
        "scenes": scenes or [
            {
                "type": "generated_image",
                "prompt": "An Irish accountant in the office",
                "voiceover": "Welcome to Taxes AI.",
                "duration": "auto",
            },
            {
                "type": "local",
                "file": "assets/homepage.png",
                "voiceover": "Here is our dashboard.",
                "duration": 7,
            },
        ],
    }


def zip_bytes(document, assets=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("storyboard.json", json.dumps(document).encode("utf-8"))
        for path, payload in (assets or {}).items():
            archive.writestr(path, payload)
    return stream.getvalue()


def test_import_export_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(utils, "storage_dir", lambda *args, **kwargs: str(tmp_path))
    source = zip_bytes(manifest(), {"assets/homepage.png": png_bytes()})
    imported = storyboard_project.import_project(source)
    assert imported["title"] == "Taxes AI"
    assert imported["voice"] == "en-IE-EmilyNeural"
    assert imported["video_aspect"] == "16:9"
    assert [s["type"] for s in imported["scenes"]] == ["generated_image", "local"]
    assert imported["scenes"][0]["duration_mode"] == "auto"
    assert imported["scenes"][0]["voiceover"] == "Welcome to Taxes AI."
    local_file = imported["scenes"][1]["file"]
    assert Path(local_file).name == local_file
    assert (tmp_path / local_file).exists()
    output = storyboard_project.export_project(
        imported["title"], imported["video_aspect"], imported["voice"], imported["scenes"]
    )
    with zipfile.ZipFile(io.BytesIO(output)) as z:
        saved = json.loads(z.read("storyboard.json"))
        assert saved["scenes"][0]["duration"] == "auto"
        assert saved["scenes"][1]["file"] == "assets/scene-02.png"
        assert z.read("assets/scene-02.png") == png_bytes()
    imported_again = storyboard_project.import_project(output)
    assert [s["voiceover"] for s in imported_again["scenes"]] == [
        "Welcome to Taxes AI.", "Here is our dashboard."
    ]


@pytest.mark.parametrize(
    "bad_path",
    ["../secret.png", "assets/../secret.png", "assets/a/b.png",
     "/tmp/secret.png", "https://example.com/pic.png", "C:\\secrets.png",
     "assets/a.mp4"],
)
def test_rejects_unsafe_or_unsupported_asset_references(bad_path):
    doc = manifest([{"type": "local", "file": bad_path, "duration": 5}])
    archive = zip_bytes(doc, {bad_path: b"fake"})
    with pytest.raises(storyboard_project.StoryboardProjectError):
        storyboard_project.import_project(archive)


def test_import_rejects_missing_file():
    with pytest.raises(storyboard_project.StoryboardProjectError, match="missing asset"):
        storyboard_project.import_project(zip_bytes(manifest()))


def test_import_rejects_invalid_image(tmp_path, monkeypatch):
    monkeypatch.setattr(utils, "storage_dir", lambda *args, **kwargs: str(tmp_path))
    with pytest.raises(storyboard_project.StoryboardProjectError, match="import project asset"):
        storyboard_project.import_project(
            zip_bytes(manifest(), {"assets/homepage.png": b"not an image"})
        )
    assert not list(tmp_path.iterdir())


def test_invalid_second_image_rolls_back_first(tmp_path, monkeypatch):
    monkeypatch.setattr(utils, "storage_dir", lambda *args, **kwargs: str(tmp_path))
    doc = manifest([
        {"type": "local", "file": "assets/one.png", "duration": 5},
        {"type": "local", "file": "assets/two.png", "duration": 6},
    ])
    archive = zip_bytes(
        doc, {"assets/one.png": png_bytes(), "assets/two.png": b"bad"}
    )
    with pytest.raises(storyboard_project.StoryboardProjectError):
        storyboard_project.import_project(archive)
    assert not list(tmp_path.iterdir())


def test_duplicate_case_insensitive_entries_rejected():
    archive = zip_bytes(manifest(), {
        "assets/homepage.png": png_bytes(),
        "assets/HOMEPAGE.PNG": png_bytes(),
    })
    with pytest.raises(storyboard_project.StoryboardProjectError, match="duplicate"):
        storyboard_project.import_project(archive)


def test_archive_rejects_symlink():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("storyboard.json", json.dumps(manifest()))
        info = zipfile.ZipInfo("assets/homepage.png")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(info, "elsewhere.png")
    with pytest.raises(storyboard_project.StoryboardProjectError, match="symlinks"):
        storyboard_project.import_project(out.getvalue())


@pytest.mark.parametrize("duration", [0, 31, -1, 3.5, None])
def test_rejects_invalid_duration(duration):
    doc = manifest([{
        "type": "generated_image", "prompt": "Hello", "duration": duration
    }])
    with pytest.raises(storyboard_project.StoryboardProjectError, match="duration"):
        storyboard_project.import_project(zip_bytes(doc))


def test_import_rejects_zip_size_limit(monkeypatch):
    monkeypatch.setattr(storyboard_project, "MAX_ARCHIVE_BYTES", 20)
    with pytest.raises(storyboard_project.StoryboardProjectError, match="60 MB"):
        storyboard_project.import_project(zip_bytes(manifest()))


def test_export_rejects_local_files_outside_upload_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(utils, "storage_dir", lambda *args, **kwargs: str(tmp_path))
    outside = tmp_path.parent / "private.png"
    with pytest.raises(storyboard_project.StoryboardProjectError, match="outside uploads"):
        storyboard_project.export_project(
            "Taxes AI", "16:9", "", [
                {"type": "local", "file": str(outside), "duration": 5}
            ]
        )


def test_import_rejects_unexpected_audio_file():
    archive = zip_bytes(manifest([{
        "type": "generated_image", "prompt": "Hello", "duration": 5
    }]), {"audio/speech.mp3": b"hello"})
    with pytest.raises(storyboard_project.StoryboardProjectError):
        storyboard_project.import_project(archive)
