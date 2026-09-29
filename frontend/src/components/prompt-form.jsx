// Adapted from Vercel ai-chatbot components/prompt-form.tsx (Apache-2.0).
import React, { useRef } from "react";
export function PromptForm({
  input,
  setInput,
  busy,
  onSubmit,
  kind,
  setKind,
  reference,
  onFile,
  removeFile,
  editing,
  onNew,
}) {
  const textarea = useRef(null);
  const file = useRef(null);
  return (
    <form className="composer" onSubmit={onSubmit}>
      {editing && (
        <div className="edit-context">
          基于 v{editing.revision} 继续修改{" "}
          <button type="button" onClick={onNew}>
            改为新作品
          </button>
        </div>
      )}
      {reference && (
        <div className="attachment">
          <img src={reference.url} alt="参考图" />
          <span>{reference.name}</span>
          <button type="button" onClick={removeFile} aria-label="移除参考图">
            ×
          </button>
        </div>
      )}
      <textarea
        ref={textarea}
        rows={2}
        aria-label="创作要求"
        placeholder={
          editing ? "告诉我你想调整哪里…" : "描述你想创作的图像或视频…"
        }
        value={input}
        onChange={(event) => {
          setInput(event.target.value);
          event.target.style.height = "auto";
          event.target.style.height =
            Math.min(event.target.scrollHeight, 180) + "px";
        }}
        onKeyDown={(event) => {
          if (
            event.key === "Enter" &&
            !event.shiftKey &&
            !event.nativeEvent.isComposing &&
            !busy
          ) {
            event.preventDefault();
            event.currentTarget.form.requestSubmit();
          }
        }}
      />
      <div className="composer-toolbar">
        <input
          ref={file}
          type="file"
          hidden
          accept="image/png,image/jpeg,image/webp"
          onChange={(event) => {
            onFile(event.target.files?.[0]);
            event.target.value = "";
          }}
        />
        <button
          className="icon-button"
          type="button"
          aria-label="添加参考图"
          title="添加参考图"
          disabled={busy || !!editing}
          onClick={() => file.current.click()}
        >
          ＋
        </button>
        <div className="mode-picker" aria-label="作品类型">
          {["image", "video"].map((mode) => (
            <button
              type="button"
              key={mode}
              aria-pressed={kind === mode}
              disabled={busy || !!editing}
              onClick={() => setKind(mode)}
            >
              {mode === "image" ? "▧ 图像" : "▷ 视频"}
            </button>
          ))}
        </div>
        <button
          className="send-button"
          type="submit"
          aria-label="发送创作要求"
          disabled={busy || !input.trim()}
        >
          {busy ? <span className="spinner" /> : "↑"}
        </button>
      </div>
    </form>
  );
}
