from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .miniagent import write_json
from .runner import run_agent
from .storage import Storage
from .task_agent import run_task_turn
from .task_runtime import diff_scripts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="短视频脚本 Agent（校招项目 demo）")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="生成并检查一次短视频脚本")
    run.add_argument("--input", required=True, help="请求 JSON")
    run.add_argument("--data-dir", default="data", help="合成素材与品牌规则目录")
    run.add_argument("--state-dir", default=".runs/latest", help="状态与输出目录")
    run.add_argument("--engine", choices=["offline", "live", "opencode-local"], default="offline")
    run.add_argument("--provider", default="opencode-go")
    run.add_argument("--model", default="deepseek-v4.1-flash")
    run.add_argument("--cli-path")
    run.add_argument("--max-context-chars", type=int, default=14000)
    run.add_argument("--now", help="保留参数，便于统一 CLI")
    run.add_argument("--fault", action="append", default=[], help="注入工具失败，如 estimate_duration:2")
    run.add_argument("--max-steps", type=int, default=14)
    run.add_argument("--max-tool-calls", type=int, default=24)
    run.add_argument("--run-id")
    run.add_argument("--output", help="输出 JSON 路径，默认 state-dir/output.json")
    run.add_argument("--trace", help="Trace JSON 路径，默认 state-dir/trace.json")

    task = sub.add_parser("task", help="多轮任务：创建、对话、历史、对比、审核")
    task_sub = task.add_subparsers(dest="task_command")

    create = task_sub.add_parser("create", help="创建任务并生成第一版")
    create.add_argument("--input", required=True, help="请求 JSON")
    create.add_argument("--db", default=".runs/agent.db")
    create.add_argument("--data-dir", default="data")
    create.add_argument("--engine", choices=["offline", "live"], default="offline")
    create.add_argument("--model")

    chat = task_sub.add_parser("chat", help="进入交互式改稿")
    chat.add_argument("task_id")
    chat.add_argument("--db", default=".runs/agent.db")
    chat.add_argument("--data-dir", default="data")
    chat.add_argument("--engine", choices=["offline", "live"], default="offline")
    chat.add_argument("--model")

    show = task_sub.add_parser("show", help="查看任务和当前版本")
    show.add_argument("task_id")
    show.add_argument("--db", default=".runs/agent.db")

    history = task_sub.add_parser("history", help="查看版本历史")
    history.add_argument("task_id")
    history.add_argument("--db", default=".runs/agent.db")

    diff = task_sub.add_parser("diff", help="比较两个版本")
    diff.add_argument("task_id")
    diff.add_argument("--from", dest="from_version", type=int, required=True)
    diff.add_argument("--to", dest="to_version", type=int, required=True)
    diff.add_argument("--db", default=".runs/agent.db")
    diff.add_argument("--json", action="store_true")

    approve = task_sub.add_parser("approve", help="批准指定版本")
    approve.add_argument("task_id")
    approve.add_argument("--version", type=int, required=True)
    approve.add_argument("--db", default=".runs/agent.db")

    reject = task_sub.add_parser("reject", help="驳回指定版本")
    reject.add_argument("task_id")
    reject.add_argument("--version", type=int, required=True)
    reject.add_argument("--reason", default="")
    reject.add_argument("--db", default=".runs/agent.db")

    rollback = task_sub.add_parser("rollback", help="切换到历史候选版本")
    rollback.add_argument("task_id")
    rollback.add_argument("--version", type=int, required=True)
    rollback.add_argument("--db", default=".runs/agent.db")

    export = task_sub.add_parser("export", help="导出分镜表")
    export.add_argument("task_id")
    export.add_argument("--version", type=int, required=True)
    export.add_argument("--format", choices=["md", "json"], default="md")
    export.add_argument("--out")
    export.add_argument("--db", default=".runs/agent.db")

    listing = task_sub.add_parser("list", help="列出任务")
    listing.add_argument("--status")
    listing.add_argument("--db", default=".runs/agent.db")

    return parser


def _store(path: str) -> Storage:
    return Storage(path)


