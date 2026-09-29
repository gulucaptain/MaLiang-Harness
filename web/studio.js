"use strict";
const $ = (id) => document.getElementById(id);
const q = new URLSearchParams(location.search);
const s = {
  run: q.get("run") || "",
  mode: q.get("mode") || (q.has("run") ? "history" : "image"),
  generation: 0,
  selection: 0,
  steps: [],
  versions: [],
  records: [],
  data: null,
  detail: null,
  state: null,
  follow: true,
  nav: "steps",
  limit: 120,
  diff: true,
  busy: false,
  rendering: false,
  tried: new Set(),
  drafts: { image: "", video: "", paint: "", pathtrace: "" },
  paintAvailable: null,
  pathtraceAvailable: null,
};
let timer;
let previewQueue = Promise.resolve();
function preview(run, revision) {
  const task = previewQueue.then(() => api("/api/preview", { run, revision }));
  previewQueue = task.catch(() => {});
  return task;
}
const labels = {
  running: "运行中",
  working: "运行中",
  completed: "已完成",
  failed: "失败",
  interrupted: "已中断",
  budget_exhausted: "预算耗尽",
  baseline_exported: "已导出",
  draft: "草稿",
  incomplete: "未完成",
  ok: "完成",
};
function el(tag, text, cls) {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (cls) n.className = cls;
  return n;
}
function error(e) {
  $("error").textContent = e?.message || String(e);
  $("error").hidden = false;
}
function clearError() {
  $("error").hidden = true;
}
async function api(path, body) {
  const r = await fetch(
    path,
    body === undefined
      ? {}
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
  );
  const data = await r.json();
  if (!r.ok) throw Error(data.error || `请求失败 ${r.status}`);
  return data;
}
const url = (path, extra = {}) =>
  path + "?" + new URLSearchParams({ run: s.run, ...extra });
const mediaURL = (path, run = s.run) =>
  "/media?" + new URLSearchParams({ run, path });
