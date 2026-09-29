"""Validated, non-secret settings for the main and low-level CLIs."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .models import Budget, OutputSpec, StrictModel


class ModelSettings(StrictModel):
    name: str = Field(min_length=1)
    timeout_seconds: float = Field(default=120, gt=0)
    max_retries: int = Field(default=0, ge=0, le=10)
    max_output_tokens_per_call: int = Field(default=12000, ge=1)


class ImageGenerationSettings(StrictModel):
    enabled: bool = False
    workflow: Literal["planned_assets", "code_first_repair", "code_directed"] = "planned_assets"
    max_repair_area_fraction: float = Field(default=0.6, gt=0, le=1)
    conservative_planning: bool = False
    max_generated_components: int = Field(default=1, ge=0, le=16)
    cutout_default_tolerance: int = Field(default=40, ge=10, le=100)
    cutout_default_feather_px: float = Field(default=0.8, ge=0, le=4)
    provider: Literal["qwen", "openai"] = "qwen"
    model: str = "qwen-image"
    api_key_env: str = Field(default="DASHSCOPE_API_KEY", pattern=r"^[A-Z][A-Z0-9_]*$")
    base_url: str = "https://dashscope.aliyuncs.com/api/v1"
    size: Literal["1664*928", "928*1664", "1328*1328", "1472*1140", "1140*1472"] = "1328*1328"
    prompt_extend: bool = True
    timeout_seconds: float = Field(default=180, gt=0, le=1800)
    poll_interval_seconds: float = Field(default=2, gt=0, le=30)

    @model_validator(mode="after")
    def endpoint(self):
        from urllib.parse import urlsplit

        if self.provider == "qwen" and self.model not in {"qwen-image", "qwen-image-plus"}:
            raise ValueError("This Qwen async adapter supports qwen-image and qwen-image-plus")
        url = urlsplit(self.base_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Image API base_url must be an HTTPS URL without credentials/query/fragment")
        return self


class TaskDefaults(StrictModel):
    # Retained for older harness.json files that predate image/video profiles.
    output: OutputSpec = Field(default_factory=lambda: OutputSpec(width=512, height=512, format="png"))
    allowed_backends: list[Literal["scene2d", "canvas", "svg", "svg_animation", "three", "paint", "pathtrace"]] = Field(
        default_factory=lambda: ["scene2d", "canvas", "svg"], min_length=1
    )
    allow_generated_assets: bool = False


class TaskProfile(StrictModel):
    prompt: str = Field(min_length=1)
    output: OutputSpec
    allowed_backends: list[Literal["scene2d", "canvas", "svg", "svg_animation", "three", "paint", "pathtrace"]] = Field(min_length=1)
    allow_generated_assets: bool = False


class EfficiencySettings(StrictModel):
    max_batch_steps: int = Field(default=16, ge=1, le=16)
    recent_image_messages: int = Field(default=4, ge=1, le=16)
    max_image_replay_turns: int = Field(default=1, ge=0, le=8)


class HarnessSettings(StrictModel):
    mode: Literal["maliang", "generic-agent", "single-shot"] = "maliang"
    model: ModelSettings
    budget: Budget = Field(default_factory=Budget)
    image_generation: ImageGenerationSettings = Field(default_factory=ImageGenerationSettings)
    efficiency: EfficiencySettings = Field(default_factory=EfficiencySettings)
    defaults: TaskDefaults = Field(default_factory=TaskDefaults)
    image: TaskProfile | None = None
    video: TaskProfile | None = None
    paint: TaskProfile | None = None
    pathtrace: TaskProfile | None = None

    @model_validator(mode="after")
    def profile_formats(self):
        for kind, expected in (("image", "png"), ("video", "mp4"), ("paint", "png"), ("pathtrace", "png")):
            profile = getattr(self, kind)
            if profile is not None and profile.output.format != expected:
                raise ValueError(f"{kind}.output.format must be {expected}")
        return self

    def profile(self, kind: Literal["image", "video", "paint", "pathtrace"]) -> TaskProfile:
        configured = getattr(self, kind)
        if configured is not None:
            return configured
        if kind == "pathtrace":
            return TaskProfile(
                prompt="创作写实静物：暖灰桌面上的陶瓷碗、金属球与玻璃杯，柔和侧光，真实材质与接触阴影。",
                output=OutputSpec(width=768, height=768, format="png"),
                allowed_backends=["pathtrace"], allow_generated_assets=False,
            )
        if kind == "paint":
            return TaskProfile(
                prompt="用真实笔刷绘制一幅静物画，从大色块到明暗与细节逐步完善。",
                output=self.profile("image").output.model_copy(update={"format": "png"}),
                allowed_backends=["paint"], allow_generated_assets=False,
            )
        # An older config with only defaults.output remains runnable.
        output = self.defaults.output.model_dump()
        output["format"] = "png" if kind == "image" else "mp4"
        return TaskProfile(
            prompt="画一张米白背景、橙色太阳与深蓝山峰的几何海报，构图简洁、留白充分。"
            if kind == "image"
            else "创作一段三秒二维动画：米白背景，橙色太阳从左向右缓慢移动。",
            output=OutputSpec.model_validate(output),
            allowed_backends=self.defaults.allowed_backends,
            allow_generated_assets=self.defaults.allow_generated_assets,
        )


def load_harness_settings(path: str | Path) -> HarnessSettings:
    config_path = Path(path)
    if not config_path.is_file():
        raise ValueError(f"Harness 配置文件不存在：{config_path}")
    try:
        return HarnessSettings.model_validate_json(config_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"Harness 配置文件无效：{config_path} ({exc})") from exc
