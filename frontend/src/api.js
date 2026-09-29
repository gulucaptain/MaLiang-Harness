export async function api(path, body, signal) {
  const response = await fetch(path, {
    signal,
    ...(body === undefined
      ? {}
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }),
  });
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error || `请求失败 (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return data;
}
export const query = (path, run, extra = {}) =>
  path + "?" + new URLSearchParams({ run, ...extra });
export const mediaURL = (run, path) => query("/media", run, { path });
export const labels = {
  completed: "已完成",
  baseline_exported: "已导出",
  draft: "草稿",
  working: "创作中",
  running: "创作中",
  failed: "未完成",
  interrupted: "已中断",
  incomplete: "待完善",
  budget_exhausted: "预算已用尽",
};
