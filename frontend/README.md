# MaLiang 对话创作界面

参考并改编 `assets/ai-chatbot` 的 React 对话组件。Python 服务仍负责模型调用、任务状态和历史数据，不使用原模板的 Next.js 服务、Vercel KV、登录系统或浏览器密钥存储。

## 使用

在仓库根目录运行 `python web.py --port 7860`，访问首页。`/studio` 保留原来的高级工作台。构建产物已放入 `web/chat/`，普通用户不需要安装 Node.js。

## 开发

```bash
cd frontend
npm ci
npm run build
```

构建输出为 `web/chat/app.js` 和 `app.css`；修改源代码后重新构建并刷新页面。Node.js 18+。不要直接修改构建后的压缩文件。

## 对话与过程

- 主回复展示生成的图片或视频，支持预览、下载和继续修改。
- 提交后的任务由 Python 后台执行；切换页面不会取消生成。
- 后续要求通过 `edit_from` 和 `revision` 创建新的编辑任务，保留版本与父任务关系。
- 恢复中断任务使用 `resume`；不等同于重新生成。
- “创作过程”展示 `/api/process` 和 `/api/step` 中实际记录的公开模型说明、工具调用、状态和预览。
- 本地参考版 ai-chatbot 没有 thinking/reasoning 专用渲染，它只把 `message.content` 显示为 Markdown。当前 UI 不获取、不推测或宣称展示隐藏思维；provider 可能返回的 reasoning 字段也没有自动接入。
- 原 Harness 以完成的模型轮次/工具事件更新记录，当前采用轮询展示，不是 token 级 thinking 流。
- 笔触绘画、路径追踪、任意历史版本和代码对比继续使用高级工作台。

Apache-2.0 上游归属见 `NOTICE` 与 `LICENSE.ai-chatbot`。React 许可证见 `LICENSE.react`。原始参考目录不作为运行依赖。
