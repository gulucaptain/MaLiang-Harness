"""Validated inputs for the public asset capabilities."""
from pydantic import Field

from ...models import AssetTask, StrictModel


class ImportAsset(StrictModel):
    expected_revision: int = Field(ge=0)
    asset_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    relative_path: str


class GenerateAsset(StrictModel):
    expected_revision: int = Field(ge=0)
    asset_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")


class PlanAsset(StrictModel):
    expected_revision: int = Field(ge=0)
    task: AssetTask


class InspectAsset(StrictModel):
    asset_id: str


class InspectAssets(StrictModel):
    asset_ids: list[str] = Field(min_length=1, max_length=8)


class ExtractWhiteBackground(StrictModel):
    expected_revision: int = Field(ge=0)
    source_asset_id: str
    asset_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    white_threshold: int = Field(default=238, ge=225, le=252)


class ExtractSubject(StrictModel):
    expected_revision: int = Field(ge=0)
    source_asset_id: str
    asset_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    method: str = Field(pattern="^(border_color|polygon|hybrid)$")
    tolerance: int | None = Field(default=None, ge=10, le=100)
    polygon: list[list[int]] | None = Field(default=None, min_length=3, max_length=128)
    protect_regions: list[list[int]] = Field(default_factory=list, max_length=8)
    feather_px: float | None = Field(default=None, ge=0, le=4)


class PreviewCutout(StrictModel):
    source_asset_id: str
    cutout_asset_id: str


class PlaceCutout(StrictModel):
    expected_revision: int = Field(ge=0)
    source_asset_id: str
    cutout_asset_id: str


class ComposeAsset(StrictModel):
    expected_revision: int = Field(ge=0)
    source_asset_id: str
    asset_id: str
    source: str = Field(min_length=1, max_length=100000)


class PlaceBackground(StrictModel):
    expected_revision: int = Field(ge=0)
    asset_id: str
    observed_anchors: dict[str, list[float]] = Field(default_factory=dict)
    layout_notes: str = Field(min_length=10, max_length=2000)


