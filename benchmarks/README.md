# MaLiang-Harness 评测指南

在仓库根目录、安装并激活项目环境后执行。公共入口为 `python -m benchmarks`，也可使用 `bash benchmarks/run.sh`。所有模型共用 `batch_eval.py`、`worker.py`、`report.py`；provider 文件仅处理接口差异。

## 1. 配置模型

复制 `benchmarks/models.example.json` 为 `benchmarks/models.local.json`，修改：

- `harness_config`：共享 Harness 配置，相对于模型配置文件。示例指向关闭额外图像生成的快速开始配置。
- `models`：一个或多个模型，分别指定唯一 `id`、`model`、`api`、`base_url`、`api_key_env`。
- `api`：`responses` 或 `chat_completions`，必须与你的服务相符。
- `adapter`：默认 `openai`；需要特殊行为时显式选择下表中的适配器。
- `env_file`：可选，路径相对于模型配置文件；默认从当前进程环境读取密钥。

配置 JSON 不存储密钥值。设置对应环境变量，例如 `export MODEL_API_KEY='YOUR_API_KEY'`。模型名称和网关地址由用户提供，示例不预设任何模型访问权限。

| adapter | 用途及限制 |
| --- | --- |
| `openai` | 标准 ChatOpenAI 传输，支持配置中的两种接口 |
| `kimi_reasoning` | Chat Completions 多轮交互中保留 `reasoning_content` |
| `kimi_k3` | 使用同一 reasoning-history 实现的明确命名入口 |
| `text_only` | 原 DeepSeek V4 Pro Responses 实验的无视觉输入适配；会移除图片并明确告知模型，不代表多模态结果 |

切勿仅为消除报错选择另一个适配器；它会改变模型收到的输入和评测条件。新增 provider 应在 `providers/` 增加适配和离线协议测试，不复制调度引擎。

## 2. 准备任务

最简单的数据格式是 JSONL，一行一个任务。仓库的 `benchmarks/examples/tasks.jsonl` 包含一个图像和一个视频输入任务，没有生成输出。

```json
{"case_id":"poster-01","prompt":"画一张蓝色圆形海报","spec":{"width":512,"height":512,"format":"png"},"allowed_backends":["canvas","svg","scene2d"]}
```

必填：`case_id`、`prompt`、`spec`。`case_id` 必须唯一且适合作为文件名。视频 `spec` 使用 `format: "mp4"`，并指定 `duration` 和 `fps`。

可选字段：`modality`、`allowed_backends`、`workflow_policy`、`allow_generated_assets`、`initial_requirements`、`input_assets`。默认使用 guided 流程并禁用生成素材。未提供 requirements 时加入“满足原始创作要求”的视觉要求。

参考图格式：

```json
{"id":"reference","path":"inputs/reference.png","media_type":"image/png"}
```

将该对象放入任务的 `input_assets` 数组。路径相对于 JSONL 文件目录，不能越出该目录；可选 `sha256` 用于校验已有哈希，否则规划时自动计算。计划阶段会把输入图复制到批次目录，后续执行不依赖原图位置。

`--dataset` 也支持旧版数据集目录，其下含 `image/`、`video/` 的 `tasks.jsonl`、`manifest.jsonl` 及各 case 目录。缺少某种模态时使用 `--modality image` 或 `video`。旧目录格式仍执行原有索引和 baseline 哈希校验。

## 3. 计划、执行和报告

```bash
python -m benchmarks plan --config benchmarks/models.local.json \
  --dataset benchmarks/examples/tasks.jsonl --batch benchmarks/results/my-run
python -m benchmarks run --batch benchmarks/results/my-run --concurrency 1
python -m benchmarks report --batch benchmarks/results/my-run
```

- `plan` 不调用模型，冻结任务、模型配置和源码指纹。批次必须是新目录，可以位于仓库外；示例使用被 Git 忽略的 `benchmarks/results/`。
- `run` 会请求真实 API。每个任务使用独立进程，默认单任务硬超时 2100 秒；可通过 `--timeout` 调整。
- `report` 不调用模型，输出 `report.md` 及 CSV/JSON 汇总。
- 使用 `--models ID ...`、`--cases ID ...`、`--modality image`、`--limit N` 或 `--repeats N` 控制计划范围。
- 源码或配置变化后创建新计划，不修改旧 `plan.json` 绕过指纹校验。

继续未开始的任务：

```bash
python -m benchmarks run --batch benchmarks/results/my-run --resume
```

`--resume` 跳过已经尝试过的任务，包括失败。只有显式追加 `--retry-failed` 才会为失败任务创建新的、可能再次计费的尝试。历史输出不会作为新任务的解题输入。

报告区分完成、失败、预算耗尽和技术有效性；它不是独立的人类视觉质量评分。比较无视觉适配器、不同预算或不同生成能力时，应报告这些条件差异。

## 无 API 自检与测试

```bash
python -m benchmarks.self_check
python -m pytest benchmarks/tests
```

自检使用确定性模型，通过同一 worker 渲染并生成报告所需记录，不读取密钥文件、不调用 API；默认写入被 Git 忽略的 `benchmarks/results/`。可用 `--output /tmp/maliang-offline-check` 指定一个不存在的临时目录。自检输出不是研究数据。

## 历史脚本

原 `bench/code` 的固定模型/日期批次、人工重跑、分析脚本及专用配置保存在本地研究归档 `experiments/2026_09/`，不随公开仓库发布。它们依赖未发布的历史数据，不能代替这里的公共入口。旧计划绑定了旧源码与路径，不能直接续跑；要使用新引擎须重新规划。
