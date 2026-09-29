// Adapted from Vercel ai-chatbot components/chat-message.tsx (Apache-2.0).
// Harness replies carry artifacts and observable steps, rather than a text-only completion.
import React from "react";
import { mediaURL, labels } from "../api";
import { ProcessPanel } from "./process-panel";
export function ChatMessage({ turn, onEdit, onResume, isCurrent }) {
  const { id, state, steps, version, prompt } = turn;
  const busy = state?.exit_code === null,
    status = state?.status?.status;
  const seen = new Set();
  const artifacts = (version?.media || []).filter((m) => {
    if (seen.has(m.path)) return false;
    seen.add(m.path);
    return true;
  });
  const exported = artifacts.filter((m) => m.kind === "export");
  const visible = (exported.length ? exported : artifacts).slice(0, 4);
  return (
    <section className="chat-turn">
      <div className="user-message">
        <div>{prompt || state?.prompt || "正在读取创作要求…"}</div>
      </div>
      <div className="assistant-message">
        <div className="assistant-heading">
          <img src="/assets/logo.png" alt="" />
          <strong>MaLiang</strong>
          <span>创作助手</span>
        </div>
        <ProcessPanel steps={steps} run={id} busy={busy} />
        {visible.length ? (
          <div className="artifact-grid">
            {visible.map((m) => (
              <figure key={m.path}>
                {m.path.toLowerCase().endsWith(".mp4") ? (
                  <video
                    controls
                    playsInline
                    preload="metadata"
                    src={mediaURL(id, m.path)}
                    aria-label="生成的视频"
                  />
                ) : (
                  <a
                    href={mediaURL(id, m.path)}
                    target="_blank"
                    rel="noreferrer"
                  >
                    <img src={mediaURL(id, m.path)} alt="生成的作品" />
                  </a>
                )}
                <figcaption>
                  <span>
                    {m.kind === "export"
                      ? status === "completed"
                        ? "生成结果"
                        : "导出作品"
                      : "过程预览"}{" "}
                    · v{version?.artwork?.revision}
                  </span>
                  <a href={mediaURL(id, m.path)} download>
                    下载 ↓
                  </a>
                </figcaption>
              </figure>
            ))}
          </div>
        ) : (
          <div className={"artifact-placeholder " + (busy ? "active" : "")}>
            <span>✧</span>
            <strong>
              {busy
                ? "正在把你的想法变成画面"
                : status === "completed"
                  ? "暂无可显示的作品"
                  : "尚未生成可展示的作品"}
            </strong>
            <p>
              {busy
                ? "作品预览将在这里出现，你可以展开上方过程查看进展。"
                : "可展开创作过程查看详情，或在工作台中检查版本。"}
            </p>
          </div>
        )}
        <div className="result-meta">
          <span className={busy ? "live" : ""}>
            {busy ? "创作中" : labels[status] || "初始化"}
          </span>
          {state?.spec && (
            <span>
              {state.spec.width} × {state.spec.height}
              {state.spec.format === "mp4" ? ` · ${state.spec.duration}s` : ""}
            </span>
          )}
          {state?.usage?.model_calls > 0 && (
            <span>{state.usage.model_calls} 次模型调用</span>
          )}
        </div>
        {state?.status?.reason && !busy && status !== "completed" && (
          <p className="result-reason">{state.status.reason}</p>
        )}
        {isCurrent && !busy && (
          <div className="result-actions">
            {version?.artwork?.revision != null && (
              <button
                onClick={() =>
                  onEdit({ run: id, revision: version.artwork.revision })
                }
              >
                继续修改这版
              </button>
            )}
            {state?.can_resume && (
              <button onClick={() => onResume(turn)}>恢复任务</button>
            )}
            <a href={"/studio?run=" + encodeURIComponent(id) + "&mode=history"}>
              版本与代码 ↗
            </a>
          </div>
        )}
        {state?.resume_block_reason && (
          <p className="result-reason">{state.resume_block_reason}</p>
        )}
      </div>
    </section>
  );
}
