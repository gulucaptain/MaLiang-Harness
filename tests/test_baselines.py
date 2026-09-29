import json

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from PIL import Image

from maliang.agent import make_context, run_agent, run_single_shot
from maliang.models import Artwork, OutputSpec
from maliang.store import ProjectStore


class SolidRenderer:
    name, suffix, supports_animation, description = "solid", ".json", False, "JSON color"

    def validate_source(self, source):
        json.loads(source)

    def frames(self, artwork, source, times, output, assets):
        for i, _ in enumerate(times):
            Image.new("RGB", (64, 64), json.loads(source)["color"]).save(output / f"{i:06d}.png")


class BaselineModel(BaseChatModel):
    mode: str

    @property
    def _llm_type(self):
        return "baseline-fixture"

    def bind_tools(self, tools, **kwargs):
        assert "finalize_artwork" not in {t.name if hasattr(t, "name") else t["name"] for t in tools}
        return self

    def get_num_tokens_from_messages(self, messages, tools=None):
        return 100

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        program = {"backend": "solid", "source": '{"color":"green"}'}
        if self.mode == "single-shot":
            response = AIMessage(content=json.dumps(program))
        else:
            count = sum(isinstance(m, AIMessage) and bool(m.tool_calls) for m in messages)
            calls = [("write_program", {"expected_revision": 0, **program}), ("export_artifact", {})]
            if count < len(calls):
                name, args = calls[count]
                response = AIMessage(content="", tool_calls=[{"id": str(count), "name": name, "args": args}])
            else:
                response = AIMessage(content="Exported")
        return ChatResult(generations=[ChatGeneration(message=response)])


@pytest.mark.parametrize("mode", ["single-shot", "generic-agent"])
def test_baselines_export_without_claiming_quality_success(tmp_path, mode):
    store = ProjectStore(tmp_path)
    store.create(
        Artwork(
            prompt="Green poster",
            spec=OutputSpec(width=64, height=64, format="png"),
            allowed_backends=["solid"],
            requirements=[{"id": "quality", "description": "A beautiful poster"}],
        )
    )
    ctx = make_context(tmp_path, mode=mode, plugins=False)
    ctx.renderers.register(SolidRenderer())
    model = BaselineModel(mode=mode)
    status = run_single_shot(ctx, model) if mode == "single-shot" else run_agent(ctx, model)
    assert status["status"] == "baseline_exported"
    assert store.path(store.evidence(status["export_id"]).paths[0]).exists()
    report = json.loads(store.path("validation.json").read_text())
    assert not report["eligible_to_finalize"]
    assert report["requirements"][0]["verdict"] == "unreviewed"
    assert ctx.meter.usage["model_calls"] == (1 if mode == "single-shot" else 3)
