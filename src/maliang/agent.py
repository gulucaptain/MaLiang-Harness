from __future__ import annotations

import hashlib
import json
import os

from filelock import FileLock

from .adapters import assets
from .adapters.renderers import (
    CanvasRenderer,
    RenderService,
    SceneRenderer,
    SVGAnimationRenderer,
    SVGRenderer,
    ThreeRenderer,
)
from .api_diagnostics import explain_api_error
from .capabilities import Context, install
from .guidance import runtime_capabilities, workflow_progress
from .models import Budget
from .runtime import BudgetExceeded, Meter, Registry
from .store import ProjectStore, atomic_json
from .verification import latest_export, verify


def prepare_image_messages(messages, recent_image_messages, max_replay_turns=None):
    """Keep all text and distinct recent visual evidence; omit exact duplicate image blocks."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    later_turns = {}
    turns = 0
    for index in range(len(messages) - 1, -1, -1):
        later_turns[index] = turns
        if isinstance(messages[index], AIMessage):
            turns += 1
    indices = [
        i
        for i, message in enumerate(messages)
        if isinstance(message, (HumanMessage, ToolMessage))
        and isinstance(message.content, list)
        and any(isinstance(block, dict) and block.get("type") == "image_url" for block in message.content)
    ]
    old = set(indices[:-recent_image_messages])
    result, seen = list(messages), set()
    for i in reversed(indices):
        content, omitted = [], []
        for block in messages[i].content:
            if not isinstance(block, dict) or block.get("type") != "image_url":
                content.append(block)
                continue
            digest = hashlib.sha256(json.dumps(block, sort_keys=True).encode()).hexdigest()
            if i in old or (max_replay_turns is not None and later_turns[i] > max_replay_turns):
                omitted.append(
                    "Previously seen image omitted from this request; all evidence and text remain saved. Re-observe or inspect the saved asset/video if another visual judgment needs it."
                )
            elif digest in seen:
                omitted.append(
                    f"Identical image {digest[:12]} is included in a more recent tool message; use that image with this evidence's metadata."
                )
            else:
                seen.add(digest)
                content.append(block)
        if omitted:
            content.append({"type": "text", "text": " ".join(dict.fromkeys(omitted))})
            result[i] = messages[i].model_copy(update={"content": content})
    return result


def completed_status(store):
    path = store.path("status.json")
    if not path.is_file():
        return None
    status = json.loads(path.read_text())
    if status.get("status") in {"completed", "draft"} and status.get("revision") == store.load().revision:
        return status
    return None


SYSTEM_PROMPT = """You are MaLiang-Harness, a code-directed image/video creator.
CLARITY: The immutable spec is the native output pixel grid. Design directly at that size;
never draw a small poster then enlarge it or surround it with margins to simulate orientation.
Budget space for content before coding. Aim for readable body/label text >= 16 output pixels
(>= 20 for dense CJK); account for all object and context transforms. Reduce content density
or simplify layout when needed, preserving requested content. Exact text, numbers, diagrams
and fine linework belong in code/SVG overlays. Image generation is a last-resort source
for a specific visual detail that code cannot adequately render after inspection. Its raster
size is limited: a larger output canvas does not create source detail.
Inspect native-resolution detail crops plus the full composition before approving render_clarity.
Preview frame_details.clarity contains heuristic tiny-text warnings; inspect and fix meaningful
labels, and distinguish intentionally decorative marks. These metrics do not prove readability
or measure text baked into bitmaps. Check video text through motion and encoded MP4 too.

