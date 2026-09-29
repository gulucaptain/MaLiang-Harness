from __future__ import annotations


class AssetPlanning:
    def validate_component(self, art, task):
        if self.config.workflow != "code_directed":
            return
        from ...generation_policy import validate_generation_plan

        validate_generation_plan(self.config, art.prompt, [c.model_dump() for c in art.components])
        component = next((c for c in art.components if c.object_id == task.get("target_object_id")), None)
        expected_route = {
            "subject": "generated_subject",
            "texture": "generated_texture",
            "background": "generated_background",
        }.get(task["role"])
        if component is None or expected_route is None or component.route != expected_route:
            raise ValueError(
                "COMPONENT_PLAN_REQUIRED: generate only the subject/texture assigned in plan_creation"
            )
        if not set(task["requirement_ids"]) <= set(component.requirement_ids):
            raise ValueError("Asset requirements must belong to its planned component")
        if art.planned_backend != "scene2d" or not art.program or art.program.backend != "scene2d":
            raise ValueError(
                "CODE_SCENE_REQUIRED: establish the code composition before requesting a component"
            )
        if not any(
            c.route == "code" and any(o.id == c.object_id and o.draw and not o.asset_ids for o in art.objects)
            for c in art.components
        ):
            raise ValueError("CODE_SCENE_REQUIRED: draw substantive code-owned content first")
        if not any(o.id == component.object_id and o.draw for o in art.objects):
            raise ValueError("Create the component's code-controlled layer/placeholder first")
        region = task.get("target_region")
        if not region or not (
            0 <= region[0] < region[2] <= art.spec.width and 0 <= region[1] < region[3] <= art.spec.height
        ):
            raise ValueError("LOCAL_COMPONENT_REQUIRED: specify an in-canvas placement region")
        area = (region[2] - region[0]) * (region[3] - region[1])
        if task["role"] == "background":
            layout = task.get("background_layout")
            if task.get("extraction") != "none" or not layout:
                raise ValueError("BACKGROUND_LAYOUT_REQUIRED: use no extraction and declare layout")
            if region != [0, 0, art.spec.width, art.spec.height]:
                raise ValueError("Background plates cover the full canvas")
            code_ids = {c.object_id for c in art.components if c.route == "code"}
            if not set(layout["excluded_object_ids"]) <= code_ids or not code_ids:
                raise ValueError("Exclude independently code-controlled objects from background generation")
            if not set(layout["reserved_regions"]) <= set(layout["excluded_object_ids"]):
                raise ValueError("Reserve regions only for excluded code objects")
            for box in layout["reserved_regions"].values():
                if box[2] > art.spec.width or box[3] > art.spec.height:
                    raise ValueError("Reserved region outside canvas")
            for point in layout["anchors"].values():
                if point[0] > art.spec.width or point[1] > art.spec.height:
                    raise ValueError("Anchor outside canvas")
            if not component.control_requirements.strip():
                raise ValueError("Describe why grouped background contents can remain static")
            return
        if area > art.spec.width * art.spec.height * self.config.max_repair_area_fraction:
            raise ValueError("LOCAL_COMPONENT_REQUIRED: generated component occupies too much of the canvas")
        if task["role"] == "subject" and task.get("extraction") != "white_background":
            raise ValueError("Subjects require an isolated-background extraction plan")


    def plan_asset(self, expected_revision, task):
        art = self.store.load()
        if not art.allow_generated_assets:
            raise ValueError("Generated assets are forbidden for this task")
        if art.workflow_policy == "guided" and (not art.plan or not art.capability_assessment):
            raise ValueError("Plan overall creation and code/generator division before planning an asset")
        if not set(task["requirement_ids"]) <= {r.id for r in art.requirements}:
            raise ValueError("Asset task must reference existing requirements")
        if task["asset_id"] in {a.id for a in art.assets} | {t.asset_id for t in art.asset_tasks}:
            raise ValueError("Asset task ID exists; use a new ID for a targeted replacement")
        if task["role"] == "background" and self.config.workflow != "code_directed":
            raise ValueError("Background plates require code_directed workflow")
        self.validate_component(art, task)
        if self.config.workflow == "code_first_repair" and art.workflow_policy == "guided":
            if not art.program or not art.objects:
                raise ValueError(
                    "CODE_DRAFT_REQUIRED: draw the scene with code before planning generated repair"
                )
            if art.program.backend != "scene2d":
                raise ValueError(
                    "LOCAL_REPAIR_REQUIRES_SCENE2D: use a code-drawn scene2d canvas for image repair"
                )
            evidence_id = task.get("repair_evidence_id")
            region = task.get("target_region")
            target = task.get("target_object_id")
            if not evidence_id or not region:
                raise ValueError("LOCAL_REPAIR_REQUIRED: cite a failed current observation and target region")
            if not (
                0 <= region[0] < region[2] <= art.spec.width and 0 <= region[1] < region[3] <= art.spec.height
            ):
                raise ValueError("Repair region outside output canvas")
            fraction = (region[2] - region[0]) * (region[3] - region[1]) / (art.spec.width * art.spec.height)
            if fraction > self.config.max_repair_area_fraction:
                raise ValueError("LOCAL_REPAIR_REQUIRED: target region covers too much of the canvas")
            obj = next((o for o in art.objects if o.id == target), None)
            if obj is None or obj.draw is None or obj.asset_ids:
                raise ValueError("CODE_OBJECT_REQUIRED: target a code-drawn scene2d object without assets")
            evidence = self.store.evidence(evidence_id)
            if (
                evidence.revision != art.revision
                or evidence.metadata.get("requirement_id") not in task["requirement_ids"]
            ):
                raise ValueError("FAILED_OBSERVATION_REQUIRED: cite a current requirement observation")
            parent_id = evidence.metadata.get("parent")
            if not parent_id:
                raise ValueError("FAILED_OBSERVATION_REQUIRED: cite a rendered scene observation")
            parent = self.store.evidence(parent_id)
            if parent.revision != art.revision or not parent.metadata.get("frame_details"):
                raise ValueError("FAILED_OBSERVATION_REQUIRED: missing current scene frame details")
            bounds = parent.metadata["frame_details"][0].get("object_bounds", {}).get(target)
            if bounds and not (
                region[0] <= bounds[0]
                and region[1] <= bounds[1]
                and bounds[2] <= region[2]
                and bounds[3] <= region[3]
            ):
                raise ValueError("LOCAL_REPAIR_REQUIRED: target region must contain the code object bounds")
            review = self.store.reviews().get(evidence.metadata["requirement_id"])
            if (
                review is None
                or review.verdict not in {"fail", "uncertain"}
                or evidence_id not in review.evidence_ids
            ):
                raise ValueError(
                    "FAILED_REVIEW_REQUIRED: review the current code rendering as fail or uncertain"
                )
            if task["role"] == "subject" and task.get("extraction") != "white_background":
                raise ValueError("LOCAL_CUTOUT_REQUIRED: subject repairs need white_background extraction")
        art = self.store.mutate(expected_revision, lambda data: data["asset_tasks"].append(task))
        return {"revision": art.revision, "asset_task": task}


