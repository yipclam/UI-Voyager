#!/usr/bin/env python3
"""Run UI-Voyager on HW-GUI-VLA through an existing physical ADB device."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Iterable
import urllib.request

from PIL import Image


ROOT = Path(__file__).resolve().parent
ANDROIDWORLD = ROOT / "androidworld"
sys.path.insert(0, str(ANDROIDWORLD))

from android_world.env import json_action  # noqa: E402
from eval.agents.qwen_agent import QwenAgent  # noqa: E402
from eval.benchmarks.hw_gui_vla import (  # noqa: E402
    APP_PACKAGES,
    EXCLUDED_APPS,
    TaskRecord,
    load_tasks,
)
from eval.clients.openai_client import OpenAIClient  # noqa: E402
from eval.envs.physical_adb_env import PhysicalAdbEnv  # noqa: E402


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _append_jsonl(path: Path, value: Any) -> None:
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(value, ensure_ascii=False) + "\n")


def _adb_prefix(adb: Path, adb_server_port: int) -> list[str]:
    prefix = [str(adb)]
    if not os.environ.get("ADB_SERVER_SOCKET"):
        prefix.extend(["-P", str(adb_server_port)])
    return prefix


def _adb(
    adb: Path,
    serial: str,
    adb_server_port: int,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*_adb_prefix(adb, adb_server_port), "-s", serial, *args],
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=120,
    )


def _list_devices(adb: Path, adb_server_port: int) -> list[str]:
    result = subprocess.run(
        [*_adb_prefix(adb, adb_server_port), "devices"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
    )
    devices = []
    for line in result.stdout.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "device":
            devices.append(fields[0])
    return devices


def _resolve_device(adb: Path, adb_server_port: int, requested: str | None) -> str:
    devices = _list_devices(adb, adb_server_port)
    if requested:
        if requested not in devices:
            raise RuntimeError(
                f"Requested device {requested!r} is not online; found {devices}."
            )
        return requested
    if len(devices) != 1:
        raise RuntimeError(
            f"Expected exactly one online ADB device, found {devices}; pass --device."
        )
    return devices[0]


def _restart_app(
    adb: Path, serial: str, adb_server_port: int, package: str
) -> None:
    _adb(adb, serial, adb_server_port, "shell", "am", "force-stop", package)
    _adb(
        adb,
        serial,
        adb_server_port,
        "shell", "monkey", "-p", package,
        "-c", "android.intent.category.LAUNCHER", "1",
    )


def _check_api(base_url: str, model: str, api_key: str) -> None:
    root = base_url.rstrip("/")
    endpoint = root + "/models" if root.endswith("/v1") else root + "/v1/models"
    request = urllib.request.Request(
        endpoint, headers={"Authorization": f"Bearer {api_key}"}
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.load(response)
    names = {entry.get("id") for entry in payload.get("data", [])}
    if model not in names:
        raise RuntimeError(f"Model {model!r} is not exposed by {endpoint}: {names}")


def _check_device(
    adb: Path,
    serial: str,
    adb_server_port: int,
    packages: Iterable[str],
) -> None:
    state = _adb(adb, serial, adb_server_port, "get-state").stdout.strip()
    if state != "device":
        raise RuntimeError(f"ADB device {serial!r} state is {state!r}.")
    installed = {
        line.removeprefix("package:").strip()
        for line in _adb(
            adb, serial, adb_server_port, "shell", "pm", "list", "packages"
        ).stdout.splitlines()
        if line.strip()
    }
    missing = sorted(set(packages) - installed)
    if missing:
        raise RuntimeError(f"Missing required app packages: {missing}")
    if "com.android.adbkeyboard" not in installed:
        raise RuntimeError(
            "ADB Keyboard (com.android.adbkeyboard) is required for UTF-8 input."
        )


def _check_infrastructure(
    args: argparse.Namespace,
    device: str,
    package: str,
) -> None:
    """Fail fast when a task error is caused by shared runtime infrastructure."""
    _check_device(args.adb, device, args.adb_server_port, {package})
    _check_api(args.base_url, args.model, args.api_key)


def _git_revision() -> dict[str, Any]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
        stdout=subprocess.PIPE, text=True,
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT, check=True,
            stdout=subprocess.PIPE, text=True,
        ).stdout.strip()
    )
    return {"commit": commit, "dirty": dirty}


def _manifest(args: argparse.Namespace, tasks: list[TaskRecord], device: str) -> dict:
    prompt = ANDROIDWORLD / "eval" / "prompts" / "qwen3vl_instruct.md"
    return {
        "schema_version": 1,
        "agent": "ui-voyager",
        "repository": _git_revision(),
        "dataset": str(args.workbook.resolve()),
        "dataset_sha256": _sha256(args.workbook),
        "excluded_apps": sorted(EXCLUDED_APPS),
        "step_budget": "ceil(expected_steps * 1.25)",
        "task_count": len(tasks),
        "primary_metric_task_count": sum(t.count_in_primary_metric for t in tasks),
        "runtime": {
            "device": device,
            "adb": str(args.adb.resolve()),
            "adb_server_socket_set": bool(os.environ.get("ADB_SERVER_SOCKET")),
            "base_url": args.base_url,
            "model": args.model,
            "temperature": args.temperature,
            "top_p": args.top_p,
            "max_tokens": args.max_tokens,
            "history_len": args.history_len,
            "n_history_image": 0,
            "wait_after_action_seconds": args.wait_after_action,
            "task_timeout_s": args.task_timeout,
            "prompt": str(prompt.relative_to(ROOT)),
            "prompt_sha256": _sha256(prompt),
        },
        "tasks": [asdict(task) for task in tasks],
    }


def _progress(run_dir: Path, tasks: list[TaskRecord]) -> dict[str, Any]:
    statuses: dict[str, int] = {}
    completed = 0
    for task in tasks:
        result_path = run_dir / "tasks" / task.key / "result.json"
        if not result_path.is_file():
            continue
        completed += 1
        status = json.loads(result_path.read_text()).get("status", "unknown")
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "expected": len(tasks),
        "completed": completed,
        "remaining": len(tasks) - completed,
        "statuses": statuses,
        "updated_at": _utcnow(),
    }


def _archive_attempt(task_dir: Path, previous_status: str) -> None:
    archive_root = task_dir / "attempts"
    archive_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = archive_root / f"{timestamp}-{previous_status}"
    suffix = 1
    while destination.exists():
        destination = archive_root / f"{timestamp}-{previous_status}-{suffix}"
        suffix += 1
    destination.mkdir()
    for child in list(task_dir.iterdir()):
        if child != archive_root:
            shutil.move(str(child), str(destination / child.name))


def _save_image(path: Path, pixels: Any) -> None:
    if pixels is None:
        return
    Image.fromarray(pixels).save(path)


def _serializable_step(step_id: int, data: dict[str, Any], wall: float) -> dict:
    action = data.get("action")
    action_dict = action.as_dict(skip_none=True) if action is not None else None
    usage = data.get("usage") or {}
    response = data.get("model_response") or ""
    return {
        "step_id": step_id,
        "model_response": response,
        "action": action_dict,
        "parse_valid": "<tool_call>" in response and action is not None,
        "request_latency_s": data.get("request_latency_s"),
        "decision_latency_s": data.get("decision_latency_s"),
        "step_wall_time_s": round(wall, 4),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "cached_tokens": usage.get("cached_tokens"),
        "reasoning_tokens": usage.get("reasoning_tokens"),
        "visual_tokens": usage.get("visual_tokens"),
        "error": data.get("error"),
    }


def _summary(steps: list[dict[str, Any]], wall: float, reason: str) -> dict:
    def values(key: str) -> list[float]:
        return [float(s[key]) for s in steps if isinstance(s.get(key), (int, float))]

    def total(key: str) -> int | None:
        observed = values(key)
        return int(sum(observed)) if observed else None

    decisions = values("decision_latency_s")
    full_steps = values("step_wall_time_s")
    return {
        "measured_steps": len(steps),
        "episode_wall_time_s": round(wall, 4),
        "termination_reason": reason,
        "average_decision_latency_s": (
            round(sum(decisions) / len(decisions), 4) if decisions else None
        ),
        "average_step_wall_time_s": (
            round(sum(full_steps) / len(full_steps), 4) if full_steps else None
        ),
        "total_prompt_tokens": total("prompt_tokens"),
        "total_completion_tokens": total("completion_tokens"),
        "total_total_tokens": total("total_tokens"),
        "token_usage_complete": bool(steps) and all(
            step.get("total_tokens") is not None for step in steps
        ),
        "steps": steps,
    }


def _run_task(
    args: argparse.Namespace,
    task: TaskRecord,
    task_dir: Path,
    env: PhysicalAdbEnv,
    client: OpenAIClient,
) -> dict[str, Any]:
    agent = QwenAgent(
        env=env,
        llm_client=client,
        name="UI-Voyager",
        model_name="qwen3vl",
        prompt_name="qwen3vl_instruct",
        wait_after_action_seconds=args.wait_after_action,
        history_len=args.history_len,
        n_history_image=0,
        sft_data_dir=None,
    )
    agent.reset(go_home=False)
    episode_start = time.perf_counter()
    deadline = episode_start + args.task_timeout
    termination_reason = "step_limit"
    steps: list[dict[str, Any]] = []
    trajectory_path = task_dir / "trajectory.jsonl"

    for step_id in range(task.max_steps):
        if time.perf_counter() >= deadline:
            termination_reason = "timeout"
            break
        step_start = time.perf_counter()
        result = agent.step(task.instruction, task_name=task.key)
        data = result.data
        if data.get("model_response") == "Error calling LLM":
            raise RuntimeError("Local model endpoint exhausted all request retries.")
        if data.get("error"):
            raise RuntimeError(f"Agent step failed: {data['error']}")
        _save_image(task_dir / f"screenshot_{step_id:03d}_before.png", data.get("before_screenshot"))
        _save_image(task_dir / f"screenshot_{step_id:03d}_after.png", data.get("after_screenshot"))
        step = _serializable_step(step_id, data, time.perf_counter() - step_start)
        steps.append(step)
        _append_jsonl(trajectory_path, step)

        if result.done:
            action = data.get("action")
            if action and action.action_type == json_action.STATUS:
                termination_reason = f"model_terminate:{action.goal_status or 'unknown'}"
            elif action and action.action_type == json_action.ANSWER:
                termination_reason = "model_answer"
            else:
                termination_reason = "model_done"
            break

    wall = time.perf_counter() - episode_start
    summary = _summary(steps, wall, termination_reason)
    _write_json(task_dir / "run_stats.json", summary)
    return summary


def run(args: argparse.Namespace) -> int:
    if args.adb_server_socket:
        os.environ["ADB_SERVER_SOCKET"] = args.adb_server_socket
    device = _resolve_device(args.adb, args.adb_server_port, args.device)
    tasks = load_tasks(args.workbook)
    if args.only_key:
        wanted = set(args.only_key)
        tasks = [task for task in tasks if task.key in wanted]
        missing = wanted - {task.key for task in tasks}
        if missing:
            raise ValueError(f"Unknown task keys: {sorted(missing)}")
    if args.limit is not None:
        tasks = tasks[: args.limit]

    run_dir = args.run_dir.resolve()
    manifest = _manifest(args, tasks, device)
    manifest_path = run_dir / "manifest.json"
    if args.resume:
        if not manifest_path.is_file():
            raise FileNotFoundError(f"Resume manifest not found: {manifest_path}")
        if json.loads(manifest_path.read_text()) != manifest:
            raise ValueError("Resume configuration differs from frozen manifest.")
    else:
        if run_dir.exists():
            raise FileExistsError(f"Refusing to overwrite run directory: {run_dir}")
        (run_dir / "tasks").mkdir(parents=True)
        _write_json(manifest_path, manifest)

    if args.dry_run:
        _write_json(run_dir / "progress.json", _progress(run_dir, tasks))
        print(json.dumps(_progress(run_dir, tasks), ensure_ascii=False, indent=2))
        return 0

    _check_device(
        args.adb,
        device,
        args.adb_server_port,
        {APP_PACKAGES[task.app] for task in tasks},
    )
    _check_api(args.base_url, args.model, args.api_key)
    env = PhysicalAdbEnv(
        args.adb,
        device,
        adb_server_port=args.adb_server_port,
        app_packages=APP_PACKAGES,
    )
    client = OpenAIClient(
        base_url=args.base_url,
        model=args.model,
        api_key=args.api_key,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        top_p=args.top_p,
        max_retry=args.max_retry,
        retry_delay=args.retry_delay,
    )

    try:
        for index, task in enumerate(tasks, start=1):
            task_dir = run_dir / "tasks" / task.key
            result_path = task_dir / "result.json"
            if result_path.is_file():
                previous = json.loads(result_path.read_text())
                if previous.get("status") not in args.retry_status:
                    continue
                _archive_attempt(task_dir, previous.get("status", "unknown"))
            task_dir.mkdir(parents=True, exist_ok=True)
            _write_json(task_dir / "task.json", asdict(task))
            started_at = _utcnow()
            error = None
            infrastructure_error = None
            status = "completed"
            summary: dict[str, Any] = {}
            package = APP_PACKAGES[task.app]
            try:
                _restart_app(args.adb, device, args.adb_server_port, package)
                time.sleep(args.launch_wait)
                summary = _run_task(args, task, task_dir, env, client)
                if summary.get("termination_reason") == "timeout":
                    status = "timeout"
            except Exception as exc:  # Preserve task-level failures and continue.
                status = "exception"
                error = f"{type(exc).__name__}: {exc}"
            finally:
                try:
                    _save_image(
                        task_dir / "final_screenshot.png",
                        env.get_state(wait_to_stabilize=False).pixels,
                    )
                except Exception as exc:
                    error = (error + "; " if error else "") + f"screenshot: {exc}"
                try:
                    _restart_app(args.adb, device, args.adb_server_port, package)
                except Exception as exc:
                    error = (error + "; " if error else "") + f"reset: {exc}"

            if error:
                try:
                    _check_infrastructure(args, device, package)
                except Exception as exc:
                    infrastructure_error = f"{type(exc).__name__}: {exc}"

            _write_json(
                result_path,
                {
                    "status": status,
                    "error": error,
                    "started_at": started_at,
                    "finished_at": _utcnow(),
                    "termination_reason": summary.get("termination_reason"),
                    "model_reported_done": str(
                        summary.get("termination_reason", "")
                    ).startswith("model_"),
                    "manual_success": None,
                    "manual_assessment_note": None,
                },
            )
            progress = _progress(run_dir, tasks)
            _write_json(run_dir / "progress.json", progress)
            print(
                f"[{index}/{len(tasks)}] {task.key} {status}; "
                f"remaining={progress['remaining']}",
                flush=True,
            )
            if infrastructure_error:
                raise RuntimeError(
                    "Benchmark infrastructure became unavailable after "
                    f"{task.key}: {infrastructure_error}"
                )
    finally:
        env.close()
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workbook", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--adb", type=Path, default=Path(shutil.which("adb") or "adb"))
    parser.add_argument("--device")
    parser.add_argument("--adb-server-port", type=int, default=5037)
    parser.add_argument("--adb-server-socket")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--model", default="UI-Voyager")
    parser.add_argument("--api-key", default="EMPTY")
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--top-p", type=float, default=0.8)
    parser.add_argument("--max-tokens", type=int, default=16384)
    parser.add_argument("--max-retry", type=int, default=3)
    parser.add_argument("--retry-delay", type=float, default=1.0)
    parser.add_argument("--history-len", type=int, default=30)
    parser.add_argument("--wait-after-action", type=float, default=1.5)
    parser.add_argument("--launch-wait", type=float, default=2.0)
    parser.add_argument("--task-timeout", type=float, default=1800.0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--retry-status", action="append", default=[])
    parser.add_argument("--only-key", action="append", default=[])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
