from __future__ import annotations

from loguru import logger

from app.models.schema import MaterialInfo, VideoParams
from app.services import material, video
from app.utils import utils


class StoryboardError(ValueError):
    """Raised when an ordered storyboard cannot be converted into video materials."""


def _scene_duration(params: VideoParams, scene) -> int:
    return int(scene.duration or params.video_clip_duration)


def _build_local_scene(params: VideoParams, scene, index: int) -> str:
    """
    Convert one uploaded local image into a normal video material.

    v0.1 intentionally focuses on screenshots, product UI and brand cards.
    """
    duration = _scene_duration(params, scene)

    extension = utils.parse_extension(scene.file).lower()
    if extension not in {"jpg", "jpeg", "png"}:
        raise StoryboardError(
            f"storyboard scene {index}: v0.1 local storyboard scenes must be "
            "JPG or PNG images"
        )

    local_material = MaterialInfo(
        provider="local",
        url=scene.file,
        duration=duration,
    )
    processed = video.preprocess_video(
        materials=[local_material],
        clip_duration=duration,
    )
    if not processed:
        raise StoryboardError(
            f"storyboard scene {index}: local material is missing, unsafe, "
            "unreadable, or below the minimum resolution"
        )

    return processed[0].url


def _build_generated_scene(
    task_id: str,
    params: VideoParams,
    scene,
    index: int,
) -> str:
    duration = _scene_duration(params, scene)

    if not material.is_openai_image_enabled():
        raise StoryboardError(
            "generated storyboard scenes require openai_image_base_url and "
            "openai_image_model in config.toml"
        )

    items = material.generate_images_openai(
        search_term=scene.prompt.strip(),
        minimum_duration=duration,
        video_aspect=params.video_aspect,
        save_dir=utils.task_dir(task_id),
    )
    if not items:
        raise StoryboardError(
            f"storyboard scene {index}: image generation returned no usable image"
        )

    try:
        rendered_path = video.render_image_zoom_video(items[0].url, duration)
    except Exception as exc:
        raise StoryboardError(
            f"storyboard scene {index}: generated image could not be rendered"
        ) from exc

    if not rendered_path:
        raise StoryboardError(
            f"storyboard scene {index}: generated image could not be rendered"
        )
    return rendered_path


def build_storyboard_materials(task_id: str, params: VideoParams) -> list[str]:
    """
    Build ordered local/generated scenes and return normal local video paths.

    Narration, subtitles, music and final MoviePy composition keep using the
    existing MoneyPrinterTurbo pipeline.
    """
    scenes = list(params.storyboard or [])
    if not scenes:
        raise StoryboardError("storyboard video source requires at least one scene")

    paths: list[str] = []
    for index, scene in enumerate(scenes, start=1):
        logger.info(
            "building storyboard scene: "
            f"index={index}, type={scene.type}, "
            f"duration={_scene_duration(params, scene)}s"
        )

        if scene.type == "local":
            paths.append(_build_local_scene(params, scene, index))
        elif scene.type == "generated_image":
            paths.append(_build_generated_scene(task_id, params, scene, index))
        else:
            raise StoryboardError(
                f"storyboard scene {index}: unsupported scene type {scene.type!r}"
            )

    if not paths:
        raise StoryboardError("storyboard did not produce any usable materials")

    return paths
