"""Deterministic integration fixture, not an LLM or a generative quality benchmark."""

from pathlib import Path
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from .store import ProjectStore


class ScriptedDemoModel(BaseChatModel):
    project_root: str
    model_name: str = "maliang:scripted-demo"

    @property
    def _llm_type(self):
        return "maliang-scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def get_num_tokens_from_messages(self, messages, tools=None):
        return sum(len(str(m.content)) // 4 for m in messages)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any):
        store = ProjectStore(self.project_root)
        art = store.load()
        step = sum(1 for m in messages if isinstance(m, AIMessage) and m.tool_calls)
        source = Path(__file__).with_name("demo_program.js").read_text()
        calls = [
            ("list_capabilities", {}),
            (
                "update_artwork",
                {
                    "expected_revision": art.revision,
                    "plan": ["Draw", "Inspect", "Export"],
                    "objects": [{"id": "pond", "layer": 0, "properties": {}, "asset_ids": []}],
                    "events": [],
                },
            ),
            (
                "write_program",
                {"expected_revision": art.revision, "backend": "canvas", "source": "function( {"},
            ),
            ("render_frames", {"timestamps": [0]}),
            ("write_program", {"expected_revision": art.revision, "backend": "canvas", "source": source}),
            ("render_frames", {"timestamps": [0, art.spec.duration / 2]}),
            ("export_artifact", {}),
        ]
        if step < len(calls):
            name, args = calls[step]
        elif step == len(calls):
            import json

            exports = [json.loads(p.read_text()) for p in store.root.glob("evidence/*.json")]
            current = [e for e in exports if e["kind"] == "export" and e["revision"] == art.revision]
            if not current:
                raise RuntimeError("Demo export failed; inspect trace.jsonl")
            name, args = "finalize_artwork", {"export_id": current[-1]["id"]}
        else:
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="Scripted offline integration demonstration finished. No live LLM was called."
                        )
                    )
                ]
            )
        message = AIMessage(content="", tool_calls=[{"id": f"demo-{step}", "name": name, "args": args}])
        return ChatResult(generations=[ChatGeneration(message=message)])