VIDEO: For MP4, include video_plan in plan_creation: mode (procedural/character/image_composite/mixed),
loop, continuity, and contiguous shots [{id,start,end,action,camera}] covering the exact spec.duration.
A single shot is sufficient for simple animations. Choose scene2d for retained objects, canvas for dense
procedural motion, svg_animation for time-computed SVG, or three for real 3D geometry and cameras.
Use imported assets.input_image according to the brief; inspect it before deciding its composition.
Never replace a requested input photo with generated imagery without a reason supported by the brief.
Use absolute time, seeded randomness and stable object identities. Shared motion helpers are available:
motion.progress(t,start,end), motion.smooth(u), motion.lerp(a,b,u), motion.cycle(t,period),
motion.random(seed,index). Drive effects from the same event schedule (scene.events or local code).
Camera movement must preserve scene-attached annotations while screen captions remain fixed.
Implement shot timing from the plan in code; the plan is not an automatic animation engine.
Inspect start/middle/end and action/shot boundaries, render a short clip, then export the full duration.
Use inspect_video on the final export to check decoded frames before finalization; this needs no rerender.
For loops check t=0, duration-1/fps and t=duration; state at duration should equal zero, without a duplicated
terminal frame. Fix unintended static output, jumps and unreadable captions. Audio is not implemented.
Three.js is bundled locally; no external libraries/loaders/network. A 2D result does not require 3D.

Before a meaningful code/object/motion edit or group, give a brief user-visible action summary: what you will change and what you intend to preserve. After observation, state the visible result when useful. These are concise operation notes, not hidden reasoning; do not add an extra model call solely for narration.
The immutable user brief and output specification are authoritative. CURRENT AUTHORITATIVE STATE
already supplies revision, capabilities, backend contracts, requirements, components and assets.
Start with plan_creation; do not spend turns rediscovering known state with describe_environment,
list_capabilities or read_artwork unless information is actually missing.

Plan by CONTROL NEEDS, not by nouns alone. Route components to code by default. Write
control_requirements: what must move, remain independently editable, preserve exact layout/text,
or share stable lighting and perspective. Build the requested scene with code first: composition,
major subjects, environment, lighting, materials and meaningful detail. A flat placeholder or
layout sketch does not count as a code attempt. Render and inspect the substantive code result
before deciding that any generated asset is necessary. Complexity, realism, painterly texture,
limited time, or the availability of an image API alone do not justify generation. Try code
refinement for an observed defect before changing its route to generated_subject or
generated_texture. Generated assets must be small, local supplements; keep the defining visual
content and most of the final visible composition code-drawn. Never replace a working code scene
with a generated image. If something must move independently, do not bake it into an asset.
No image editing, depth, segmentation or video generation model is available: the existing image
API accepts TEXT ONLY. Layout sketches are code state, not image conditioning.

Under conservative_planning, generated components still need brief_evidence quoting the original brief,
code_limitation explaining the observed code result's specific visual defect, and minimal_scope
describing the smallest local generated part. Cite the relevant rendered observation in the plan;
do not treat a style adjective in the brief as evidence that code cannot meet it. Do not invent
realism requirements. Do not select generated_background for ordinary image or video creation.
Only use a full-canvas generated background when the user explicitly requests generated imagery
or explicitly approves that route. A code-drawn foreground accent does not make a generated
full-scene background code-directed work.
Respect max_generated_components and budgets. Select only registered/permitted renderers; generated
composition requires scene2d. Three.js/WebGL and network imports are unavailable.

In code_directed policy: plan_creation with code routes -> implement and inspect a substantive
code scene -> refine observed defects in code -> replan only an unresolved local detail -> plan_asset.
Do not batch generation with the initial plan or a layout sketch. If the user explicitly permits
a generated_background: role=background, extraction=none, target_region=full canvas. Supply
background_layout with static_contents, excluded_object_ids matching code components, camera, lighting,
reserved_regions keyed by excluded actor IDs, and named anchors (contact/ground points) in canvas pixels.
Write a self-contained generation prompt describing environment AND excluding the actors in ordinary words.
Generate once, inspect_asset, assess the center cover crop, empty actor spaces, perspective and support surfaces.
Do not assume the generator obeyed exact coordinates. Use place_background with observed_anchors measured
in final canvas coordinates and layout_notes explaining suitability; then align/refine the code actors.
Generated background is a stable image layer; it has no automatic depth or foreground occlusion. Only use
simple camera transforms; separately design occluders or revise the plan if large view changes are needed.
If the plate is unsuitable, intentionally plan a replacement with a new asset ID. After replacement,
recheck actor contacts/occlusion and all requirements; never carry over an old layout assessment.

