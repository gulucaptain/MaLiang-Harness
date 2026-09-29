import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { api, query } from "./api";
import { ChatList } from "./components/chat-list";
import { PromptForm } from "./components/prompt-form";
import { useAtBottom } from "./hooks/use-at-bottom";
import "./style.css";

async function readTurn(id, signal, revision = null) {
  const [state, process] = await Promise.all([
    api(query("/api/state", id, { compact: 1 }), undefined, signal),
    api(query("/api/process", id), undefined, signal),
  ]);
  const version =
    state.revision == null
      ? null
      : await api(
          query("/api/version", id, { revision: revision ?? state.revision }),
          undefined,
          signal,
        );
  return {
    id,
    state,
    steps: (process.steps || []).filter(
      (step) => revision == null || step.revision <= revision,
    ),
    version,
    prompt: version?.edit?.instruction || state.prompt,
  };
}
function App() {
  const [selected, setSelected] = useState(
    new URLSearchParams(location.search).get("run") || "",
  );
  const [records, setRecords] = useState([]),
    [turns, setTurns] = useState([]),
    [input, setInput] = useState("");
  const [kind, setKind] = useState("image"),
    [reference, setReference] = useState(null),
    [error, setError] = useState("");
  const [sending, setSending] = useState(false),
    [editing, setEditing] = useState(null),
    [sidebar, setSidebar] = useState(false);
  const [orientation, setOrientation] = useState("auto"),
    [duration, setDuration] = useState("3");
  const [loading, setLoading] = useState(false);
  const [deleting, setDeleting] = useState("");
  const atBottom = useAtBottom(180),
    bottom = useRef(null),
    follow = useRef(true);
  follow.current = atBottom;
  const last = turns.at(-1),
    busy = sending || last?.state?.exit_code === null;
  const editContext =
    editing ||
    (selected && last?.version?.artwork?.revision != null && !busy
      ? { run: selected, revision: last.version.artwork.revision }
      : null);
  async function refreshHistory() {
    const data = await api("/api/runs");
    setRecords(data.records || []);
  }
  useEffect(() => {
    refreshHistory().catch((e) => setError(e.message));
    const timer = setInterval(() => refreshHistory().catch(() => {}), 5000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    history.replaceState(
      null,
      "",
      selected ? "/?run=" + encodeURIComponent(selected) : "/",
    );
    if (!selected) {
      setTurns([]);
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    let timer,
      first = true;
    setLoading(true);
    async function poll() {
      try {
        const current = await readTurn(selected, controller.signal);
        if (first) {
          const historyTurns = [current],
            seen = new Set([selected]);
          let parent = current.version?.edit;
          while (
            parent?.source_run &&
            !seen.has(parent.source_run) &&
            historyTurns.length < 50
          ) {
            seen.add(parent.source_run);
            let previous;
            try {
              previous = await readTurn(
                parent.source_run,
                controller.signal,
                parent.source_revision,
              );
            } catch (error) {
              if (error.status === 404) break;
              throw error;
            }
            historyTurns.unshift(previous);
            parent = previous.version?.edit;
          }
          if (controller.signal.aborted) return;
          setTurns((previous) =>
            historyTurns.map((turn) => ({
              ...turn,
              prompt:
                turn.prompt ||
                previous.find((item) => item.id === turn.id)?.prompt,
            })),
          );
          setKind(current.state.spec?.format === "mp4" ? "video" : "image");
          first = false;
          requestAnimationFrame(() =>
            bottom.current?.scrollIntoView({ behavior: "instant" }),
          );
        } else {
          if (controller.signal.aborted) return;
          setTurns((old) => [...old.filter((t) => t.id !== selected), current]);
        }
        setLoading(false);
        if (follow.current)
          requestAnimationFrame(() =>
            bottom.current?.scrollIntoView({ behavior: "smooth" }),
          );
        if (current.state.exit_code === null) timer = setTimeout(poll, 1400);
      } catch (e) {
        if (!controller.signal.aborted) {
          setError(e.message);
          setLoading(false);
          timer = setTimeout(poll, 3000);
        }
      }
    }
    poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [selected]);
  function newChat() {
    setSelected("");
    setTurns([]);
    setEditing(null);
    setReference(null);
    setInput("");
    setError("");
    setSidebar(false);
  }
  async function deleteRecord(record) {
    if (
      deleting ||
      !window.confirm(
        `确定删除「${record.title}」？\n\n将永久删除这条创作的本地文件，包括图片、视频、版本记录、日志和参考图。此操作无法撤销，后续编辑创作会保留。`,
      )
    )
      return;
    setDeleting(record.id);
    setError("");
    try {
      await api("/api/runs/delete", { run: record.id, confirmed: true });
      setRecords((old) => old.filter((item) => item.id !== record.id));
      if (selected === record.id) newChat();
      else setTurns((old) => old.filter((turn) => turn.id !== record.id));
    } catch (error) {
      setError(error.message);
    } finally {
      setDeleting("");
    }
  }
  function select(id) {
    setSelected(id);
    setTurns([]);
    setEditing(null);
    setReference(null);
    setInput("");
    setError("");
    setSidebar(false);
  }
  async function upload(file) {
    if (!file) return;
    if (
      !["image/png", "image/jpeg", "image/webp"].includes(file.type) ||
      file.size > 10000000
    ) {
      setError("请选择小于 10 MB 的 PNG、JPEG 或 WebP 图像。");
      return;
    }
    const reader = new FileReader();
    reader.onload = () =>
      setReference({
        name: file.name,
        url: reader.result,
        data: String(reader.result).split(",")[1],
      });
    reader.onerror = () => setError("参考图读取失败");
    reader.readAsDataURL(file);
  }
  async function submit(event) {
    event.preventDefault();
    if (busy || loading || !input.trim()) return;
    setSending(true);
    setError("");
    const prompt = input.trim();
    const body = editContext
      ? {
          kind,
          prompt,
          edit_from: editContext.run,
          revision: editContext.revision,
        }
      : {
          kind,
          prompt,
          orientation,
          resolution: "profile",
          ...(kind === "video" ? { duration: Number(duration), fps: 12 } : {}),
          ...(reference ? { image: reference.data } : {}),
        };
    try {
      const response = await api("/api/runs", body);
      setTurns((old) => [
        ...(editContext ? old : []),
        { id: response.run_id, prompt, state: { exit_code: null }, steps: [] },
      ]);
      setSelected(response.run_id);
      setInput("");
      setReference(null);
      setEditing(null);
      refreshHistory().catch(() => {});
      requestAnimationFrame(() =>
        bottom.current?.scrollIntoView({ behavior: "smooth" }),
      );
    } catch (e) {
      setError(e.message);
    } finally {
      setSending(false);
    }
  }
  async function resume(turn) {
    setSending(true);
    setError("");
    try {
      await api("/api/runs", { resume: turn.id, kind, prompt: "" });
      setSelected("");
      setTimeout(() => setSelected(turn.id), 0);
    } catch (e) {
      setError(e.message);
    } finally {
      setSending(false);
    }
  }
  const suggestions =
    kind === "image"
      ? [
          "一只拿着画笔的蓝绿色变色龙，柔和的工作室光线",
          "设计一张极简山峰海报，米白色背景，橙色太阳",
          "绘制一个温暖的早餐厨房，充满可爱的细节",
        ]
      : [
          "一轮橙色太阳缓缓越过山峰，三秒循环动画",
          "蓝色小球沿着优雅的曲线移动，留下一串光点",
          "一片叶子缓缓飘落，构图简洁，动作轻盈",
        ];
  return (
    <div className="app-shell">
      {sidebar && (
        <button
          className="sidebar-overlay"
          aria-label="关闭会话列表"
          onClick={() => setSidebar(false)}
        />
      )}
      <aside className={"sidebar " + (sidebar ? "open" : "")}>
        <a className="brand" href="/">
          <img src="/assets/logo.png" alt="" />
          <span>
            MaLiang<span className="brand-suffix">Harness</span>
          </span>
        </a>
        <button className="new-chat" onClick={newChat}>
          ＋ <span>新建创作</span>
          <kbd>New</kbd>
        </button>
        <div className="sidebar-label">你的创作</div>
        <nav aria-label="创作历史">
          {records.length ? (
            records.map((record) => (
              <div className="history-row" key={record.id}>
                <button
                  className={selected === record.id ? "selected" : ""}
                  onClick={() => select(record.id)}
                >
                  <span className="history-icon">
                    {record.kind === "video" ? "▷" : "▧"}
                  </span>
                  <span>{record.title}</span>
                  {record.is_edit && <small>编辑</small>}
                </button>
                <button
                  className="delete-record"
                  aria-label={`删除创作：${record.title}`}
                  title="删除创作"
                  disabled={!!deleting}
                  onClick={() => deleteRecord(record)}
                >
                  {deleting === record.id ? "…" : "×"}
                </button>
              </div>
            ))
          ) : (
            <p className="history-empty">第一份作品，从一个想法开始。</p>
          )}
        </nav>
        <div className="sidebar-bottom">
          <a href="/studio">打开高级工作台 ↗</a>
          <div>
            <span className="local-dot" /> 本地创作空间
          </div>
        </div>
      </aside>
      <main className="main">
        <header className="topbar">
          <button
            className="mobile-menu icon-button"
            aria-label="打开会话列表"
            onClick={() => setSidebar(true)}
          >
            ☰
          </button>
          <span>
            MaLiang-Harness <small>创作空间</small>
          </span>
        </header>
        <div className={"conversation " + (!selected ? "empty" : "")}>
          {!selected ? (
            <section className="welcome">
              <div className="welcome-logo">
                <img src="/assets/logo.png" alt="MaLiang logo" />
              </div>
              <div className="eyebrow">MAKE SOMETHING ONLY YOU CAN IMAGINE</div>
              <h1>让想象，成为画面。</h1>
              <p>从一句描述开始，创作图像与视频。</p>
              <div className="suggestions">
                {suggestions.map((s, i) => (
                  <button key={s} onClick={() => setInput(s)}>
                    <span>{["✧", "◈", "◌"][i]}</span>
                    {s}
                    <b>↗</b>
                  </button>
                ))}
              </div>
            </section>
          ) : (
            <>
              <ChatList
                turns={turns}
                onEdit={(value) => {
                  setEditing(value);
                  document.querySelector("textarea")?.focus();
                }}
                onResume={resume}
              />
              {loading && (
                <p className="loading" role="status">
                  正在读取创作记录…
                </p>
              )}
            </>
          )}
        </div>
        <div ref={bottom} />
        <div className="composer-dock">
          {!atBottom && selected && (
            <button
              className="scroll-bottom"
              onClick={() =>
                bottom.current?.scrollIntoView({ behavior: "smooth" })
              }
              aria-label="滚动到最新回复"
            >
              ↓
            </button>
          )}
          {error && (
            <div className="error" role="alert">
              {error}
              <button aria-label="关闭错误提示" onClick={() => setError("")}>
                ×
              </button>
            </div>
          )}
          {!editContext && (
            <div className="creation-options">
              <label>
                画面方向{" "}
                <select
                  value={orientation}
                  onChange={(e) => setOrientation(e.target.value)}
                  disabled={busy}
                >
                  <option value="auto">自动</option>
                  <option value="square">方形</option>
                  <option value="landscape">横版</option>
                  <option value="portrait">竖版</option>
                </select>
              </label>
              {kind === "video" && (
                <label>
                  时长{" "}
                  <select
                    value={duration}
                    onChange={(e) => setDuration(e.target.value)}
                    disabled={busy}
                  >
                    <option value="3">3 秒</option>
                    <option value="5">5 秒</option>
                    <option value="10">10 秒</option>
                  </select>
                </label>
              )}
            </div>
          )}
          <PromptForm
            input={input}
            setInput={setInput}
            busy={busy || loading}
            onSubmit={submit}
            kind={kind}
            setKind={setKind}
            reference={reference}
            onFile={upload}
            removeFile={() => setReference(null)}
            editing={editContext}
            onNew={newChat}
          />
          <p className="composer-note">
            {busy
              ? "任务正在后台执行；切换页面不会取消任务。"
              : "图像与视频由 MaLiang-Harness 生成 · 结果请以实际预览为准"}
          </p>
        </div>
      </main>
    </div>
  );
}
createRoot(document.getElementById("root")).render(<App />);