function locationState() {
  const p = new URLSearchParams({ mode: s.mode });
  if (s.run) p.set("run", s.run);
  history.replaceState(null, "", "/studio?" + p);
}
function running() {
  return s.state?.exit_code === null;
}
function renderMonitor() {
  const state = s.state;
  for (const [id, key] of [
    ["llmCalls", "model_calls"],
    ["toolCalls", "tool_calls"],
    ["imageGenCalls", "asset_api_calls"],
  ]) {
    const count = state?.usage?.[key];
    $(id).textContent = Number.isFinite(count)
      ? count.toLocaleString("zh-CN")
      : "—";
    const limit = state?.budget?.["max_" + key];
    $(id).title = Number.isFinite(limit)
      ? `累计调用 / 上限：${count ?? "—"} / ${limit}`
      : "当前任务累计调用";
  }
  $("currentRevision").textContent =
    state?.revision != null ? `v${state.revision}` : "—";
  const viewed = s.data?.artwork?.revision;
  $("viewingRevision").hidden =
    viewed == null || state?.revision == null || viewed === state.revision;
  $("viewingRevision").textContent =
    `正在查看历史 v${viewed} · 任务当前为 v${state?.revision}`;
  if (!state) {
    $("status").dataset.state = "unknown";
    return;
  }
  const latest = s.steps.at(-1);
  $("activity").textContent = running()
    ? latest
      ? `${latest.kind === "model" && latest.status === "running" ? "等待 LLM 返回" : latest.title}${latest.status === "running" && latest.kind !== "model" ? " · 执行中" : ""}`
      : "正在初始化…"
    : state.status?.reason ||
      (state.status?.status === "completed"
        ? "作品已完成"
        : latest?.title || "任务已停止");
  $("status").dataset.state = running()
    ? "running"
    : state.status?.status || "unknown";
}
function controls() {
  renderMonitor();
  document.body.dataset.hasRun = Boolean(s.run);
  const ready = !!s.data?.artwork && !s.selecting;
  $("previewVersion").disabled =
    !s.data?.artwork?.program || s.rendering || s.selecting;
  $("toEdit").disabled = !ready;
  $("editSubmit").disabled = !ready || s.busy || running();
  $("resume").disabled = !s.state?.can_resume || s.busy;
  $("submit").disabled = s.busy || (s.mode === "paint" && s.paintAvailable !== true) || (s.mode === "pathtrace" && s.pathtraceAvailable !== true);
  $("follow").setAttribute("aria-pressed", s.follow);
  $("editTarget").textContent = s.data
    ? `v${s.data.artwork.revision}`
    : "未选择版本";
  $("editState").textContent = running()
    ? "任务运行中，完成后可编辑。"
    : "保留原版，创建新版本。";
}
function setMode(mode) {
  if (!["image", "video", "history", "paint", "pathtrace"].includes(mode)) mode = "image";
  if (s.mode !== "history") s.drafts[s.mode] = $("prompt").value;
  s.mode = mode;
  document
    .querySelectorAll("[data-mode]")
    .forEach((b) => b.setAttribute("aria-pressed", b.dataset.mode === mode));
  $("createPanel").hidden = mode === "history";
  $("historyIntro").hidden = mode !== "history";
  $("editPanel").hidden = mode !== "history";
  $("videoFields").hidden = mode !== "video";
  $("paintInfo").hidden = mode !== "paint";
  if (mode === "paint") checkPaint();
  $("pathtraceInfo").hidden = mode !== "pathtrace";
  if (mode === "pathtrace") checkPathtrace();
  $("createTitle").textContent =
    mode === "pathtrace" ? "创作写实静物" : mode === "paint" ? "用笔触创作" : mode === "video" ? "生成静音视频" : "生成一张图像";
  $("submit").textContent = mode === "pathtrace" ? "开始渲染" : mode === "paint" ? "开始绘画" : mode === "video" ? "生成视频" : "生成图像";
  if (mode !== "history") $("prompt").value = s.drafts[mode] || "";
  if (mode === "history") setNav("versions");
  const welcome = $("welcomeTitle");
  if (welcome)
    welcome.textContent =
      mode === "video"
        ? "创建视频"
        : mode === "history"
          ? "选择历史任务"
          : mode === "pathtrace" ? "开始写实渲染" : mode === "paint" ? "开始笔触绘画" : "创建图像";
  renderHistory();
  locationState();
  controls();
}
async function checkPaint() {
  try {
    const status = await api("/api/paint");
    s.paintAvailable = status.available;
    $("paintStatus").textContent = status.available
      ? "libmypaint 已就绪 · 5 种笔刷 · 支持参考图观察"
      : status.message;
  } catch (e) {
    s.paintAvailable = false;
    $("paintStatus").textContent = "引擎检查失败：" + e.message;
  }
  controls();
}
async function checkPathtrace() {
  try {
    const status = await api("/api/pathtrace");
    s.pathtraceAvailable = status.available;
    $("pathtraceStatus").textContent = status.message;
  } catch (e) {
    s.pathtraceAvailable = false;
    $("pathtraceStatus").textContent = "引擎检查失败：" + e.message;
  }
  controls();
}
function setNav(nav) {
  s.nav = nav;
  $("steps").hidden = nav !== "steps";
  $("versions").hidden = nav !== "versions";
  $("stepsTab").setAttribute("aria-selected", nav === "steps");
  $("versionsTab").setAttribute("aria-selected", nav === "versions");
  renderNav();
}
function renderHistory() {
  const select = $("history");
  select.replaceChildren(new Option("选择已有任务…", ""));
  for (const r of s.records.filter(
    (r) => s.mode === "history" || r.kind === s.mode || r.id === s.run,
  )) {
    select.add(
      new Option(
        `${r.is_edit ? "编辑 · " : ""}${r.kind === "pathtrace" ? "写实" : r.kind === "paint" ? "绘画" : r.kind === "video" ? "视频" : "图像"} · ${r.title}`,
        r.id,
      ),
    );
  }
  if (s.run && !Array.from(select.options).some((o) => o.value === s.run))
    select.add(new Option("任务初始化中 · " + s.run, s.run));
  select.value = s.run;
}
async function refreshHistory() {
  const data = await api("/api/runs");
  s.records = data.records || [];
  renderHistory();
}
function renderNav() {
  const scroll = $("steps").scrollTop,
    versionScroll = $("versions").scrollTop;
  const query = $("stepSearch").value.toLowerCase();
  $("stepCount").textContent = s.steps.length;
  $("versionCount").textContent = s.versions.length;
  const steps = s.steps.filter((x) =>
    `${x.title} ${x.tool} ${x.summary}`.toLowerCase().includes(query),
  );
  $("steps").replaceChildren();
  for (const [i, x] of steps.slice(-s.limit).entries()) {
    const b = el("button", undefined, "step");
    b.dataset.step = x.id;
    b.setAttribute("aria-pressed", s.detail?.id === x.id);
    const top = el("div", undefined, "step-top");
    top.append(
      el(
        "span",
        String(Math.max(0, steps.length - s.limit) + i + 1).padStart(2, "0"),
        "step-number",
      ),
      el("strong", x.title),
      el("span", undefined, "dot " + x.status),
    );
    b.append(
      top,
      el(
        "small",
        `v${x.before_revision} → v${x.revision} · ${labels[x.status] || x.status}`,
      ),
    );
    b.onclick = () => selectStep(x, false);
    $("steps").append(b);
  }
  $("steps").scrollTop = s.follow ? $("steps").scrollHeight : scroll;
  if (!steps.length) $("steps").append(el("p", "暂无匹配步骤", "empty-small"));
  $("versions").replaceChildren();
  for (const v of s.versions.filter((v) =>
    `v${v.revision} ${v.backend} ${v.summary}`.toLowerCase().includes(query),
  )) {
    const b = el("button", undefined, "version-card");
    b.dataset.revision = v.revision;
    b.setAttribute("aria-pressed", s.data?.artwork.revision === v.revision);
    if (v.thumbnail) {
      const img = el("img");
      img.src = mediaURL(v.thumbnail);
      img.alt = `版本 v${v.revision} 缩略图`;
      img.loading = "lazy";
      b.append(img);
    }
    b.append(
      el("strong", `v${v.revision} · ${v.backend || "规划中"}`),
      el("small", v.summary),
      el("small", new Date(v.created_at * 1000).toLocaleString("zh-CN")),
    );
    b.onclick = () => selectVersion(v.revision);
    $("versions").append(b);
  }
  if (!s.versions.length)
    $("versions").append(el("p", "暂无历史版本", "empty-small"));
  $("versions").scrollTop = versionScroll;
  $("moreSteps").hidden = s.nav !== "steps" || steps.length <= s.limit;
}
function mediaElement(m, run = s.run) {
  const video = /\.mp4$/i.test(m.path);
  const n = el(video ? "video" : "img");
  n.src = mediaURL(m.path, run);
  if (video) {
    n.controls = true;
    n.preload = "metadata";
  } else
    n.alt = `版本画面${m.time != null ? " · " + m.time + " 秒" : ""}${m.crop ? "（局部观察）" : ""}`;
  return n;
}
function mediaList() {
  const all = [
    ...(s.detail?.media || []).filter(
      (m) => m.revision === s.data?.artwork.revision,
    ),
    ...(s.data?.media || []),
  ];
  const paths = new Set();
  return all
    .filter((m) => {
      if (paths.has(m.path)) return false;
      paths.add(m.path);
      return true;
    })
    .map((m) => ({...m, pathtrace: m.pathtrace || s.data?.media?.find((v) => v.path === m.path)?.pathtrace}))
    .sort((a, b) => {
      const cropOrder = Number(!!a.crop) - Number(!!b.crop);
      if (cropOrder || s.data?.artwork.program?.backend !== "pathtrace") return cropOrder;
      return Number(b.pathtrace?.quality === "final") - Number(a.pathtrace?.quality === "final")
        || (b.pathtrace?.export_version || 0) - (a.pathtrace?.export_version || 0)
        || (b.pathtrace?.samples || 0) - (a.pathtrace?.samples || 0);
    });
}
function showMedia() {
  const list = mediaList();
  $("versionPreview").replaceChildren();
  $("mediaStrip").replaceChildren();
  $("openMedia").hidden = true;
  if (!list.length) {
    $("versionPreview").append(
      el(
        "p",
        s.data?.artwork.program
          ? "此版本尚无画面。可点击「渲染此版本」。"
          : "此版本尚未编写绘制程序。",
        "empty-small",
      ),
    );
    return;
  }
  const select = (m, i) => {
    $("versionPreview").replaceChildren(mediaElement(m));
    $("openMedia").href = mediaURL(m.path);
    $("openMedia").hidden = false;
    $("openMedia").textContent = /\.mp4$/i.test(m.path)
      ? "打开视频文件 ↗"
      : "打开原图 ↗";
    $("mediaStrip")
      .querySelectorAll("button")
      .forEach((b, j) => b.setAttribute("aria-pressed", i === j));
    $("previewStatus").textContent = m.pathtrace
      ? `${m.pathtrace.quality === "final" ? "成品" : "预览"} · ${m.pathtrace.mode === "raster" ? "快速构图" : m.pathtrace.samples + " 次采样"}${m.crop ? " · 局部" : ""}`
      : m.crop
      ? "局部观察画面"
      : m.time != null
        ? `实际渲染 · ${m.time} 秒`
        : "实际渲染画面";
  };
  list.forEach((m, i) => {
    const b = el("button", undefined, "frame-button");
    if (!/\.mp4$/i.test(m.path)) {
      const img = el("img");
      img.src = mediaURL(m.path);
      img.alt = "";
      img.loading = "lazy";
      b.append(img);
    }
    b.append(
      el(
        "span",
        /\.mp4$/i.test(m.path)
          ? "播放视频"
          : m.pathtrace ? `${m.pathtrace.quality === "final" ? "成品" : "预览"}${m.crop ? " · 局部" : ""}`
          : `${m.crop ? "局部 · " : ""}${m.time ?? 0}s`,
      ),
    );
    b.onclick = () => select(m, i);
    $("mediaStrip").append(b);
  });
  select(list[0], 0);
}
function codeFiles() {
  if (!s.data) return [];
  if (s.diff) return (s.data.changes?.files || []).map((f) => [f.name, f.diff]);
  return Object.entries(s.data.sources || {}).concat([
    [
      "scene.json",
      JSON.stringify(
        { objects: s.data.artwork.objects, events: s.data.artwork.events },
        null,
        2,
      ),
    ],
  ]);
}
function populateCode() {
  const files = codeFiles(),
    old = $("codeFile").value;
  $("codeFile").replaceChildren(
    ...files.map(([name]) => new Option(name, name)),
  );
  if (files.some(([n]) => n === old)) $("codeFile").value = old;
  renderCode();
}
function renderCode() {
  const content =
    codeFiles().find(([n]) => n === $("codeFile").value)?.[1] ||
    (s.diff ? "此步骤没有代码或场景变更。" : "此版本尚无代码。");
  $("codeContent").replaceChildren();
  for (const line of content.split("\n")) {
    $("codeContent").append(
      el(
        "span",
        line,
        "code-line " +
          (s.diff
            ? line.startsWith("+")
              ? "added"
              : line.startsWith("-")
                ? "removed"
                : line.startsWith("@@")
                  ? "diff-header"
                  : ""
            : ""),
      ),
    );
  }
  $("codeMode").textContent = s.diff ? "变更 diff" : "完整源码";
  $("codeMode").setAttribute("aria-pressed", s.diff);
  $("codeNote").textContent = s.data
    ? `v${s.data.changes.base_revision} → v${s.data.artwork.revision} · ${s.diff ? "仅显示实际变更" : "当前版本源码"}`
    : "尚无代码";
}
function renderLogs() {
  const type = $("logType").value;
  let txt =
    type === "terminal"
      ? s.state?.log || "暂无终端输出"
      : type === "usage"
        ? JSON.stringify(
            { budget: s.state?.budget, usage: s.state?.usage },
            null,
            2,
          )
        : s.detail
          ? (s.detail.events || [])
              .map((e) => JSON.stringify(e, null, 2))
              .join("\n\n")
          : "选择执行步骤以查看原始记录。";
  const filter = $("logSearch").value.toLowerCase();
  if (filter)
    txt = txt
      .split("\n")
      .filter((l) => l.toLowerCase().includes(filter))
      .join("\n");
  $("logContent").textContent = txt;
}
function renderDetail() {
  const art = s.data.artwork,
    step = s.detail;
  $("selectedLabel").textContent = step ? step.title : `版本 v${art.revision}`;
  $("selectedMeta").textContent =
    `v${art.revision} · ${art.program?.backend || "规划中"}`;
  $("runLabel").textContent =
    s.records.find((r) => r.id === s.run)?.title ||
    (art.spec.format === "mp4" ? "视频任务" : "图像任务");
  $("runLabel").title = s.run;
  $("selectionNote").textContent =
    `${s.follow ? "实时跟随" : "历史查看"}${step && step.before_revision !== step.revision ? ` · v${step.before_revision} → v${step.revision}` : ""}`;
  $("intentSource").textContent = step?.explanation_source || "历史快照";
  $("intentSource").hidden = step?.explanation_source === "无文字说明";
  $("stepTitle").textContent = "步骤详情";
  $("intentText").textContent =
    (step?.explanation_source === "无文字说明"
      ? "未记录操作说明"
      : step?.summary) ||
    s.versions.find((v) => v.revision === art.revision)?.summary ||
    "未记录操作说明";
  $("stepOutcome").textContent = step
    ? `状态：${labels[step.status] || step.status}${step.seconds != null ? " · " + step.seconds.toFixed(2) + " 秒" : ""}${step.error ? "\n" + step.error : ""}`
    : "";
  $("versionDetails").textContent = JSON.stringify(
    {
      prompt: art.prompt,
      plan: art.plan,
      video_plan: art.video_plan,
      requirements: art.requirements,
    },
    null,
    2,
  );
  $("outputSpec").textContent =
    `${art.spec.width} × ${art.spec.height} · ${art.spec.format.toUpperCase()}${art.spec.format === "mp4" ? ` · ${art.spec.duration} 秒 · ${art.spec.fps}fps` : ""}`;
  $("assets").replaceChildren();
  for (const a of art.assets || []) {
    $("assets").append(el("p", a.id || a.path || "素材"));
    if (a.path && /\.(png|jpe?g|webp)$/i.test(a.path))
      $("assets").append(mediaElement(a));
  }
  if (!art.assets?.length) $("assets").textContent = "暂无素材";
  $("lineage").replaceChildren();
  if (s.data.edit) {
    const e = s.data.edit,
      b = el("button", `源版本 v${e.source_revision}`, "quiet");
    b.title = e.source_run;
    b.onclick = () => loadRun(e.source_run, e.source_revision);
    $("lineage").append(b);
  }
  const prior = $("compareRevision").value;
  $("compareRevision").replaceChildren();
  if (s.data.edit?.baseline?.length)
    $("compareRevision").add(new Option("编辑起点（原作品）", "baseline"));
  for (const v of s.versions.filter(
    (v) => v.revision !== art.revision && v.renderable,
  ))
    $("compareRevision").add(
      new Option(`v${v.revision} · ${v.summary}`, String(v.revision)),
    );
  if ([...$("compareRevision").options].some((o) => o.value === prior))
    $("compareRevision").value = prior;
  else if (
    [...$("compareRevision").options].some(
      (o) => o.value === String(art.revision - 1),
    )
  )
    $("compareRevision").value = String(art.revision - 1);
  $("compareToggle").disabled = !$("compareRevision").options.length;
  showMedia();
  populateCode();
  renderLogs();
  renderNav();
  controls();
}
async function selectStep(step, follow = false) {
  s.follow = follow;
  s.selecting = true;
  controls();
  $("selectionNote").textContent = "加载中…";
  const generation = s.generation,
    seq = ++s.selection;
  try {
    const detail = await api(url("/api/step", { id: step.id }));
    const data = await api(
      url("/api/version", {
        revision: detail.revision,
        base: detail.before_revision,
      }),
    );
    if (generation !== s.generation || seq !== s.selection) return;
    s.detail = detail;
    s.data = data;
    s.selecting = false;
    renderDetail();
    afterSelection();
  } catch (e) {
    if (seq === s.selection) {
      error(e);
      $("selectionNote").textContent = "读取失败，请重新选择。";
    }
  } finally {
    if (seq === s.selection) {
      s.selecting = false;
      controls();
    }
  }
}
async function selectVersion(revision, follow = false) {
  s.follow = follow;
  s.selecting = true;
  controls();
  $("selectionNote").textContent = "加载中…";
  const generation = s.generation,
    seq = ++s.selection;
  try {
    const data = await api(url("/api/version", { revision }));
    if (generation !== s.generation || seq !== s.selection) return;
    s.detail = null;
    s.data = data;
    s.selecting = false;
    renderDetail();
    afterSelection();
  } catch (e) {
    if (seq === s.selection) {
      error(e);
      $("selectionNote").textContent = "读取失败，请重新选择。";
    }
  } finally {
    if (seq === s.selection) {
      s.selecting = false;
      controls();
    }
  }
}
function afterSelection() {
  if ($("compareToggle").checked) compare();
  else autoPreview();
}
function autoPreview() {
  if (
    $("autoPreview").checked &&
    s.data?.artwork.program &&
    !mediaList().some((m) => !m.crop) &&
    !s.rendering &&
    !s.selecting
  ) {
    const key = s.run + ":" + s.data.artwork.revision;
    if (!s.tried.has(key)) {
      s.tried.add(key);
      renderSelected();
    }
  }
}
async function renderSelected() {
  if (!s.data || s.rendering || s.selecting) return;
  const run = s.run,
    revision = s.data.artwork.revision,
    seq = s.selection;
  s.rendering = true;
  controls();
  $("previewStatus").textContent = `正在渲染 v${revision}…`;
  if (!mediaList().length)
    $("versionPreview").replaceChildren(
      el("p", `正在渲染 v${revision}，画面准备好后自动显示…`, "empty-small"),
    );
  try {
    const p = await preview(run, revision);
    if (run !== s.run || seq !== s.selection) return;
    s.data.media = p.media;
    showMedia();
    const v = s.versions.find((v) => v.revision === revision);
    if (v) v.thumbnail = p.media.find((m) => m.path.endsWith(".png"))?.path;
    renderNav();
  } catch (e) {
    if (run === s.run && seq === s.selection)
      $("previewStatus").textContent = "预览失败：" + e.message;
  } finally {
    s.rendering = false;
    controls();
    if (seq !== s.selection) autoPreview();
  }
}
async function compare() {
  const enabled = $("compareToggle").checked;
  $("comparison").hidden = !enabled;
  $("versionPreview").hidden = enabled;
  $("compareRevision").disabled = !enabled;
  if (!enabled) {
    $("renderProgress").textContent = "";
    return;
  }
  if (!s.data || !$("compareRevision").value) return;
  const seq = s.selection,
    run = s.run,
    revision = s.data.artwork.revision,
    base = $("compareRevision").value,
    baseline = s.data.edit?.baseline;
  $("beforeView").replaceChildren();
  $("afterView").replaceChildren();
  $("renderProgress").textContent = "按相同时间点渲染对比…";
  try {
    const current = await preview(run, revision);
    const previous =
      base === "baseline"
        ? { media: baseline }
        : await preview(run, Number(base));
    if (
      seq !== s.selection ||
      run !== s.run ||
      !$("compareToggle").checked ||
      base !== $("compareRevision").value
    )
      return;
    for (const [target, data] of [
      ["beforeView", previous],
      ["afterView", current],
    ])
      for (const m of data.media) {
        const frame = el("div");
        frame.append(mediaElement(m, run), el("small", `${m.time ?? 0} 秒`));
        $(target).append(frame);
      }
    $("renderProgress").textContent = "同时间点对比";
  } catch (e) {
    if (seq === s.selection)
      $("renderProgress").textContent = "对比未完成：" + e.message;
  }
}
async function poll(generation, initialRevision) {
  if (generation !== s.generation || !s.run) return;
  try {
    const [state, process] = await Promise.all([
      api(url("/api/state", { compact: 1 })),
      api(url("/api/process")),
    ]);
    if (generation !== s.generation) return;
    const changed =
      s.cursor !== process.cursor || s.state?.revision !== state.revision;
    const ended = s.state?.exit_code === null && state.exit_code !== null;
    s.state = state;
    s.cursor = process.cursor;
    s.steps = process.steps;
    const status = state.status?.status;
    $("status").textContent = labels[status] || status || "初始化";
    renderMonitor();
    if (changed || ended || !s.versions.length) {
      const v = await api(url("/api/versions"));
      if (generation !== s.generation) return;
      s.versions = v.versions;
      renderNav();
    }
    if (initialRevision != null) {
      await selectVersion(initialRevision);
    } else if (s.follow && (changed || !s.data)) {
      const latest = s.steps.at(-1);
      if (latest) await selectStep(latest, true);
      else if (state.revision != null)
        await selectVersion(state.revision, true);
    }
    controls();
    renderLogs();
    clearError();
    if (running() || state.revision == null)
      timer = setTimeout(() => poll(generation), 1200);
    else refreshHistory().catch(error);
  } catch (e) {
    if (generation !== s.generation) return;
    error(e);
    timer = setTimeout(() => poll(generation, initialRevision), 2500);
  }
}
async function loadRun(id, revision) {
  clearTimeout(timer);
  s.generation++;
  s.selection++;
  s.run = id;
  s.steps = [];
  s.versions = [];
  s.data = null;
  s.selecting = false;
  s.detail = null;
  s.state = null;
  s.cursor = null;
  s.follow = true;
  s.limit = 120;
  if (!id && s.mode !== "history") setNav("steps");
  clearError();
  $("intentText").textContent = "选择步骤查看详情。";
  $("stepTitle").textContent = "等待任务";
  $("intentSource").textContent = "步骤详情";
  $("intentSource").hidden = false;
  $("stepOutcome").textContent = "";
  $("versionDetails").textContent = "尚无记录";
  $("assets").replaceChildren();
  $("lineage").replaceChildren();
  $("codeContent").textContent = "尚无代码";
  $("codeFile").replaceChildren();
  $("logContent").textContent = "尚无记录";
  $("outputSpec").textContent = "尚无输出";
  $("selectedMeta").textContent = "";
  $("runLabel").textContent = "预览";
  $("selectionNote").textContent = "";
  $("status").textContent = id ? "读取中" : "未选择";
  $("activity").textContent = id ? "读取任务" : "准备创作";
  $("compareToggle").checked = false;
  compare();
  $("versionPreview").replaceChildren(
    el("p", id ? "正在读取作品…" : "选择或创建一个任务", "empty-small"),
  );
  $("mediaStrip").replaceChildren();
  $("previewStatus").textContent = "";
  $("openMedia").hidden = true;
  $("selectedLabel").textContent = id ? "读取任务" : "作品预览";
  $("editError").textContent = "";
  renderNav();
  controls();
  renderHistory();
  locationState();
  if (id) await poll(s.generation, revision);
}
async function dispatch(body) {
  if (s.busy) return;
  s.busy = true;
  controls();
  clearError();
  try {
    const limit = $("maxOutputTokens");
    if (limit.value !== "") {
      if (!limit.reportValidity()) throw Error("请输入有效的单次输出 Token 上限");
      body.max_output_tokens = Number(limit.value);
    }
    const result = await api("/api/runs", body);
    await loadRun(result.run_id);
    await refreshHistory();
  } catch (e) {
    error(e);
    $("editError").textContent = e.message;
  } finally {
    s.busy = false;
    controls();
  }
}
$("form").onsubmit = async (e) => {
  e.preventDefault();
  try {
    let image;
    const file = $("inputImage").files[0];
    if (file) {
      if (file.size > 10_000_000) throw Error("参考图应小于 10 MB");
      image = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result.split(",")[1]);
        reader.onerror = reject;
        reader.readAsDataURL(file);
      });
    }
    const body = {
      prompt: $("prompt").value,
      kind: s.mode,
      orientation: $("orientation").value,
      resolution: $("resolution").value,
    };
    if (s.mode === "video") {
      body.duration = Number($("duration").value);
      body.fps = Number($("fps").value);
    }
    if (image) body.image = image;
    await dispatch(body);
  } catch (e) {
    error(e);
  }
};
$("editForm").onsubmit = (e) => {
  e.preventDefault();
  if (s.data && !s.selecting)
    dispatch({
      edit_from: s.run,
      revision: s.data.artwork.revision,
      prompt: $("editPrompt").value,
    });
};
$("resume").onclick = () =>
  dispatch({
    resume: s.run,
    prompt: "",
    kind: s.state?.spec?.format === "mp4" ? "video" : "image",
  });
