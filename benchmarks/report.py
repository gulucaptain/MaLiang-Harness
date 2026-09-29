"""Summaries measure completion/efficiency, not independent visual quality."""

import csv
from collections import defaultdict
from pathlib import Path
from statistics import mean

from .common import read, write, write_jsonl


def write_csv(path, rows, fields):
    with Path(path).open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def generate_report(batch):
    batch = Path(batch)
    plan = read(batch / "plan.json")
    if not plan:
        raise ValueError("Missing plan.json")
    details, all_attempts, reviews = [], [], []
    for row in plan["jobs"]:
        folder = (batch / row["job_path"]).parent
        attempts = sorted(folder.glob("attempt-[0-9][0-9][0-9]"))
        results = []
        for attempt in attempts:
            result = read(attempt / "result.json", {"outcome": "unfinished"})
            result = dict(
                result,
                job_id=row["job_id"],
                model_id=row["model_id"],
                case_id=row["case_id"],
                modality=row["modality"],
                repeat=row["repeat"],
                attempt_dir=str(attempt.relative_to(batch)),
            )
            all_attempts.append(result)
            results.append(result)
        latest = results[-1] if results else {"outcome": "pending"}
        usage = latest.get("usage", {})
        item = {k: row[k] for k in ("job_id", "model_id", "modality", "case_id", "repeat", "baseline_path")}
        item.update(
            baseline_available=bool(row.get("baseline_path")),
            outcome=latest["outcome"],
            first_outcome=results[0]["outcome"] if results else "pending",
            attempts=len(results),
            output_path=latest.get("output_path"),
            wall_seconds=latest.get("wall_seconds"),
            all_attempt_wall_seconds=sum(r.get("wall_seconds", 0) for r in results),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            model_calls=usage.get("model_calls"),
            tool_calls=usage.get("tool_calls"),
            asset_api_calls=usage.get("asset_api_calls"),
            all_attempt_input_tokens=sum(r.get("usage", {}).get("input_tokens", 0) for r in results),
            all_attempt_output_tokens=sum(r.get("usage", {}).get("output_tokens", 0) for r in results),
        )
        details.append(item)
        reviews.append(
            dict(
                job_id=row["job_id"],
                baseline_path=row["baseline_path"],
                baseline_available=bool(row.get("baseline_path")),
                candidate_path=str(batch / latest["output_path"]) if latest.get("output_path") else "",
                attempt_dir=latest.get("attempt_dir", ""),
                outcome=latest["outcome"],
                prompt_path=str(batch / row["job_path"]),
                brief_fulfillment_1_to_5="",
                visual_quality_1_to_5="",
                motion_consistency_1_to_5="",
                preference_candidate_tie_baseline="",
                reviewer="",
                notes="",
            )
        )
    groups = defaultdict(list)
    for row in details:
        groups[row["model_id"], row["modality"]].append(row)
    summary = []
    for (model, kind), rows in sorted(groups.items()):
        finished = [r for r in rows if r["outcome"] not in {"pending", "unfinished"}]
        attempted = [r for r in rows if r["attempts"]]
        passed = sum(r["outcome"] == "success" for r in rows)
        first = sum(r["first_outcome"] == "success" for r in rows)
        summary.append(
            dict(
                model_id=model,
                modality=kind,
                planned=len(rows),
                attempted=len(attempted),
                finished=len(finished),
                success_latest=passed,
                success_first=first,
                success_rate_latest_over_planned=passed / len(rows),
                success_rate_first_over_planned=first / len(rows),
                mean_latest_wall_seconds=mean(
                    [r["wall_seconds"] for r in finished if r["wall_seconds"] is not None]
                )
                if any(r["wall_seconds"] is not None for r in finished)
                else None,
                total_attempt_wall_seconds=sum(r["all_attempt_wall_seconds"] for r in rows),
                total_input_tokens=sum(r["all_attempt_input_tokens"] for r in rows),
                total_output_tokens=sum(r["all_attempt_output_tokens"] for r in rows),
                usage_recorded_jobs=sum(r["input_tokens"] is not None for r in rows),
            )
        )
    write_jsonl(batch / "results.jsonl", details)
    write_jsonl(batch / "attempts.jsonl", all_attempts)
    write(batch / "summary.json", summary)
    if details:
        write_csv(batch / "results.csv", details, list(details[0]))
    if summary:
        write_csv(batch / "summary.csv", summary, list(summary[0]))
    # Generated template only; never overwrite the user's completed ratings.csv.
    if reviews:
        write_csv(batch / "review_template.csv", reviews, list(reviews[0]))
    md = [
        "# Harness 多模型批测",
        "",
        "成功 = completed + 当前版本验证通过 + 输出可完整解码；不是独立画质评分。",
        "首次与最新尝试分别报告；失败重试不会覆盖首次结果。分母包含所有计划任务，未执行任务也会影响显示的比例。",
        "历史 GPT 是外部基线，Harness 版本/素材随机性可能不同，不并入本次受控运行成功率。",
        "baseline_available=false 表示新题尚无历史GPT输出；可以评绝对质量，暂不能填写相对GPT的偏好。",
        "",
        "| 模型 | 类型 | 已结束 / 计划 | 首次成功 | 最新成功 | 最新成功率（含未执行） | 平均耗时 s |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in summary:
        wall = f"{r['mean_latest_wall_seconds']:.1f}" if r["mean_latest_wall_seconds"] is not None else "—"
        md.append(
            f"| {r['model_id']} | {r['modality']} | {r['finished']} / {r['planned']} | {r['success_first']} | {r['success_latest']} | {r['success_rate_latest_over_planned']:.1%} | {wall} |"
        )
    md += [
        "",
        "画质对照：复制 review_template.csv 为 ratings.csv 后填写。report 不会覆盖 ratings.csv，也不把空评分自动当成零分。",
        "results.jsonl/CSV 是每条任务的最新尝试；attempts.jsonl 保留每次尝试；累计 token 和耗时包含重试。缺少用量记录的任务不推算费用。",
        "provider 返回的 token 计量可能不同；这里没有按 token 单价计算费用，也没有自动评判画面质量。",
        "",
    ]
    (batch / "report.md").write_text("\n".join(md))