Textures can be sampled directly via compose_asset. Independently movable generated subjects may use
extraction: inspect_asset -> extract_subject -> inspect -> place_cutout (aspect preserving) or compose_asset.
Avoid nonuniform scaling that squashes geometry. Gray backgrounds alone do not justify regeneration;
reuse/refine the source if pursuing the subject route. Inspect the final scene, not just raw assets.
In code_first_repair policy retain the existing local-area and failed-observation requirements;
background plates are only supported by code_directed policy. Do not deliberately make a poor
code draft to manufacture a reason to generate an asset.

ASSET ITERATION: inspect independent needed assets together using inspect_assets, retaining full images.
Use progress.asset_workflow to see which immutable images have cached inspection analysis and remain unbound.
Cached analysis is not a model verdict or proof an image remains in context: reuse a prior judgment only when its visual basis is still available;
if the image expired or detail is uncertain, inspect again. Do not repeat inspections just to rediscover IDs.
After the first usable source is inspected/extracted, bind it into the planned scene and observe the
composite before spending further generation calls on layout problems. Truly unusable imagery can be
replaced immediately; do not force a knowingly invalid composition. Batch already-decided extraction,
placement and observation operations; stop before decisions requiring an unseen image.
Generated subjects cannot be put_object asset references: use place_cutout or compose_asset, keeping
provenance/target restrictions. Plan a distinct target for each independently placed source BEFORE generation;
multiple independent sources cannot be combined into one restricted target via put_object.
Regions are [left,top,right,bottom], not [x,y,width,height]. On binding errors use the structured recovery
hint; do not buy new images or list all capabilities to resolve a known tool/coordinate mismatch.

EFFICIENCY: preserve visual iteration, requirement checks and repair quality. Do not shorten code or skip
observations to save calls. Use observe_requirements (empty IDs = all) after a meaningful edit group,
then evaluate EACH requirement using its mapped evidence ID. Different crops/time samples remain distinct.
A pass for the original brief cannot substitute for the other requirement reviews. If anything fails,
revise the artwork and observe again. Batch reviews+checkpoint+finalize only after seeing evidence.
Combine related patch_program edits (e.g. particle size and opacity) in one execute_steps call when
no intermediate visual decision is needed; follow with observations in that same batch.
For video: batch full export + inspect_video using evidence_id="$latest_export", then inspect native
encoded detail if necessary, then batch reviews + checkpoint + finalize with export_id="$latest_export".
Reuse the current export after review; do not export again without a revision change. Existing mapped
observations remain valid at the same revision; collect them again only if you need to view them again.
Renderer slowness alone is not a reason to simplify geometry, change particle shapes, reduce resolution,
frame rate or duration. Preserve the visual algorithm during performance repairs.
On execute_steps failure, prefer resume_steps with its resume_batch_id and corrected replacement_args:
unexecuted code is saved verbatim, so do not regenerate it. Read authoritative state only if needed;
format-only mistakes generally need corrected arguments, not another read_artwork call.
Use execute_steps for already-known sequential work: plan+object writes; multiple
object edits; generation+inspection; extraction+inspection+preview; related observations; or
reviews+checkpoint+finalize using an already inspected export. The harness supplies fresh revisions for each child operation.
Stop the group before any decision requiring unseen image evidence. Batch independent requirements
for the same revision. Do not repeatedly render or inspect unchanged content. Work coarse-to-fine
in coherent groups and observe after meaningful edits, not after each small mutation.