$("clearTask").onclick = () => loadRun("");
$("history").onchange = () => loadRun($("history").value);
$("refreshHistory").onclick = () => refreshHistory().catch(error);
document
  .querySelectorAll("[data-mode]")
  .forEach((b) => (b.onclick = () => setMode(b.dataset.mode)));
$("stepsTab").onclick = () => setNav("steps");
$("versionsTab").onclick = () => setNav("versions");
$("stepSearch").oninput = renderNav;
$("moreSteps").onclick = () => {
  s.limit += 120;
  renderNav();
};
$("follow").onclick = () => {
  s.follow = true;
  if (s.steps.length) selectStep(s.steps.at(-1), true);
  else if (s.versions.length) selectVersion(s.versions[0].revision, true);
};
$("previewVersion").onclick = renderSelected;
$("autoPreview").onchange = autoPreview;
$("toEdit").onclick = () => {
  setMode("history");
  s.follow = false;
  controls();
  if (s.data) renderDetail();
  $("editPrompt").focus();
};
$("compareToggle").onchange = compare;
$("compareRevision").onchange = compare;
document.querySelectorAll("[data-detail]").forEach(
  (b) =>
    (b.onclick = () => {
      document.querySelectorAll("[data-detail]").forEach((x) => {
        const active = x === b;
        x.setAttribute("aria-selected", active);
        $("detail-" + x.dataset.detail).hidden = !active;
      });
    }),
);
$("codeMode").onclick = () => {
  s.diff = !s.diff;
  populateCode();
};
$("codeFile").onchange = renderCode;
$("logType").onchange = renderLogs;
$("logSearch").oninput = renderLogs;
setMode(s.mode);
controls();
refreshHistory().catch(error);
if (s.run)
  loadRun(s.run, q.has("revision") ? Number(q.get("revision")) : undefined);
