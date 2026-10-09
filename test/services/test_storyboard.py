"""Unit tests for the mixed storyboard material builder (no paid API calls)."""

from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.models.schema import MaterialInfo, StoryboardScene, VideoParams
from app.services import storyboard


def make_params(*scenes, clip_duration=5):
    return VideoParams(
        video_subject="Taxes AI",
        video_source="storyboard",
        video_clip_duration=clip_duration,
        storyboard=list(scenes),
    )


@pytest.mark.parametrize(
    "data",
    [
        {"type": "generated_image", "prompt": "  "},
        {"type": "local", "file": ""},
        {"type": "local", "file": "homepage.png", "duration": 0},
        {"type": "local", "file": "homepage.png", "duration": 31},
        {"type": "unexpected", "file": "homepage.png"},
    ],
)
def test_rejects_invalid_scenes(data):
    with pytest.raises(ValidationError):
        StoryboardScene(**data)


def test_requires_at_least_one_scene():
    with pytest.raises(storyboard.StoryboardError, match="at least one"):
        storyboard.build_storyboard_materials("task-1", make_params())


def test_mixed_scenes_preserve_explicit_order_and_duration():
    params = make_params(
        StoryboardScene(type="generated_image", prompt="Irish taxpayer", duration=4),
        StoryboardScene(type="local", file="dashboard.png", duration=8),
        StoryboardScene(type="generated_image", prompt="An accountant", duration=6),
    )
    generated_items = [
        [MaterialInfo(provider="openai_image", url="/task/a.png", duration=4)],
        [MaterialInfo(provider="openai_image", url="/task/b.png", duration=6)],
    ]
    processed_local = [MaterialInfo(provider="local", url="/task/dashboard.mp4", duration=8)]
    with (
        patch.object(storyboard.material, "is_openai_image_enabled", return_value=True),
        patch.object(storyboard.material, "generate_images_openai", side_effect=generated_items) as generate,
        patch.object(storyboard.video, "preprocess_video", return_value=processed_local) as preprocess,
        patch.object(storyboard.video, "render_image_zoom_video", side_effect=["/task/a.mp4", "/task/b.mp4"]) as render,
        patch.object(storyboard.utils, "task_dir", return_value="/task"),
    ):
        actual = storyboard.build_storyboard_materials("task-1", params)

    assert actual == ["/task/a.mp4", "/task/dashboard.mp4", "/task/b.mp4"]
    assert [call.kwargs["minimum_duration"] for call in generate.call_args_list] == [4, 6]
    assert [call.args[1] for call in render.call_args_list] == [4, 6]
    assert preprocess.call_args.kwargs["clip_duration"] == 8
    assert preprocess.call_args.kwargs["materials"][0].url == "/task/dashboard.mp4"


def test_default_duration_is_video_clip_duration():
    params = make_params(StoryboardScene(type="local", file="homepage.jpg"), clip_duration=7)
    with patch.object(
        storyboard.video,
        "preprocess_video",
        return_value=[MaterialInfo(provider="local", url="/task/homepage.mp4")],
    ) as preprocess:
        actual = storyboard.build_storyboard_materials("task-1", params)
    assert actual == ["/task/homepage.mp4"]
    assert preprocess.call_args.kwargs["clip_duration"] == 7


@pytest.mark.parametrize("filename", ["clip.mp4", "../private.txt", "x.gif"])
def test_local_rejects_unsupported_types_before_processing(filename):
    params = make_params(StoryboardScene(type="local", file=filename))
    with patch.object(storyboard.video, "preprocess_video") as preprocess:
        with pytest.raises(storyboard.StoryboardError, match="JPG or PNG"):
            storyboard.build_storyboard_materials("task-1", params)
    preprocess.assert_not_called()


def test_local_invalid_or_missing_image_fails():
    params = make_params(StoryboardScene(type="local", file="missing.png"))
    with patch.object(storyboard.video, "preprocess_video", return_value=[]):
        with pytest.raises(storyboard.StoryboardError, match="missing, unsafe"):
            storyboard.build_storyboard_materials("task-1", params)


def test_generated_scene_requires_config():
    params = make_params(StoryboardScene(type="generated_image", prompt="a taxpayer"))
    with patch.object(storyboard.material, "is_openai_image_enabled", return_value=False):
        with pytest.raises(storyboard.StoryboardError, match="openai_image_base_url"):
            storyboard.build_storyboard_materials("task-1", params)


def test_generated_scene_empty_response_stops():
    params = make_params(StoryboardScene(type="generated_image", prompt="a taxpayer"))
    with (
        patch.object(storyboard.material, "is_openai_image_enabled", return_value=True),
        patch.object(storyboard.material, "generate_images_openai", return_value=[]),
        patch.object(storyboard.utils, "task_dir", return_value="/task"),
    ):
        with pytest.raises(storyboard.StoryboardError, match="no usable image"):
            storyboard.build_storyboard_materials("task-1", params)


def test_generated_scene_render_failure_stops_before_following_scene():
    params = make_params(
        StoryboardScene(type="generated_image", prompt="a taxpayer"),
        StoryboardScene(type="local", file="homepage.png"),
    )
    with (
        patch.object(storyboard.material, "is_openai_image_enabled", return_value=True),
        patch.object(
            storyboard.material,
            "generate_images_openai",
            return_value=[MaterialInfo(provider="openai_image", url="/task/a.png")],
        ),
        patch.object(storyboard.video, "render_image_zoom_video", side_effect=RuntimeError("encode error")),
        patch.object(storyboard.video, "preprocess_video") as preprocess,
        patch.object(storyboard.utils, "task_dir", return_value="/task"),
    ):
        with pytest.raises(storyboard.StoryboardError, match="could not be rendered"):
            storyboard.build_storyboard_materials("task-1", params)
    preprocess.assert_not_called()