PATHTRACE BACKEND: For a pathtrace-only task, plan backend="pathtrace" with code components.
Use set_pathtrace_scene then edit_pathtrace_scene to change individual objects/materials/lights or camera/render.
Coordinates are world units, Y-up, rotations XYZ degrees. Lathe profile is [radius,y] and needs
an inner wall for hollow vessels; tube points are xyz; extrude profile is xy and depth is height along Z.
Presets are starting points, not guaranteed realism. Glass/liquid need closed geometry and plausible scale.
Use preview_mode=raster only for composition, then pathtrace for material/light checks. Export always
uses final_samples independently of preview; observe_requirements after export shows the actual final image (and inspect_region accepts PNG exports). Do not substitute preview observations for final-image inspection. Do not confuse noisy
low-sample previews with incorrect geometry. Improve shape/material before increasing sample count.
Uploaded images can be texture_asset IDs. There is no external asset/model download or glTF loader;
custom meshes can be supplied as vertices and triangle indices. No skin/food scattering guarantee.

PAINT BACKEND: If paint is permitted, use plan_creation(backend="paint", route="code" components),
then init_painting and paint_strokes. These tools write a replayable libmypaint brush document;
do not write JavaScript/SVG or call image generation in a paint-only task. Build large shapes,
then mid-scale shading and details. Batch strokes instead of issuing one tool call per point.
Use soft brushes for tonal transitions, round for masses, ink for details. Smudge samples only
its own layer. Eraser reveals lower layers/paper. Inspect full composition and native crops after
major stages. Existing paintings can be edited by adding/removing strokes, changing layer
opacity/visibility, or restoring a version. Reference images are for observation, not pixel stamping.
Brush simulation does not establish realism; assess the actual output honestly.

Create concrete composition, style and temporal requirements; checkpoints cover ALL hard checks
including brief_fulfillment. Existing requirements may not be weakened. Use object IDs in
observation specs when appropriate, avoiding stale fixed crop regions after placement. Review
CURRENT evidence honestly; these are model self-assessments. Every edit invalidates reviews.
Do not infer visibility or continuous motion from object metadata or sparse frames. Render relevant
video intervals; compare_versions is useful for uncertain changes, not a mandatory ritual every time.
Keep sources in domain tools (read_object for old code), not scratch files. Absolute t and seeded
randomness drive animation. No network calls in rendering code. Do not delegate.

