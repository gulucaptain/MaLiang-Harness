"""Register asset capabilities; implementation is split by responsibility.

The public import path and tool names remain unchanged.
"""
import os

from ...runtime import Capability
from ...settings import ImageGenerationSettings
from .extraction import AssetExtraction
from .generation import AssetGeneration
from .geometry import background_geometry as background_geometry
from .inspection import AssetInspection
from .placement import AssetPlacement
from .planning import AssetPlanning
from .schemas import ComposeAsset as ComposeAsset
from .schemas import ExtractSubject as ExtractSubject
from .schemas import ExtractWhiteBackground as ExtractWhiteBackground
from .schemas import GenerateAsset as GenerateAsset
from .schemas import ImportAsset as ImportAsset
from .schemas import InspectAsset as InspectAsset
from .schemas import InspectAssets as InspectAssets
from .schemas import PlaceBackground as PlaceBackground
from .schemas import PlaceCutout as PlaceCutout
from .schemas import PlanAsset as PlanAsset
from .schemas import PreviewCutout as PreviewCutout
from .storage import AssetStorage


class AssetService(AssetStorage, AssetPlanning, AssetInspection, AssetExtraction, AssetPlacement, AssetGeneration):
    """Shared task state for storage, planning, inspection and composition."""

    def __init__(self, context):
        self.context = context
        self.store = context.store
        self.registry = context.registry
        if context.image_generation is None:
            model = os.environ.get("MALIANG_IMAGE_MODEL")
            context.image_generation = ImageGenerationSettings(
                enabled=bool(model), provider="openai", model=model or "unused",
                api_key_env="OPENAI_API_KEY",
            )
        self.config = context.image_generation


def install(context):
    """Install the same public tools, with provider tools gated by credentials."""
    service = AssetService(context)
    store, registry, cfg = service.store, service.registry, service.config
    registry.register(
        Capability(
            "import_asset",
            "Import a staged project PNG/JPEG as a reusable immutable image asset.",
            ImportAsset,
            service.import_asset,
            "assets",
            True,
        )
    )

    registry.register(
        Capability(
            "inspect_assets",
            "Inspect up to eight assets together in one model turn, preserving original images and per-asset metadata. Identical images returned once. Each inspection is metered and traced. Not final scene evidence.",
            InspectAssets,
            service.inspect_assets,
            "assets",
        )
    )

    registry.register(
        Capability(
            "inspect_asset",
            "View an asset; not evidence of the final composite.",
            InspectAsset,
            service.inspect_asset,
            "assets",
        )
    )

    registry.register(
        Capability(
            "extract_white_background",
            "Convert a planned white-background subject asset into a transparent PNG. Inspect edge quality.",
            ExtractWhiteBackground,
            service.extract_white,
            "assets",
            True,
        )
    )

    registry.register(
        Capability(
            "extract_subject",
            "Extract a planned generated subject using border colors, an LLM-specified outer contour "
            "(polygon), or hybrid border removal plus an INTERIOR protection polygon/rectangles. "
            "Coordinates are pixels in the original source image. Use a new output ID to retry "
            "from the SAME paid source without another provider call.",
            ExtractSubject,
            service.extract_adaptive,
            "assets",
            True,
        )
    )

    registry.register(
        Capability(
            "preview_cutout",
            "View a cutout over the current code-rendered scene before committing replacement.",
            PreviewCutout,
            service.preview_cutout,
            "assets",
        )
    )

    registry.register(
        Capability(
            "place_cutout",
            "Replace one code-drawn scene2d object with its transparent cutout in the planned region; preserve other layers.",
            PlaceCutout,
            service.place_cutout,
            "assets",
            True,
        )
    )

    registry.register(
        Capability(
            "place_background",
            "Place an inspected environment plate beneath other objects without extraction or distortion. "
            "Supply observed anchors in final canvas pixels after centered cover crop, and assessment of layout. "
            "This is a static plate, not depth geometry. Re-render and review after replacement.",
            PlaceBackground,
            service.place_background,
            "assets",
            True,
        )
    )

    registry.register(
        Capability(
            "compose_asset",
            "Bind a planned transparent subject or texture to its scene2d layer using your own Canvas function(ctx,t,object,assets,random). Source uses local coordinates and only the chosen asset ID. Keep save/restore balanced, do not reset transforms; drawing is clipped to the planned region. Use code for cropping, sizing, blending, lighting and time-dependent effects; then observe.",
            ComposeAsset,
            service.compose_asset,
            "assets",
            True,
        )
    )

    if cfg.enabled and os.environ.get(cfg.api_key_env) and store.load().allow_generated_assets:
        registry.register(
            Capability(
                "plan_asset",
                "Plan a bounded local repair of a code-drawn scene using a CURRENT failed/uncertain "
                "observe_requirement review. Cite evidence, target object/region and extraction mode; "
                "no whole-scene generation."
                if cfg.workflow == "code_first_repair"
                else "Plan a routed subject, texture or generated_background. Backgrounds require full-canvas target_region, extraction none and background_layout (static contents, excluded actors, reserved regions, camera, lighting, anchors). Keep actors code-controlled."
                if cfg.workflow == "code_directed"
                else "Plan a generated component after overall intent decomposition.",
                PlanAsset,
                service.plan_asset,
                "assets",
                True,
            )
        )

        registry.register(
            Capability(
                "generate_asset",
                "Execute a saved asset task; incurs provider cost. "
                "For Qwen timeout retry SAME ID to recover the saved job.",
                GenerateAsset,
                service.generate,
                "assets",
                True,
            )
        )

