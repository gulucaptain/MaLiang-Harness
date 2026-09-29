"""Offline fixture through the real Deep Agents loop; not an LLM quality evaluation."""

from __future__ import annotations

import json

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from .store import ProjectStore

BACKGROUND = """function(ctx,t,o){
const g=ctx.createLinearGradient(0,0,0,360);g.addColorStop(0,'#182f4f');g.addColorStop(1,'#f2c68c');
ctx.fillStyle=g;ctx.fillRect(0,0,640,360);
}"""
MOUNTAINS = """function(ctx,t,o){
ctx.fillStyle='#425e6c';ctx.beginPath();ctx.moveTo(0,260);ctx.lineTo(145,135);ctx.lineTo(300,268);ctx.lineTo(465,148);ctx.lineTo(640,270);ctx.lineTo(640,360);ctx.lineTo(0,360);ctx.fill();
ctx.fillStyle='#233d50';ctx.beginPath();ctx.moveTo(0,305);ctx.lineTo(120,240);ctx.lineTo(255,320);ctx.lineTo(370,210);ctx.lineTo(560,315);ctx.lineTo(640,278);ctx.lineTo(640,360);ctx.lineTo(0,360);ctx.fill();
ctx.fillStyle='#f6e9d0';ctx.font='14px sans-serif';ctx.fillText('MALIANG  /  CODE-DRIVEN MOTION',28,333);
}"""
SUN = """function(ctx,t,o){
const r=o.properties.radius;
const halo=ctx.createRadialGradient(0,0,r*.4,0,0,r*1.8);halo.addColorStop(0,'rgba(255,211,126,.45)');halo.addColorStop(1,'rgba(255,211,126,0)');
ctx.fillStyle=halo;ctx.fillRect(-r*2,-r*2,r*4,r*4);
ctx.fillStyle=o.properties.color;ctx.beginPath();ctx.arc(0,0,r,0,2*Math.PI);ctx.fill();
}"""


class SceneDemoModel(BaseChatModel):
    project_root: str
    model_name: str = "maliang:scene-demo"

    @property
    def _llm_type(self):
        return "offline-scene-fixture"

    def bind_tools(self, tools, **kwargs):
        return self

    def get_num_tokens_from_messages(self, messages, tools=None):
        return 500

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        store = ProjectStore(self.project_root)
        art = store.load()
        step = sum(isinstance(m, AIMessage) and bool(m.tool_calls) for m in messages)
        calls = [
            ("describe_environment", {}),
            (
                "plan_creation",
                {
                    "expected_revision": art.revision,
                    "backend": "scene2d",
                    "capability_assessment": "A 2D geometric scene and keyframes meet this fixture. No generated assets or video API are needed.",
                    "plan": [
                        "Create background, mountains and sun",
                        "Animate sun independently",
                        "Observe composition and motion",
                        "Review and export",
                    ],
                    "requirements": [
                        {
                            "id": "composition",
                            "description": "Orange sun is visible",
                            "observation": {"object_id": "sun"},
                        },
                        {
                            "id": "motion",
                            "description": "Sun travels horizontally and returns while mountains stay fixed",
                            "kind": "temporal",
                            "observation": {"start": 0, "end": 3, "samples": 3},
                        },
                    ],
                    "checkpoints": [
                        {
                            "id": "delivery",
                            "description": "Composition and motion checks",
                            "requirement_ids": ["brief", "composition", "motion"],
                        }
                    ],
                },
            ),
            (
                "put_object",
                {
                    "expected_revision": art.revision,
                    "object_id": "background",
                    "source": BACKGROUND,
                    "layer": 0,
                },
            ),
            (
                "put_object",
                {
                    "expected_revision": art.revision,
                    "object_id": "mountains",
                    "source": MOUNTAINS,
                    "layer": 2,
                },
            ),
            (
                "put_object",
                {
                    "expected_revision": art.revision,
                    "object_id": "sun",
                    "source": SUN,
                    "layer": 1,
                    "properties": {"radius": 29, "color": "#ffb54d"},
                    "transform": {"x": 180, "y": 100},
                },
            ),
            (
                "animate_object",
                {
                    "expected_revision": art.revision,
                    "object_id": "sun",
                    "reason": "Show parameter-driven motion with stable background",
                    "tracks": [
                        {
                            "property": "x",
                            "interpolation": "smooth",
                            "keyframes": [
                                {"time": 0, "value": 180},
                                {"time": 1.5, "value": 450},
                                {"time": 3, "value": 180},
                            ],
                        }
                    ],
                },
            ),
        ]
        if step < len(calls):
            name, args = calls[step]
        elif step < 12:
            requirement = ["brief", "composition", "motion"][(step - 6) // 2]
            if step % 2 == 0:
                name, args = "observe_requirement", {"requirement_id": requirement}
            else:
                evidence = [json.loads(p.read_text()) for p in store.root.glob("evidence/*.json")]
                evidence = [
                    e
                    for e in evidence
                    if e["revision"] == art.revision and e["metadata"].get("requirement_id") == requirement
                ]
                if not evidence:
                    raise RuntimeError("Fixture observation failed; inspect trace")
                name, args = (
                    "record_review",
                    {
                        "requirement_id": requirement,
                        "evidence_ids": [evidence[-1]["id"]],
                        "verdict": "pass",
                        "explanation": "Offline scripted fixture review, not an independent model quality judgment. Geometry and motion contracts are checked separately by pixel/video tests.",
                    },
                )
        elif step == 12:
            name, args = "complete_checkpoint", {"checkpoint_id": "delivery"}
        elif step == 13:
            name, args = "export_artifact", {}
        elif step == 14:
            from .verification import latest_export

            name, args = "finalize_artwork", {"export_id": latest_export(store)}
        else:
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(content="Offline scene-control fixture finished. No LLM API used.")
                    )
                ]
            )
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="", tool_calls=[{"id": f"scene-{step}", "name": name, "args": args}]
                    )
                )
            ]
        )
