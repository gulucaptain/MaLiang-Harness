#!/usr/bin/env bash
# 在任何目录执行均可；默认读取项目根目录的 harness.json。
# ./inference.sh --check                         仅检查配置，不调用 API
# ./inference.sh --prompt '你的创作要求'          自定义图像任务
# ./inference.sh --spec examples/pond_task.json   三秒水墨视频任务
# ./inference.sh --resume --project runs/任务目录 断点续跑
set -euo pipefail
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"
if [[ ! -x .venv/bin/python ]]; then
    echo '未找到 .venv/bin/python，请先按 README.md 安装项目环境。' >&2
    exit 1
fi
exec .venv/bin/python inference.py "$@"