Record reviews, complete checkpoints and finalize in a single execute_steps group when
ready; finalize export_id can be "$latest_export". Export first if no current export exists; for video
inspect the encoded export before this final review group. Finalization gates cannot be bypassed by prose.
REVIEW EVIDENCE: A passing visual/style/temporal review must include its own observe_requirement evidence;
additional current exports, decoded frames, crops and other requirement observations are allowed as support.
Temporal passes need >=3 timestamps from that requirement's own observations. Fail/uncertain reviews can
cite any current evidence: never repeatedly observe an unavailable capability merely to record failure.
VIDEO AUDIO POLICY: MP4 tasks deliver silent video. Ignore requested audio, music synthesis, speech,
sound effects and audio export; do not implement Web Audio or add audio acceptance requirements.
Keep music-inspired visual movement if requested. Evaluate brief_fulfillment against the visual brief,
not its sound requests. Existing audio_track requirements are explicitly ignored by policy, never
claimed as implemented. Missing sound alone must not force a draft or block finalization.
For non-video requests, mark unsupported sound requirements with required_capability="audio_track".
Declare unsupported requested capabilities in the initial plan. Produce the supported visual draft where
useful, then export and call finish_draft with the limitation. Do not attempt impossible passing checkpoints.
Once a valid visual judgment is made, batch record_review calls and checkpoint/finalization. Old viewed
images expire from later requests; use existing inspection/observation tools if you need to inspect again.
Stop after successful finalization or finish_draft. If quality/capabilities/budget block delivery, preserve a draft
and finish_draft with the specific blocker. Never claim photographic quality merely from API setup.
"""


def make_context(root, budget=None, mode="maliang", resume=False, plugins=True, image_generation=None):
    store = ProjectStore(root)
    meter = Meter(store, budget or Budget(), resume=resume)
    registry = Registry(store, meter)
    renderers = RenderService(store, meter)
    renderers.register(SceneRenderer())
    renderers.register(CanvasRenderer())
    renderers.register(SVGRenderer())
    renderers.register(SVGAnimationRenderer())
    renderers.register(ThreeRenderer())
    from .adapters.renderers import PaintRenderer, PathtraceRenderer

    renderers.register(PaintRenderer())
    renderers.register(PathtraceRenderer())
    if image_generation is None:
        from .settings import ImageGenerationSettings

        saved = store.path("run_config.json")
        config = json.loads(saved.read_text()).get("image_generation") if saved.exists() else None
        image_generation = ImageGenerationSettings.model_validate(config) if config else None
    if store.path("edit.json").exists():
        from .settings import ImageGenerationSettings

        image_generation = ImageGenerationSettings(enabled=False)
    context = Context(store, meter, renderers, registry, mode, image_generation)
    from .settings import EfficiencySettings

    saved = store.path("run_config.json")
    efficiency = json.loads(saved.read_text()).get("efficiency") if saved.exists() else None
    context.efficiency = EfficiencySettings.model_validate(efficiency or {})
    install(context)
    assets.install(context)
    from . import paint, pathtrace, scene_tools, workflow

    scene_tools.install(context)
    paint.install(context)
    pathtrace.install(context)
    if mode == "maliang":
        workflow.install(context)
        from . import editing

        editing.install(context)
    if plugins:
        registry.load_plugins(context)
    from . import batching

    batching.install(context)
    return context


def create_model(model_name: str, *, timeout: float = 120, max_retries: int = 0, max_tokens: int = 12000):
    if not model_name:
        raise ValueError("Set MALIANG_MODEL or pass --model to choose an API model")
    if not os.environ.get("OPENAI_API_KEY"):
        raise ValueError("Set OPENAI_API_KEY in your environment. Never put it in artwork files.")
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=model_name,
        use_responses_api=True,
        timeout=timeout,
        max_retries=max_retries,
        max_tokens=max_tokens,
        model_kwargs={"parallel_tool_calls": False},
    )


def build_agent(context, model, checkpointer=None):
    from deepagents import create_deep_agent
    from deepagents.backends import StateBackend
    from langchain.agents.middleware import AgentMiddleware, hook_config
    from langchain_core.messages import SystemMessage, ToolMessage

    class CreativeContext(AgentMiddleware):
        @hook_config(can_jump_to=["end"])
        def before_model(self, state, runtime):
            # Finish through the graph's normal terminal edge, without another API call.
            if completed_status(context.store):
                return {"jump_to": "end"}
            return None

        def wrap_model_call(self, request, handler):
            # Context is rebuilt from authoritative artwork, not inferred from conversation memory.
            art = context.store.load()
            details = {
                "revision": art.revision,
                "spec": art.spec.model_dump(),
                "video_plan": art.video_plan.model_dump() if art.video_plan else None,
                "allowed_backends": art.allowed_backends,
                "allow_generated_assets": art.allow_generated_assets,
                "usage": context.meter.usage,
                "budget": context.meter.budget.model_dump(),
                "input_tokens_remaining": max(
                    0, context.meter.budget.max_input_tokens - context.meter.usage["input_tokens"]
                ),
                "objects": [
                    {
                        "id": o.id,
                        "layer": o.layer,
                        "asset_ids": o.asset_ids,
                        "transform": o.transform.model_dump(),
                        "background_layout": o.properties.get("background_layout"),
                    }
                    for o in art.objects
                ],
                "assets": [{"id": a.id, "provenance": a.provenance} for a in art.assets],
                "components": [c.model_dump() for c in art.components],
            }
            if context.mode == "maliang":
                details.update(
                    plan=art.plan,
                    requirements=[r.model_dump() for r in art.requirements],
                    validation=verify(context.store),
                    environment=runtime_capabilities(context),
                    progress=workflow_progress(context.store),
                )
            original = request.system_message.content if request.system_message else ""
            if not isinstance(original, str):
                original = json.dumps(original)
            tools = [t for t in request.tools if (t.get("name") if isinstance(t, dict) else t.name) != "task"]
            # Checkpoint messages remain intact; optimize only the outgoing request.
            messages = prepare_image_messages(
                request.messages,
                context.efficiency.recent_image_messages,
                context.efficiency.max_image_replay_turns,
            )
            return handler(
                request.override(
                    messages=messages,
                    system_message=SystemMessage(
                        content=original
                        + "\nCURRENT AUTHORITATIVE STATE:\n"
                        + json.dumps(details, ensure_ascii=False)
                    ),
                    tools=tools,
                )
            )

        def wrap_tool_call(self, request, handler):
            if request.tool_call["name"] == "task":
                return ToolMessage(
                    content="Delegation disabled for this controlled run.",
                    tool_call_id=request.tool_call["id"],
                    status="error",
                )
            return handler(request)

    return create_deep_agent(
        model=model,
        tools=context.registry.langchain_tools(),
        system_prompt=SYSTEM_PROMPT
        if context.mode == "maliang"
        else "Create the requested artwork using available tools. Read contracts, render and revise as useful. "
        "Respect the immutable task specification. Export a final artifact. Do not delegate.",
        backend=StateBackend(),
        middleware=[CreativeContext()],
        checkpointer=checkpointer,
        name="maliang",
    )


def user_content(store, *, full_spec=False):
    art = store.load()
    content = [
        {
            "type": "text",
            "text": json.dumps(art.model_dump(), ensure_ascii=False) if full_spec else art.prompt,
        }
    ]
    if art.spec.format == "mp4":
        content.append({"type": "text", "text": "Delivery policy: generate silent video. Ignore all sound/audio synthesis requests and audio acceptance criteria, including in the original brief. Preserve requested visual actions. Do not claim audio was generated."})
    edit_path = store.path("edit.json")
    if edit_path.exists():
        import base64

        edit = json.loads(edit_path.read_text())
        content.append(
            {
                "type": "text",
                "text": "EDIT SESSION: Start from the inherited artwork; do not recreate it from scratch. "
                "Inspect source previews and read relevant object/program code before changing it. "
                "The latest edit overrides conflicting historical requirements only. Before plan_creation, "
                "use revise_edit_requirement for each inherited requirement that conflicts, quoting the user instruction "
                "and retaining unaffected clauses. Then plan checkpoints covering every hard requirement. "
                "Use inspect_edit_source for visual before/after comparison. New evidence and reviews are required. "
                "Bitmap generation/editing is unavailable. Source: "
                + str(edit["source_run"])
                + " revision "
                + str(edit["source_revision"]),
            }
        )
        for item in edit.get("baseline", []):
            content.append({"type": "text", "text": f"Source version reference at t={item['time']}s"})
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,"
                        + base64.b64encode(store.path(item["path"]).read_bytes()).decode()
                    },
                }
            )
    for asset in art.assets:
        if asset.provenance.get("source") == "user_upload":
            import base64

            content.append(
                {
                    "type": "text",
                    "text": f"User image: assets.{asset.id}; native dimensions {asset.provenance.get('width')}x{asset.provenance.get('height')}. Use as reference or compose according to the brief.",
                }
            )
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,"
                        + base64.b64encode(store.path(asset.path).read_bytes()).decode()
                    },
                }
            )
    return content


def run_agent(context, model, resume=False):
    from langchain_core.callbacks import BaseCallbackHandler
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.errors import GraphRecursionError

    class Accounting(BaseCallbackHandler):
        raise_error = True

        def on_chat_model_start(self, *args, **kwargs):
            context.meter.consume("model_calls")
            context.store.log("model_start", {"call": context.meter.usage["model_calls"]})

        def on_llm_end(self, response, **kwargs):
            for generation in response.generations:
                for item in generation:
                    message = getattr(item, "message", None)
                    if message is not None:
                        context.meter.record_tokens(getattr(message, "usage_metadata", None) or {})
                        content = message.content
                        if isinstance(content, list):
                            content = [
                                block
                                for block in content
                                if isinstance(block, dict) and block.get("type") in {"text", "output_text"}
                            ]
                        context.store.log(
                            "model_response",
                            {"content": content, "tool_calls": getattr(message, "tool_calls", [])},
                        )
            context.store.log("model_end", {"usage": context.meter.usage})

        def on_llm_error(self, error, **kwargs):
            context.store.log(
                "model_error",
                {
                    "error": explain_api_error(error) or type(error).__name__,
                    "error_type": type(error).__name__,
                    "cause_type": type(error.__cause__).__name__ if error.__cause__ else None,
                },
            )

        def on_tool_start(self, serialized, input_str, **kwargs):
            name = serialized.get("name", "") if serialized else ""
            if name not in context.registry.capabilities:
                context.meter.consume("tool_calls")
                context.store.log("upstream_tool", {"tool": name, "arguments": input_str})

    config = {
        "configurable": {"thread_id": "artwork"},
        "recursion_limit": 400,
        "max_concurrency": 1,
        "callbacks": [Accounting()],
    }
    lock = FileLock(str(context.store.root / ".run.lock"), timeout=0)
    with lock, SqliteSaver.from_conn_string(str(context.store.path("checkpoints.sqlite"))) as saver:
        previous = completed_status(context.store)
        if resume and previous and previous["status"] == "draft":
            atomic_json(
                context.store.path("status.json"), {"status": "working", "revision": previous["revision"]}
            )
        agent = build_agent(context, model, saver)
        snapshot = agent.get_state(config)
        initial = {"messages": [{"role": "user", "content": user_content(context.store)}]}
        if resume:
            if snapshot.next:
                initial = None
            elif snapshot.values:
                initial = {
                    "messages": [
                        {
                            "role": "user",
                            "content": "Continue from the saved artwork state. Re-read current revision and pending requirements before acting.",
                        }
                    ]
                }
        try:
            # With a single worker, async delta checkpoint commits can wait for writes
            # queued behind themselves. Flush each step before advancing the graph.
            result = agent.invoke(initial, config=config, durability="sync")
            # Plain prose cannot bypass the finalization gate.
            status_file = context.store.path("status.json")
            status = json.loads(status_file.read_text())
            if context.mode == "generic-agent":
                exports = [context.store.evidence(p.stem) for p in context.store.root.glob("evidence/*.json")]
                exports = [
                    e for e in exports if e.kind == "export" and e.revision == context.store.load().revision
                ]
                if exports:
                    export = exports[-1]
                    atomic_json(context.store.path("validation.json"), verify(context.store, export.id))
                    status = {
                        "status": "baseline_exported",
                        "revision": export.revision,
                        "export_id": export.id,
                        "note": "Requires independent quality evaluation",
                    }
                    atomic_json(status_file, status)
            export_id = latest_export(context.store)
            report = verify(context.store, export_id)
            atomic_json(context.store.path("validation.json"), report)
            if status.get("status") not in {"completed", "baseline_exported", "draft"}:
                unmet = [r for r in report["requirements"] if r["hard"] and r["verdict"] != "pass"]
                status = {
                    "status": "draft" if export_id else "incomplete",
                    "revision": context.store.load().revision,
                    "export_id": export_id,
                    "reason": "Exported draft; requirement/checkpoint validation is incomplete"
                    if export_id
                    else "Agent ended without an export or verified finalization",
                    "pending_requirements": [r["id"] for r in unmet],
                }
                atomic_json(status_file, status)
            context.store.log("run_end", status)
            last = result["messages"][-1]
            context.store.path("agent_response.txt").write_text(str(last.content))
            return status
        except (BudgetExceeded, GraphRecursionError) as exc:
            export_id = latest_export(context.store)
            report = verify(context.store, export_id)
            atomic_json(context.store.path("validation.json"), report)
            status = {
                "status": "budget_exhausted",
                "export_id": export_id,
                "revision": context.store.load().revision,
                "reason": str(exc),
                "pending_requirements": [
                    r["id"] for r in report["requirements"] if r["hard"] and r["verdict"] != "pass"
                ],
            }
            atomic_json(context.store.path("status.json"), status)
            context.store.log("run_end", status)
            return status
        except Exception as exc:
            reason = explain_api_error(exc) or str(exc)[:2000]
            atomic_json(
                context.store.path("status.json"),
                {"status": "failed", "revision": context.store.load().revision, "reason": reason},
            )
            context.store.log("run_error", {"error": reason})
            raise
        finally:
            context.meter.flush()


def _single_shot(context, model):
    """One model call, common backend contracts, no visual iteration. Evaluation is external."""
    from langchain_core.messages import HumanMessage, SystemMessage

    context.meter.consume("model_calls")
    response = model.invoke(
        [
            SystemMessage(
                content="Create the requested artwork. Return JSON with backend and source. "
                "For scene2d also include a nonempty objects array, each containing object_id, source "
                "(custom draw function), properties, transform and layer as needed. "
                "Backend contracts: " + json.dumps(context.renderers.available())
            ),
            HumanMessage(content=user_content(context.store, full_spec=True)),
        ]
    )
    context.meter.record_tokens(response.usage_metadata or {})
    text = (
        response.content
        if isinstance(response.content, str)
        else "".join(item.get("text", "") for item in response.content if isinstance(item, dict))
    )
    if text.strip().startswith("```"):
        text = text.strip().split("\n", 1)[1].rsplit("```", 1)[0]
    payload = json.loads(text)
    saved = context.registry.invoke(
        "write_program",
        {
            "expected_revision": context.store.load().revision,
            "backend": payload["backend"],
            "source": payload["source"],
        },
    )
    if saved.get("status") == "error":
        raise ValueError(saved["error"])
    if payload["backend"] == "scene2d":
        if not payload.get("objects"):
            raise ValueError("scene2d single-shot output must include bound objects")
        for obj in payload["objects"]:
            created = context.registry.invoke(
                "put_object", {**obj, "expected_revision": context.store.load().revision}
            )
            if created.get("status") == "error":
                raise ValueError(created["error"])
    export = context.renderers.export()
    report = verify(context.store, export.id)
    atomic_json(context.store.path("validation.json"), report)
    status = {
        "status": "baseline_exported",
        "export_id": export.id,
        "revision": export.revision,
        "note": "No visual self-review; use the same external evaluator for all modes",
    }
    atomic_json(context.store.path("status.json"), status)
    return status


def run_single_shot(context, model):
    with FileLock(str(context.store.root / ".run.lock"), timeout=0):
        try:
            return _single_shot(context, model)
        except BudgetExceeded as exc:
            export_id = latest_export(context.store)
            report = verify(context.store, export_id)
            atomic_json(context.store.path("validation.json"), report)
            status = {
                "status": "budget_exhausted",
                "export_id": export_id,
                "revision": context.store.load().revision,
                "reason": str(exc),
                "pending_requirements": [
                    r["id"] for r in report["requirements"] if r["hard"] and r["verdict"] != "pass"
                ],
            }
            atomic_json(context.store.path("status.json"), status)
            return status
        except Exception as exc:
            atomic_json(
                context.store.path("status.json"),
                {"status": "failed", "revision": context.store.load().revision, "reason": str(exc)[:2000]},
            )
            raise
        finally:
            context.meter.flush()
