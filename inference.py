"""MaLiang-Harness 推理入口：加载配置、创建任务、执行 Harness 并报告结果。"""

import argparse
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4


def main():
    # 与 shell 入口保持一致：相对路径始终以项目目录为基准。
    os.chdir(Path(__file__).resolve().parent)
    parser = argparse.ArgumentParser(
        description="MaLiang-Harness 真实 LLM 推理入口；相对路径以项目目录为基准"
    )
    task = parser.add_mutually_exclusive_group()
    task.add_argument("--prompt", help="自然语言创作要求")
    task.add_argument("--prompt-file", help="UTF-8 创作要求文件")
    task.add_argument("--spec", help="完整任务 JSON，例如 examples/pond_task.json")
    parser.add_argument(
        "--task", choices=["image", "video", "paint", "pathtrace"], help="选择图像、视频、笔触绘画或写实渲染配置；默认 image"
    )
    parser.add_argument("--config", default="harness.json", help="Harness 运行配置 JSON")
    parser.add_argument("--env", help="凭据配置文件；默认仅读取 .env，不读取示例模板")
    parser.add_argument("--model", help="覆盖 harness.json 中的模型名称")
    parser.add_argument("--project", help="运行目录；默认创建唯一 runs/inference-* 目录")
    parser.add_argument("--edit-from", help="从已有任务创建独立的代码编辑任务")
    parser.add_argument("--revision", type=int, help="编辑源任务的历史版本号")
    parser.add_argument("--resume", action="store_true", help="继续已有任务，须指定 --project")
    parser.add_argument("--check", action="store_true", help="仅验证本地配置和依赖，不调用 API")
    parser.add_argument(
        "--check-api", action="store_true", help="发送一次最小模型请求，验证 API 连接及模型权限"
    )
    parser.add_argument("--backends", help="覆盖默认允许的绘制后端，逗号分隔")
    parser.add_argument("--format", choices=["png", "mp4"])
    parser.add_argument("--orientation", choices=["auto", "portrait", "landscape", "square"], default="auto")
    parser.add_argument("--resolution", choices=["profile", "standard", "high"], default="profile")
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--input-image", help="PNG/JPEG/WebP reference or compositing image")
    parser.add_argument("--fps", type=int)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--allow-generated-assets", action="store_true", help="无 spec 时允许图像生成素材")
    parser.add_argument("--mode", choices=["maliang", "generic-agent", "single-shot"])
    parser.add_argument("--budget", help="覆盖 harness.json 的累计预算 JSON")
    parser.add_argument("--max-output-tokens", type=int, help="单次模型输出 Token 上限")
    args = parser.parse_args()
    if args.max_output_tokens is not None and not 1 <= args.max_output_tokens <= 1000000:
        parser.error("单次输出上限须为 1–1000000")
    if args.edit_from:
        if args.revision is None or args.revision < 0 or not (args.prompt or args.prompt_file):
            parser.error("编辑须提供 --revision 和 --prompt/--prompt-file")
        if any(
            (
                args.resume,
                args.spec,
                args.task,
                args.input_image,
                args.backends,
                args.format,
                args.width is not None,
                args.height is not None,
                args.duration is not None,
                args.fps is not None,
                args.seed is not None,
                args.allow_generated_assets,
                args.orientation != "auto",
                args.resolution != "profile",
                args.mode not in (None, "maliang"),
            )
        ):
            parser.error("编辑继承源版本的规格、后端和素材，使用 maliang 模式；不能混用新建或续跑选项")
    elif args.revision is not None:
        parser.error("--revision 需要 --edit-from")
    if args.resume and not args.project:
        parser.error("--resume 必须指定 --project")
    if args.resume and any((args.prompt, args.prompt_file, args.spec, args.input_image)):
        parser.error("续跑沿用原任务；不要同时指定新 prompt 或 spec")
    if (args.resume or args.spec) and (
        args.orientation != "auto"
        or args.resolution != "profile"
        or args.width is not None
        or args.height is not None
    ):
        parser.error("已有任务 / spec 使用原始尺寸，请勿同时指定尺寸选项")
    if args.resume and args.task:
        parser.error("续跑沿用原任务类型；不要再次指定 --task")
    if args.spec and args.input_image:
        parser.error("完整 spec 请在 assets 中声明图像，不同时指定 --input-image")
    if args.resume and (args.duration is not None or args.fps is not None):
        parser.error("续跑保留原时长和帧率")
    if args.spec and args.task:
        parser.error("--spec 已包含输出类型；不要同时指定 --task")
    if args.task and args.format and args.format != ("mp4" if args.task == "video" else "png"):
        parser.error("--format 与 --task 指定的类型不一致")
    if args.check and args.check_api:
        parser.error("--check 与 --check-api 只能选择一个")

    from maliang.models import Artwork, Budget
    from maliang.settings import load_harness_settings

    harness_path = Path(args.config).resolve()
    settings = load_harness_settings(harness_path)
    mode = "maliang" if args.edit_from else args.mode or settings.mode
    kind = args.task or ("video" if args.format == "mp4" else "image")
    profile = settings.profile(kind)
    default_output = profile.output
    from maliang.resolution import resolve_output

    sizing_prompt = (
        Path(args.prompt_file).read_text(encoding="utf-8")
        if args.prompt_file
        else args.prompt or profile.prompt
    )
    output = resolve_output(
        default_output,
        sizing_prompt,
        orientation=args.orientation,
        resolution=args.resolution,
        width=args.width,
        height=args.height,
        duration=args.duration if args.duration is not None else default_output.duration,
        fps=args.fps if args.fps is not None else default_output.fps,
        format=args.format or default_output.format,
        seed=args.seed if args.seed is not None else default_output.seed,
    )
    budget = (
        Budget.model_validate_json(Path(args.budget).read_text(encoding="utf-8"))
        if args.budget
        else settings.budget
    )
    print(
        f"已读取 Harness 配置：{harness_path}（累计输入 token 上限：{budget.max_input_tokens}）", flush=True
    )
    if not args.spec and not args.resume and not args.edit_from:
        print(f"任务配置：{kind} → {output.width}×{output.height} {output.format.upper()}", flush=True)

    # 只解析已知配置项，不执行配置文件中的 shell 命令，也不输出密钥。
    # 已有非空环境变量优先于文件；--model 最后覆盖。
    keys = {
        "OPENAI_API_KEY",
        "MALIANG_MODEL",
        "OPENAI_BASE_URL",
        "MALIANG_IMAGE_MODEL",
        "MALIANG_CHROMIUM",
        "MALIANG_MYPAINT_LIBRARY",
        "DASHSCOPE_API_KEY",
        settings.image_generation.api_key_env,
    }
    env_config = (
        Path(args.env)
        if args.env
        else (Path(".env") if Path(".env").is_file() else None)
    )
    if env_config is not None:
        if not env_config.is_file():
            raise ValueError("指定的配置文件不存在")
        for number, raw in enumerate(env_config.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            key, separator, value = line.partition("=")
            key = key.strip()
            if not separator or key not in keys:
                continue
            try:
                parsed = " ".join(shlex.split(value, comments=True, posix=True))
            except ValueError:
                raise ValueError(f"配置文件第 {number} 行引号不匹配（值已隐藏）") from None
            if parsed and not os.environ.get(key):
                os.environ[key] = parsed
        print(f"已读取密钥配置：{env_config}（不显示配置值）", flush=True)
    for key in ("OPENAI_BASE_URL", "MALIANG_IMAGE_MODEL", "MALIANG_CHROMIUM"):
        if not os.environ.get(key, "").strip():
            os.environ.pop(key, None)
    os.environ["MALIANG_MODEL"] = args.model or settings.model.name
    for key in ("OPENAI_API_KEY", "MALIANG_MODEL"):
        if not os.environ.get(key, "").strip():
            raise ValueError(f"请在 .env 或环境变量中配置 {key}")

    if kind == "pathtrace":
        from maliang.pathtrace import engine_status

        status = engine_status()
        if not status["available"]:
            raise ValueError(status["message"])

    if kind == "paint":
        from maliang.paint_native import require_library

        require_library()

    from playwright.sync_api import sync_playwright

    if args.spec:
        Artwork.model_validate_json(Path(args.spec).read_text(encoding="utf-8"))
    if args.prompt_file and not Path(args.prompt_file).is_file():
        raise ValueError("prompt 文件不存在")
    with sync_playwright() as pw:
        executable = os.environ.get("MALIANG_CHROMIUM")
        if executable:
            if not Path(executable).is_file():
                raise ValueError("MALIANG_CHROMIUM 指向的文件不存在")
        else:
            # 以已安装完整 Chromium 为默认可执行文件，避免仅缺少 headless shell 导致失败。
            executable = pw.chromium.executable_path
            if not Path(executable).is_file():
                raise ValueError("缺少 Chromium；请运行 .venv/bin/python -m playwright install chromium")
            os.environ["MALIANG_CHROMIUM"] = executable

    command = [sys.executable, "-m", "maliang.cli"]
    subprocess.run([*command, "doctor"], check=True)
    if args.check:
        print("本地配置与依赖检查通过；尚未请求 API，未验证密钥有效性。")
        return 0
    if args.check_api:
        from maliang.agent import create_model
        from maliang.api_diagnostics import explain_api_error

        try:
            create_model(
                os.environ["MALIANG_MODEL"],
                timeout=min(15, settings.model.timeout_seconds),
                max_retries=0,
                max_tokens=settings.model.max_output_tokens_per_call,
            ).invoke("Reply with OK.")
        except Exception as exc:
            print(f"API 检查失败：{explain_api_error(exc) or type(exc).__name__}", file=sys.stderr)
            return 1
        print(f"API 检查通过：模型 {os.environ['MALIANG_MODEL']} 已完成一次最小请求。")
        return 0

    project = Path(
        args.project or f"runs/inference-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"
    ).resolve()
    if args.edit_from:
        from maliang.editing import create_edit
        from maliang.store import ProjectStore

        if project.exists() and any(project.iterdir()):
            raise ValueError("编辑须使用新的空目录")
        source_path = Path(args.edit_from).resolve()
        if not (source_path / "artwork.json").is_file():
            raise ValueError("源任务不存在")
        create_edit(ProjectStore(source_path), ProjectStore(project), args.revision, sizing_prompt)
    elif args.resume:
        if not (project / "artwork.json").is_file():
            raise ValueError("指定目录没有可恢复的 artwork.json")
    else:
        if project.exists() and any(project.iterdir()):
            raise ValueError("运行目录非空；请使用新目录，或指定 --resume")
        init = [
            *command,
            "init",
            str(project),
            "--harness-config",
            str(harness_path),
            "--workflow",
            "guided" if mode == "maliang" else "legacy",
        ]
        if args.input_image:
            init += ["--input-image", str(Path(args.input_image).resolve())]
        if args.spec:
            init += ["--spec", str(Path(args.spec).resolve())]
        else:
            init += [
                "--backends",
                args.backends or ",".join(profile.allowed_backends),
                "--format",
                output.format,
                "--width",
                str(output.width),
                "--height",
                str(output.height),
                "--duration",
                str(output.duration),
                "--fps",
                str(output.fps),
                "--seed",
                str(output.seed),
            ]
            if args.allow_generated_assets or profile.allow_generated_assets:
                init += ["--allow-generated-assets"]
            if args.prompt_file:
                init += ["--prompt-file", str(Path(args.prompt_file).resolve())]
            else:
                init += [
                    "--prompt",
                    args.prompt or profile.prompt,
                ]
        subprocess.run(init, check=True)

    print(f"\n任务目录：{project}\n开始调用真实 LLM，可能产生模型及工具费用。", flush=True)
    run = [
        *command,
        "run",
        str(project),
        "--mode",
        mode,
        "--model",
        os.environ["MALIANG_MODEL"],
        "--harness-config",
        str(harness_path),
    ]
    if args.max_output_tokens is not None:
        run += ["--max-output-tokens", str(args.max_output_tokens)]
    if args.budget:
        run += ["--budget", str(Path(args.budget).resolve())]
    if args.resume:
        run += ["--resume"]
    result = subprocess.run(run, check=False)
    if (project / "status.json").is_file():
        subprocess.run([*command, "report", str(project)], check=False)
        state = json.loads((project / "status.json").read_text())
        if state.get("export_id"):
            evidence = json.loads((project / "evidence" / f"{state['export_id']}.json").read_text())
            for path in evidence["paths"]:
                print(f"输出文件：{(project / path).resolve()}")
        print(f"运行状态：{state.get('status')}\n过程记录：{project / 'trace.jsonl'}")
        if result.returncode == 0 and state.get("status") in {"completed", "baseline_exported"}:
            return 0
        report_path = project / "validation.json"
        if report_path.exists():
            report = json.loads(report_path.read_text())
            for requirement in report.get("requirements", []):
                if requirement["hard"] and requirement["verdict"] != "pass":
                    print(f"待解决：{requirement['id']} ({requirement['verdict']}) — {requirement['detail']}")
        if state.get("status") == "draft":
            print(f"已保存草稿，尚未通过交付检查：{state.get('reason', '')}", file=sys.stderr)
        elif state.get("status") == "budget_exhausted":
            print("累计预算已耗尽；增加相应预算后可 --resume。", file=sys.stderr)
        else:
            print(
                f"任务尚未交付：{state.get('reason', state.get('status'))}。请查看 trace.jsonl。",
                file=sys.stderr,
            )
    else:
        print("运行未写入状态，请检查上述启动错误。", file=sys.stderr)
    return result.returncode or 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n运行已中断；可通过 --resume 继续。", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        # 不打印潜在包含配置值的第三方异常详情。
        if isinstance(exc, ValueError) and type(exc) is ValueError:
            print(f"入口错误：{exc}", file=sys.stderr)
        else:
            print(
                f"入口检查或执行失败（{type(exc).__name__}）。请检查文件格式、依赖及运行报告。",
                file=sys.stderr,
            )
        sys.exit(1)
