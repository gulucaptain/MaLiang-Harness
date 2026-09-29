import pytest

from maliang.generation_policy import validate_generation_plan
from maliang.settings import ImageGenerationSettings


def component(**changes):
    value = dict(
        object_id="cat",
        route="generated_subject",
        brief_evidence="photorealistic fur",
        code_limitation="Procedural paths do not capture the requested individual fur strands.",
        minimal_scope="Only the isolated cat; window, framing and lighting stay in code.",
    )
    return value | changes


def test_conservative_plan_allows_zero_generation_and_justified_exception():
    cfg = ImageGenerationSettings(conservative_planning=True)
    validate_generation_plan(cfg, "A cute cartoon kitchen", [{"route": "code"}])
    validate_generation_plan(cfg, "A cat with photorealistic fur", [component()])


def test_rejects_invented_quality_requirement_and_missing_rationale():
    cfg = ImageGenerationSettings(conservative_planning=True)
    with pytest.raises(ValueError, match="immutable user brief"):
        validate_generation_plan(cfg, "A cute cartoon cat", [component()])
    with pytest.raises(ValueError, match="specific code limitation"):
        validate_generation_plan(cfg, "A cat with photorealistic fur", [component(code_limitation="")])


def test_component_limit_is_a_ceiling_and_zero_disables_planned_generation():
    cfg = ImageGenerationSettings(conservative_planning=True, max_generated_components=1)
    with pytest.raises(ValueError, match="too many"):
        validate_generation_plan(cfg, "photorealistic fur", [component(), component(object_id="dog")])
    cfg.max_generated_components = 0
    validate_generation_plan(cfg, "photorealistic fur", [{"route": "code"}])
    with pytest.raises(ValueError, match="too many"):
        validate_generation_plan(cfg, "photorealistic fur", [component()])
