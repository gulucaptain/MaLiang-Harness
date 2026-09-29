# 可运行示例

先按根目录 README 安装项目及 Chromium，以下命令均从仓库根目录执行。输出目录必须是新目录；示例只创建自己的输出，不读取旧实验结果。

| 示例 | 命令 | 额外依赖 |
| --- | --- | --- |
| 对象级二维场景 | `python examples/scene_demo.py --project runs/scene-demo` | Chromium |
| 原生笔触绘画 | `python examples/paint_demo.py runs/paint-demo` | libmypaint |
| 路径追踪 | `python examples/pathtrace_demo.py --project runs/pathtrace-demo --size 256` | Chromium / 可用的图形后端 |
| 内置离线端到端演示 | `maliang-harness demo runs/offline-demo` | Chromium |

这些演示不调用真实 LLM 或图像生成 API。路径追踪示例的参数以 `--help` 为准。

`pond_task.json`、`scene_video_task.json`、`svg_task.json` 是任务规格示例；`budget.json`、`guided_budget.json` 是预算示例；`ink_pond.js` 是绘图程序示例。

`harness.quickstart.json` 用于快速开始和公共评测，关闭额外图像生成、使用较小的图像画布。真实推理时通过 `--model` 选择你可访问的模型。

`src/maliang/demo.py` 与 `scene_demo.py` 是离线脚本模型的内部实现，由 CLI、测试和这里的示例共享，不是另一套用户启动入口。