def _version_by_no(store: Storage, task_id: str, version_no: int) -> dict[str, Any] | None:
    return next(
        (item for item in store.list_versions(task_id) if item["version_no"] == version_no),
        None,
    )


def approve_version(
    store: Storage,
    task_id: str,
    version_no: int,
    reason: str = "",
) -> dict[str, Any]:
    version = _version_by_no(store, task_id, version_no)
    if version is None:
        raise ValueError(f"版本 v{version_no} 不存在")
    if version["status"] not in {"validated", "approved"}:
        raise ValueError(f"v{version_no} 当前状态是 {version['status']}，不能批准")
    existing = store.get_approval(task_id, version["script_id"])
    if existing and existing["decision"] == "rejected":
        raise ValueError(f"v{version_no} 已被驳回，不能直接批准")
    approval = store.add_approval(task_id, version["script_id"], "approved", reason)
    store.update_version_status(version["script_id"], "approved")
    store.update_task(task_id, current_script_id=version["script_id"], status="approved")
    return approval


def reject_version(
    store: Storage,
    task_id: str,
    version_no: int,
    reason: str = "",
) -> dict[str, Any]:
    version = _version_by_no(store, task_id, version_no)
    if version is None:
        raise ValueError(f"版本 v{version_no} 不存在")
    if version["status"] not in {"validated", "approved"}:
        raise ValueError(f"v{version_no} 当前状态是 {version['status']}，不能驳回")
    existing = store.get_approval(task_id, version["script_id"])
    if existing and existing["decision"] == "approved":
        raise ValueError(f"v{version_no} 已批准，审核决定已记录")
    approval = store.add_approval(task_id, version["script_id"], "rejected", reason)
    store.update_version_status(version["script_id"], "rejected")
    task = store.get_task(task_id) or {}
    if task.get("current_script_id") == version["script_id"]:
        candidates = [
            item
            for item in store.list_versions(task_id)
            if item["version_no"] < version_no and item["status"] in {"validated", "approved"}
        ]
        fallback = candidates[-1] if candidates else None
        if fallback:
            store.restore_candidate(task_id, fallback["script_id"])
        else:
            store.update_task(task_id, current_script_id=None, status="new")
    return approval


def rollback_version(store: Storage, task_id: str, version_no: int) -> dict[str, Any]:
    version = _version_by_no(store, task_id, version_no)
    if version is None:
        raise ValueError(f"版本 v{version_no} 不存在")
    return store.restore_candidate(task_id, version["script_id"])


def export_version(store: Storage, task_id: str, version_no: int) -> dict[str, Any]:
    version = _version_by_no(store, task_id, version_no)
    if version is None:
        raise ValueError(f"版本 v{version_no} 不存在")
    return version


def _format_history(store: Storage, task_id: str) -> str:
    lines = ["版本\t时长\t状态\t改动\tscript_id"]
    for item in store.list_versions(task_id):
        lines.append(
            f"v{item['version_no']}\t{item['total_duration_sec']}s\t{item['status']}\t"
            f"{item.get('change_summary') or ''}\t{item['script_id']}"
        )
    return "\n".join(lines)


def _format_show(store: Storage, task_id: str) -> str:
    task = store.get_task(task_id)
    if task is None:
        raise ValueError(f"任务 {task_id} 不存在")
    lines = [f"任务 {task_id}  状态={task['status']}", f"请求：{json.dumps(task['request'], ensure_ascii=False)}"]
    version = store.get_version(task["current_script_id"]) if task.get("current_script_id") else None
    if version is None:
        lines.append("当前没有版本")
        return "\n".join(lines)
    content = version["content"]
    estimate = store.latest_estimate(version["script_id"])
    check = store.latest_rule_check(version["script_id"])
    lines.append(
        f"当前 v{version['version_no']}：{version['total_duration_sec']} 秒，"
        f"目标 {content.get('target_duration_sec')} 秒，模板 {version['template']}，"
        f"状态 {version['status']}"
    )
    if estimate:
        lines.append(f"估算：within_target={estimate['within_target']}")
    if check:
        lines.append(f"规则：passed={check['passed']} warnings={check['warnings']}")
    lines.append("分镜：")
    for scene in content.get("scenes", []):
        lines.append(
            f"  {scene['scene']}. {scene['duration_sec']}s  {scene['narration']}  "
            f"claim={scene.get('claim')} sources={scene.get('source_ids')}"
        )
    return "\n".join(lines)


