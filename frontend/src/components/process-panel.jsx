import React, { useState } from "react";
import { api, query, mediaURL } from "../api";
function ProcessStep({ step, run }) {
  const [detail, setDetail] = useState(null),
    [error, setError] = useState("");
  async function expand(event) {
    if (event.target.open && !detail) {
      try {
        setDetail(await api(query("/api/step", run, { id: step.id })));
      } catch (e) {
        setError(e.message);
      }
    }
  }
  return (
    <details className="process-step" onToggle={expand}>
      <summary>
        <span className={"step-dot " + step.status} />
        <span>{step.title}</span>
        <small>
          {step.status === "running"
            ? "进行中"
            : step.status === "error"
              ? "失败"
              : "完成"}
        </small>
      </summary>
      <p className="step-summary">
        {detail?.summary || step.summary || "此步骤没有额外的文字说明。"}
      </p>
      {detail?.media?.map((m, i) => (
        <a
          key={i}
          href={mediaURL(run, m.path)}
          target="_blank"
          rel="noreferrer"
        >
          {m.path.endsWith(".mp4") ? (
            "查看视频预览"
          ) : (
            <img
              className="step-image"
              src={mediaURL(run, m.path)}
              alt="步骤预览"
            />
          )}
        </a>
      ))}
      {detail?.events && (
        <details className="raw-events">
          <summary>查看工具记录</summary>
          <pre>{JSON.stringify(detail.events, null, 2)}</pre>
        </details>
      )}
      {error && <p role="alert">{error}</p>}
    </details>
  );
}
export function ProcessPanel({ steps = [], run, busy }) {
  return (
    <details className="process-panel">
      <summary>
        {busy ? (
          <span className="spinner" />
        ) : (
          <span className="process-symbol">✧</span>
        )}
        <span>{busy ? "正在创作" : "创作过程"}</span>
        <small>
          {steps.length ? `${steps.length} 个步骤` : "等待任务初始化"}
        </small>
        <span className="chevron">⌄</span>
      </summary>
      <div className="process-body">
        <p className="process-note">
          模型公开输出的说明、工具调用和执行结果。这里不展示或推测未公开的内部思维。
        </p>
        {steps.map((step) => (
          <ProcessStep key={step.id} step={step} run={run} />
        ))}
        {!steps.length && <p className="muted">尚无执行记录。</p>}
      </div>
    </details>
  );
}