def _format_diff(diff: dict[str, Any]) -> str:
    lines = [
        f"v{diff['from_version']} -> v{diff['to_version']}，"
        f"时长变化 {diff['duration_delta_sec']} 秒，目标变化 {diff['target_delta_sec']} 秒"
    ]
    for item in diff["scenes"]:
        if item["change"] == "modified":
            fields = ", ".join(item["fields"].keys())
            lines.append(f"  分镜 {item['scene']} 修改：{fields}")
        elif item["change"] == "added":
            lines.append(f"  分镜 {item['scene']} 新增")
        elif item["change"] == "removed":
            lines.append(f"  分镜 {item['scene']} 删除")
    if not diff["scenes"]:
        lines.append("  没有分镜变化")
    return "\n".join(lines)


def _export_markdown(store: Storage, task_id: str, version_no: int) -> str:
    version = export_version(store, task_id, version_no)
    content = version["content"]
    check = store.latest_rule_check(version["script_id"])
    lines = [
        f"# {content.get('title') or '短视频脚本'}",
        "",
        f"- 版本：v{version['version_no']}",
        f"- 总时长：{version['total_duration_sec']} 秒",
        f"- 目标时长：{content.get('target_duration_sec')} 秒",
        f"- 模板：{version['template']}",
        f"- 规则检查：{'通过' if check and check.get('passed') else '未通过'}",
        "",
        "| # | 时长 | 旁白 | 声明 | 来源 |",
        "| ---: | ---: | --- | --- | --- |",
    ]
    for scene in content.get("scenes", []):
        claim = scene.get("claim") or ""
        sources = ", ".join(scene.get("source_ids") or [])
        lines.append(
            f"| {scene['scene']} | {scene['duration_sec']} | {scene['narration']} | {claim} | {sources} |"
        )
    return "\n".join(lines)


def _task_main(args: argparse.Namespace) -> int:
    command = getattr(args, "task_command", None)
    if not command:
        print("用法：run.py task {create,chat,show,history,diff,approve,reject,rollback,export,list}", file=sys.stderr)
        return 2
    store = _store(args.db)
    try:
        if command == "create":
            request = json.loads(Path(args.input).read_text(encoding="utf-8"))
            task = store.create_task(request)
            result = run_task_turn(
                store,
                task["id"],
                "生成第一版",
                engine_name=args.engine,
                data_dir=args.data_dir,
                force_intent="create",
                model=args.model,
            )
            print(f"task_id: {task['id']}")
            print(result["assistant_text"])
            print(_format_history(store, task["id"]))
            return 0
        if command == "chat":
            _chat(store, args)
            return 0
        if command == "show":
            print(_format_show(store, args.task_id))
            return 0
        if command == "history":
            print(_format_history(store, args.task_id))
            return 0
        if command == "diff":
            versions = {item["version_no"]: item for item in store.list_versions(args.task_id)}
            if args.from_version not in versions or args.to_version not in versions:
                print("版本不存在", file=sys.stderr)
                return 1
            diff = diff_scripts(versions[args.from_version], versions[args.to_version])
            print(json.dumps(diff, ensure_ascii=False, indent=2) if args.json else _format_diff(diff))
            return 0
        if command == "approve":
            approval = approve_version(store, args.task_id, args.version)
            print(f"已批准 v{args.version}：{approval['id']}")
            return 0
        if command == "reject":
            approval = reject_version(store, args.task_id, args.version, args.reason)
            print(f"已驳回 v{args.version}：{approval['id']}")
            return 0
        if command == "rollback":
            version = rollback_version(store, args.task_id, args.version)
            print(f"已回退到 v{version['version_no']}")
            return 0
        if command == "export":
            if args.format == "json":
                payload = export_version(store, args.task_id, args.version)
                text = json.dumps(payload, ensure_ascii=False, indent=2)
            else:
                text = _export_markdown(store, args.task_id, args.version)
            if args.out:
                Path(args.out).write_text(text + "\n", encoding="utf-8")
                print(f"已写入 {args.out}")
            else:
                print(text)
            return 0
        if command == "list":
            for task in store.list_tasks(args.status):
                current = store.get_version(task["current_script_id"]) if task.get("current_script_id") else None
                label = f"v{current['version_no']}" if current else "-"
                print(f"{task['id']}\t{task['status']}\t{label}\t{task['updated_at']}")
            return 0
    except Exception as exc:
        print(f"任务命令失败：{exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
    return 2


def _chat(store: Storage, args: argparse.Namespace) -> None:
    task_id = args.task_id
    print(f"任务 {task_id}，输入自然语言改稿；/help 查看命令，/quit 退出。")
    while True:
        try:
            line = input("> ").strip()
        except EOFError:
            break
        if not line:
            continue
        if line in {"/quit", "/exit"}:
            break
        if line == "/help":
            print("/show /history /diff <from> <to> /approve <n> /reject <n> [原因] /rollback <n> /export <n> /quit")
            continue
        if line.startswith("/"):
            parts = line.split()
            command = parts[0]
            try:
                if command == "/show":
                    print(_format_show(store, task_id))
                elif command == "/history":
                    print(_format_history(store, task_id))
                elif command == "/diff" and len(parts) == 3:
                    versions = {item["version_no"]: item for item in store.list_versions(task_id)}
                    diff = diff_scripts(versions[int(parts[1])], versions[int(parts[2])])
                    print(_format_diff(diff))
                elif command == "/approve" and len(parts) == 2:
                    approval = approve_version(store, task_id, int(parts[1]))
                    print(f"已批准 v{parts[1]}：{approval['id']}")
                elif command == "/reject" and len(parts) >= 2:
                    reason = " ".join(parts[2:])
                    approval = reject_version(store, task_id, int(parts[1]), reason)
                    print(f"已驳回 v{parts[1]}：{approval['id']}")
                elif command == "/rollback" and len(parts) == 2:
                    version = rollback_version(store, task_id, int(parts[1]))
                    print(f"已回退到 v{version['version_no']}")
                elif command == "/export" and len(parts) == 2:
                    print(_export_markdown(store, task_id, int(parts[1])))
                else:
                    print("未知命令或参数不对，输入 /help 查看。")
            except Exception as exc:
                print(f"命令失败：{exc}")
            continue
        result = run_task_turn(
            store,
            task_id,
            line,
            engine_name=args.engine,
            data_dir=args.data_dir,
            model=args.model,
        )
        print(result["assistant_text"])


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "task":
        return _task_main(args)
    if args.command != "run":
        parser.print_help()
        return 2
    try:
        output, trace = run_agent(
            input_path=args.input,
            data_dir=args.data_dir,
            state_dir=args.state_dir,
            engine_name=args.engine,
            now=args.now,
            faults=args.fault,
            max_steps=args.max_steps,
            max_tool_calls=args.max_tool_calls,
            run_id=args.run_id,
            provider=args.provider,
            model=args.model,
            cli_path=args.cli_path,
            max_context_chars=args.max_context_chars,
        )
    except Exception as exc:
        print(f"运行失败：{exc}", file=sys.stderr)
        return 1

    output_path = Path(args.output) if args.output else Path(args.state_dir) / "output.json"
    trace_path = Path(args.trace) if args.trace else Path(args.state_dir) / "trace.json"
    write_json(output_path, output)
    write_json(trace_path, trace)
    print(
        json.dumps(
            {
                "status": output["status"],
                "task_status": output["task_status"],
                "title": output["title"],
                "estimated_total_sec": output["estimated_total_sec"],
                "target_duration_sec": output["target_duration_sec"],
                "duration_ok": output["duration_ok"],
                "rule_passed": (output.get("rule_check") or {}).get("passed"),
                "script": output["script"],
                "output": str(output_path),
                "trace": str(trace_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if output["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
