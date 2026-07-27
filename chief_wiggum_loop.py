#!/usr/bin/env python3
"""Chief Wiggum Loop.

Recursively traverses a directory of task files and, for each task file, uses
`opencode run` in non-interactive JSON mode to:

1. Invoke `/ralph-loop` against the task file.
2. Open fresh `/review` sessions until the git working tree is clean.
3. Create a git commit after each review pass that leaves changes to commit.

The script persists per-directory state in `.chief-wiggum/state.json` so it can
resume after interruption and bubble child summaries up to parent directories.
It also renders a lightweight ANSI dashboard that refreshes every 10 seconds.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import select
import signal
import subprocess
import sys
import textwrap
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable

from chief_wiggum_dashboard import Dashboard, WIGGUM_ART, WIGGUM_QUOTE, read_single_key, show_config_editor, show_paged_text, show_startup_menu

USAGE_INTENT = (
    "Chief Wiggum is a 'kick-it-off-and-walk-away' task runner for local LLMs, not a zero-shot code generator like opencode. "
    "It relies on a strict hybrid pipeline: rely on a heavy cloud LLM for the upfront spec work, then let your local LLM grind through the actual coding for free. "
    "If you don't aggressively break your project down into small, isolated chunks, your local model will hit context limits, hallucinate, or infinitely loop. "
    "Keep your context windows minimal, spec heavily upfront, and Chief Wiggum will handle the rest. Feed it crap and you'll end up with Rex Banner."
)

DEFAULT_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".chief-wiggum",
    ".opencode",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "coverage",
    ".pytest_cache",
    "__pycache__",
}

DEFAULT_EXCLUDE_PATTERNS = [
    "*/.chief-wiggum/*",
    "*/.opencode/ralph-loop.local.md",
]

COMMIT_PREFIX = "COMMIT:"
SYNC_STATE_TAG = "implementation_state"
SYNC_STATUS_VALUES = {"pending", "in_progress", "done", "blocked"}


class ShutdownRequested(Exception):
    """Raised when the user requests graceful shutdown."""


class RetryCurrentRunRequested(Exception):
    """Raised when the user requests that the active opencode run be killed and restarted."""


@dataclass
class RuntimeControl:
    shutdown_requested: bool = False
    retry_requested: bool = False
    active_process: subprocess.Popen[str] | None = None
    active_process_label: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)

    def request_shutdown(self) -> None:
        with self.lock:
            self.shutdown_requested = True
            proc = self.active_process
        if proc is not None:
            terminate_process_tree(proc)

    def request_retry(self) -> None:
        with self.lock:
            self.retry_requested = True
            proc = self.active_process
        if proc is not None:
            terminate_process_tree(proc)

    def set_active_process(self, proc: subprocess.Popen[str] | None, label: str = "") -> None:
        with self.lock:
            self.active_process = proc
            self.active_process_label = label

    def clear_active_process(self, proc: subprocess.Popen[str] | None) -> None:
        with self.lock:
            if self.active_process is proc:
                self.active_process = None
                self.active_process_label = ""

    def active_process_snapshot(self) -> tuple[subprocess.Popen[str] | None, str]:
        with self.lock:
            return self.active_process, self.active_process_label

    def check(self) -> None:
        if self.shutdown_requested:
            raise ShutdownRequested()
        if self.retry_requested:
            with self.lock:
                self.retry_requested = False
            raise RetryCurrentRunRequested()


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def slugify_path(path: Path) -> str:
    return str(path).replace(os.sep, " /")


def compact_text(value: str | None, limit: int = 220) -> str:
    if not value:
        return ""
    collapsed = re.sub(r"\s+", " ", value).strip()
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."


def safe_relpath(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def is_text_file(path: Path, max_bytes: int) -> bool:
    try:
        if path.stat().st_size > max_bytes:
            return False
        with path.open("rb") as handle:
            chunk = handle.read(4096)
    except OSError:
        return False
    if b"\x00" in chunk:
        return False
    return True


def help_description() -> str:
    return "\n".join(
        [
            *WIGGUM_ART,
            "",
            WIGGUM_QUOTE,
            "",
            "Chief Wiggum Loop: run nested Ralph loops over a task tree.",
            "",
            USAGE_INTENT
        ]
        
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=help_description(),
        epilog="Chief says: pick your mode, keep the tree moving, and let the loop finish the job.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    parser.add_argument("target_dir", nargs="?", help="Directory containing task files to process recursively.")
    parser.add_argument("-h", "--help", action="store_true", dest="help_requested", help="Show this help message and exit.")
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        help="Glob pattern to include, relative to target_dir. Repeatable. Defaults to all text files not excluded.",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Glob pattern to exclude, relative to target_dir. Repeatable.",
    )
    parser.add_argument(
        "--ignore-dir",
        action="append",
        default=[],
        help="Directory name to skip during traversal. Repeatable.",
    )
    parser.add_argument(
        "--max-file-size-kb",
        type=int,
        default=512,
        help="Maximum task file size in KB when auto-detecting text tasks.",
    )
    parser.add_argument(
        "--refresh-seconds",
        type=int,
        default=10,
        help="Dashboard refresh interval in seconds.",
    )
    parser.add_argument(
        "--max-review-passes",
        type=int,
        default=5,
        help="Maximum review and commit passes per task file.",
    )
    parser.add_argument(
        "--opencode-bin",
        default="opencode",
        help="Path to the opencode executable.",
    )
    parser.add_argument("--model", help="Optional opencode model override.")
    parser.add_argument("--agent", help="Optional opencode agent override.")
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Pass --auto to opencode. Use only if your opencode policy allows it.",
    )
    parser.add_argument(
        "--allow-dirty-start",
        action="store_true",
        help="Allow starting when the git working tree already has unrelated changes.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan tasks and write initial state, but do not invoke opencode or git commits.",
    )
    parser.add_argument(
        "--sync-mode",
        action="store_true",
        help="Traverse tasks and refresh implementation state via a Ralph run without review or commits.",
    )
    return parser


def parse_args() -> argparse.Namespace:
    return build_parser().parse_args()


def show_help() -> None:
    show_paged_text(build_parser().format_help().rstrip())


def startup_config_entries(args: argparse.Namespace) -> list[dict[str, Any]]:
    return [
        {"key": "target_dir", "label": "Target directory", "kind": "str", "value": args.target_dir or os.getcwd()},
        {"key": "include", "label": "Include globs", "kind": "list", "value": list(args.include)},
        {"key": "exclude", "label": "Exclude globs", "kind": "list", "value": list(args.exclude)},
        {"key": "ignore_dir", "label": "Ignored directories", "kind": "list", "value": list(args.ignore_dir)},
        {"key": "max_file_size_kb", "label": "Max file size KB", "kind": "int", "value": args.max_file_size_kb},
        {"key": "refresh_seconds", "label": "Refresh seconds", "kind": "int", "value": args.refresh_seconds},
        {"key": "max_review_passes", "label": "Max review passes", "kind": "int", "value": args.max_review_passes},
        {"key": "opencode_bin", "label": "Opencode binary", "kind": "str", "value": args.opencode_bin},
        {"key": "model", "label": "Model override", "kind": "str", "value": args.model, "allow_empty": True},
        {"key": "agent", "label": "Agent override", "kind": "str", "value": args.agent, "allow_empty": True},
        {"key": "auto", "label": "Auto mode", "kind": "bool", "value": args.auto},
        {"key": "allow_dirty_start", "label": "Allow dirty start", "kind": "bool", "value": args.allow_dirty_start},
        {"key": "dry_run", "label": "Dry run", "kind": "bool", "value": args.dry_run},
        {"key": "sync_mode", "label": "Sync mode", "kind": "bool", "value": args.sync_mode},
    ]


def apply_startup_config(args: argparse.Namespace, values: dict[str, Any]) -> None:
    for key, value in values.items():
        setattr(args, key, value)


def startup_config_summary(args: argparse.Namespace) -> list[str]:
    mode = "sync" if args.sync_mode else ("dry-run" if args.dry_run else "run")
    include = ", ".join(args.include) if args.include else "all text files"
    exclude = ", ".join(args.exclude) if args.exclude else "default excludes"
    model = args.model or "default"
    agent = args.agent or "default"
    return [
        f"mode={mode} auto={'on' if args.auto else 'off'} dirty-start={'on' if args.allow_dirty_start else 'off'}",
        f"include={include}",
        f"exclude={exclude}",
        f"opencode={args.opencode_bin} model={model} agent={agent}",
    ]


def run_command(
    args: list[str],
    cwd: Path,
    *,
    check: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        args,
        cwd=str(cwd),
        text=True,
        capture_output=capture_output,
    )
    if check and proc.returncode != 0:
        stderr = proc.stderr.strip() if proc.stderr else ""
        stdout = proc.stdout.strip() if proc.stdout else ""
        details = stderr or stdout or f"exit code {proc.returncode}"
        raise RuntimeError(f"Command failed: {' '.join(args)}\n{details}")
    return proc


def terminate_process_tree(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name != "nt":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except ProcessLookupError:
        return
    except OSError:
        proc.terminate()


def start_intervention_listener(runtime: RuntimeControl, dashboard: DashboardState, dashboard_ui: Dashboard) -> threading.Thread | None:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None

    def key_available(timeout: float) -> bool:
        if os.name == "nt":
            import msvcrt

            end_time = time.monotonic() + timeout
            while time.monotonic() < end_time:
                if msvcrt.kbhit():
                    return True
                time.sleep(0.05)
            return False
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        return bool(ready)

    def prompt_user(prompt: str) -> str:
        dashboard_ui.pause()
        print(prompt, end="", flush=True)
        try:
            choice = read_single_key()
        finally:
            print("", flush=True)
            print("\r\033[2K", end="", flush=True)
            if not runtime.shutdown_requested:
                dashboard_ui.resume()
        return choice

    def listen() -> None:
        while not runtime.shutdown_requested:
            proc, label = runtime.active_process_snapshot()
            if proc is None:
                time.sleep(0.1)
                continue
            if not key_available(0.2):
                continue
            try:
                key = read_single_key()
            except KeyboardInterrupt:
                runtime.request_shutdown()
                return
            if key != "q":
                continue

            proc, label = runtime.active_process_snapshot()
            if proc is None:
                continue

            choice = prompt_user(f": Intervene in {label}. [r]etry current run, [q]uit, [c]ancel ")
            if choice == "r":
                dashboard.add_event(f"manual retry requested for {label}")
                dashboard.add_output(f"=== manually stopped {label}; retrying current run ===")
                runtime.request_retry()
                continue
            if choice == "q":
                dashboard.add_event("manual quit requested")
                dashboard.add_output(f"=== manually stopped {label}; quitting ===")
                runtime.request_shutdown()
                return
            dashboard.add_event("manual intervention cancelled")

    thread = threading.Thread(target=listen, daemon=True)
    thread.start()
    return thread


def git_root(target_dir: Path) -> Path:
    proc = run_command(["git", "rev-parse", "--show-toplevel"], target_dir)
    return Path(proc.stdout.strip())


def parse_status_paths(line: str) -> list[str]:
    payload = line[3:].strip() if len(line) >= 4 else line.strip()
    if " -> " in payload:
        old_path, new_path = payload.split(" -> ", 1)
        return [old_path.strip(), new_path.strip()]
    return [payload]


def is_ignored_git_path(path: str, ignored_paths: set[str]) -> bool:
    normalized = path.rstrip("/")
    for ignored in ignored_paths:
        ignored_normalized = ignored.rstrip("/")
        if normalized == ignored_normalized:
            return True
        if normalized.startswith(ignored_normalized + "/"):
            return True
        if ignored_normalized.startswith(normalized + "/"):
            return True
    return False


def git_status_lines(repo_root: Path, ignored_paths: set[str] | None = None) -> list[str]:
    proc = run_command(["git", "status", "--porcelain"], repo_root)
    lines = []
    ignored = ignored_paths or set()
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        paths = parse_status_paths(line.rstrip())
        if paths and all(is_ignored_git_path(path, ignored) for path in paths):
            continue
        lines.append(line.rstrip())
    return lines


def git_recent_commits(repo_root: Path, limit: int = 5) -> list[str]:
    proc = run_command(["git", "log", f"--pretty=format:%h %s", f"-n{limit}"], repo_root)
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def stageable_paths(repo_root: Path, ignored_paths: set[str]) -> list[str]:
    candidates: list[str] = []
    for line in git_status_lines(repo_root, ignored_paths):
        for path in parse_status_paths(line):
            if is_ignored_git_path(path, ignored_paths):
                continue
            if path not in candidates:
                candidates.append(path)
    return candidates


def ensure_git_excludes(repo_root: Path, task_root: Path) -> None:
    info_exclude = repo_root / ".git" / "info" / "exclude"
    info_exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = info_exclude.read_text(encoding="utf-8") if info_exclude.exists() else ""
    required = [
        ".chief-wiggum/",
        "**/.chief-wiggum/",
        ".opencode/ralph-loop.local.md",
        "**/.opencode/ralph-loop.local.md",
    ]
    if task_root != repo_root:
        task_rel = safe_relpath(task_root, repo_root).rstrip("/") + "/"
        required.append(task_rel)
    missing = [entry for entry in required if entry not in existing]
    if missing:
        prefix = "\n" if existing and not existing.endswith("\n") else ""
        with info_exclude.open("a", encoding="utf-8") as handle:
            handle.write(prefix)
            for entry in missing:
                handle.write(entry + "\n")


@dataclass
class TaskFile:
    path: Path
    relative_path: str
    directory: Path


@dataclass
class DirectoryPlan:
    path: Path
    relative_path: str
    files: list[TaskFile] = field(default_factory=list)
    context_files: list[TaskFile] = field(default_factory=list)
    children: list[Path] = field(default_factory=list)


def should_include(path: Path, root: Path, include: list[str], exclude: list[str]) -> bool:
    relative = safe_relpath(path, root)
    patterns = DEFAULT_EXCLUDE_PATTERNS + exclude
    if any(fnmatch.fnmatch(relative, pattern) for pattern in patterns):
        return False
    if include:
        return any(fnmatch.fnmatch(relative, pattern) for pattern in include)
    return True


def is_context_index_file(path: Path) -> bool:
    return path.name.startswith("000")


def build_plan(root: Path, args: argparse.Namespace) -> dict[Path, DirectoryPlan]:
    ignored_dirs = DEFAULT_IGNORED_DIRS | set(args.ignore_dir)
    max_bytes = args.max_file_size_kb * 1024
    plan: dict[Path, DirectoryPlan] = {}

    for dirpath, dirnames, filenames in os.walk(root):
        dir_path = Path(dirpath)
        dirnames[:] = sorted(name for name in dirnames if name not in ignored_dirs)
        relative_dir = "." if dir_path == root else safe_relpath(dir_path, root)
        entry = plan.setdefault(dir_path, DirectoryPlan(path=dir_path, relative_path=relative_dir))
        entry.children = [dir_path / name for name in dirnames]
        for filename in sorted(filenames):
            file_path = dir_path / filename
            if not should_include(file_path, root, args.include, args.exclude):
                continue
            if not is_text_file(file_path, max_bytes):
                continue
            task_file = TaskFile(
                path=file_path,
                relative_path=safe_relpath(file_path, root),
                directory=dir_path,
            )
            if is_context_index_file(file_path):
                entry.context_files.append(task_file)
            else:
                entry.files.append(task_file)
    return plan


def context_files_for_task(
    task: TaskFile,
    root: Path,
    plan_map: dict[Path, DirectoryPlan],
) -> list[TaskFile]:
    del root
    plan = plan_map.get(task.directory)
    if not plan:
        return []
    return list(plan.context_files)


def render_context_block(context_files: list[TaskFile]) -> str:
    if not context_files:
        return ""
    sections: list[str] = ["Directory context:"]
    for context_file in context_files:
        sections.append(f"--- BEGIN CONTEXT {context_file.relative_path} ---")
        sections.append(context_file.path.read_text(encoding="utf-8").rstrip())
        sections.append(f"--- END CONTEXT {context_file.relative_path} ---")
    return "\n".join(sections).strip()


def state_dir(directory: Path) -> Path:
    return directory / ".chief-wiggum"


def state_path(directory: Path) -> Path:
    return state_dir(directory) / "state.json"


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temp_name = handle.name
    Path(temp_name).replace(path)


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def initial_file_entry(task: TaskFile) -> dict[str, Any]:
    return {
        "path": str(task.path),
        "relative_path": task.relative_path,
        "name": task.path.name,
        "status": "pending",
        "implementation_state": "pending",
        "attempts": 0,
        "started_at": None,
        "updated_at": None,
        "last_synced_at": None,
        "sync_session_id": None,
        "elapsed_seconds": 0.0,
        "implementation_session_id": None,
        "review_session_ids": [],
        "review_passes": 0,
        "last_commit": None,
        "summary": "",
        "final_text": "",
        "blockers": "",
        "next_step": "",
    }


def initial_child_entry(child_path: Path, root: Path) -> dict[str, Any]:
    relative = "." if child_path == root else safe_relpath(child_path, root)
    return {
        "path": str(child_path),
        "relative_path": relative,
        "status": "pending",
        "tldr": "pending",
        "summary": "",
        "updated_at": None,
    }


def ensure_directory_state(directory: Path, root: Path, plan: DirectoryPlan) -> dict[str, Any]:
    current = read_json(state_path(directory)) or {}
    current.setdefault("version", 1)
    current.setdefault("directory", str(directory))
    current.setdefault("relative_directory", plan.relative_path)
    current.setdefault("started_at", now_utc())
    current.setdefault("updated_at", now_utc())
    current.setdefault("status", "pending")
    current.setdefault("tldr", "pending")
    current.setdefault("summary", "")
    current.setdefault("current", {"phase": None, "file": None, "session_id": None})
    current.setdefault("files", [])
    current.setdefault("children", [])
    current.setdefault("latest_commits", [])

    existing_files = {entry["relative_path"]: entry for entry in current["files"]}
    merged_files = []
    for task in plan.files:
        merged_files.append(existing_files.get(task.relative_path, initial_file_entry(task)))
    current["files"] = merged_files

    existing_children = {entry["relative_path"]: entry for entry in current["children"]}
    merged_children = []
    for child in plan.children:
        rel_child = "." if child == root else safe_relpath(child, root)
        merged_children.append(existing_children.get(rel_child, initial_child_entry(child, root)))
    current["children"] = merged_children

    summarize_directory_state(current)
    atomic_write_json(state_path(directory), current)
    return current


def summarize_directory_state(state: dict[str, Any]) -> None:
    files = state.get("files", [])
    children = state.get("children", [])
    file_done = sum(1 for item in files if item.get("status") == "done")
    file_failed = sum(1 for item in files if item.get("status") == "failed")
    file_active = sum(1 for item in files if item.get("status") == "in_progress")
    child_done = sum(1 for item in children if item.get("status") == "done")
    child_failed = sum(1 for item in children if item.get("status") == "failed")
    child_active = sum(1 for item in children if item.get("status") == "in_progress")

    if file_failed or child_failed:
        status = "failed"
    elif file_active or child_active:
        status = "in_progress"
    elif files or children:
        if file_done == len(files) and child_done == len(children):
            status = "done"
        else:
            status = "pending"
    else:
        status = "done"

    state["status"] = status
    state["updated_at"] = now_utc()
    parts = [
        f"files {file_done}/{len(files)} done",
        f"subdirs {child_done}/{len(children)} done",
    ]
    if file_active or child_active:
        parts.append(f"active {file_active + child_active}")
    if file_failed or child_failed:
        parts.append(f"failed {file_failed + child_failed}")
    state["tldr"] = ", ".join(parts)

    lines = [state["tldr"]]
    if files:
        latest_files = ", ".join(
            f"{item['name']}={item.get('implementation_state', item['status'])}" for item in files[:6]
        )
        lines.append(f"file status: {latest_files}")
    if children:
        latest_children = ", ".join(
            f"{Path(item['relative_path']).name or '.'}={item['status']}" for item in children[:6]
        )
        lines.append(f"child status: {latest_children}")
    state["summary"] = "\n".join(lines)


def write_directory_state(directory: Path, state: dict[str, Any]) -> None:
    summarize_directory_state(state)
    atomic_write_json(state_path(directory), state)


def sync_child_into_parent(parent: Path, child: Path, root: Path) -> None:
    parent_state = read_json(state_path(parent))
    child_state = read_json(state_path(child))
    if not parent_state or not child_state:
        return
    relative = "." if child == root else safe_relpath(child, root)
    for entry in parent_state.get("children", []):
        if entry.get("relative_path") == relative:
            entry["status"] = child_state.get("status", "pending")
            entry["tldr"] = child_state.get("tldr", "")
            entry["summary"] = child_state.get("summary", "")
            entry["updated_at"] = child_state.get("updated_at")
            break
    write_directory_state(parent, parent_state)


@dataclass
class DashboardState:
    root: Path
    total_tasks: int
    refresh_seconds: int
    mode: str = "run"
    start_time: float = field(default_factory=time.time)
    completed_tasks: int = 0
    current_breadcrumb: str = "idle"
    current_phase: str = "scanning"
    current_session_id: str = ""
    current_command: str = ""
    last_sync_at: str = ""
    recent_commits: list[str] = field(default_factory=list)
    recent_events: deque[str] = field(default_factory=lambda: deque(maxlen=8))
    recent_commands: deque[str] = field(default_factory=lambda: deque(maxlen=8))
    opencode_output: deque[str] = field(default_factory=lambda: deque(maxlen=240))
    directory_rows: list[str] = field(default_factory=list)
    task_durations: list[float] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add_event(self, message: str) -> None:
        with self.lock:
            self.recent_events.appendleft(f"[{timestamp_local()}] {message}")

    def add_output(self, text: str) -> None:
        lines = text.splitlines() or [text]
        with self.lock:
            for line in lines:
                self.opencode_output.append(line)

    def add_command(self, command: str) -> None:
        stamp = f"[{timestamp_local()}] {command}"
        with self.lock:
            self.current_command = command
            self.recent_commands.appendleft(stamp)

    def mark_sync(self) -> None:
        with self.lock:
            self.last_sync_at = now_utc()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            elapsed = time.time() - self.start_time
            avg = sum(self.task_durations) / len(self.task_durations) if self.task_durations else 0.0
            remaining = max(self.total_tasks - self.completed_tasks, 0)
            eta = avg * remaining if avg else 0.0
            return {
                "root": str(self.root),
                "elapsed": elapsed,
                "elapsed_display": format_duration(elapsed),
                "avg_task": avg,
                "avg_task_display": format_duration(avg),
                "eta": eta,
                "eta_display": format_duration(eta),
                "completed": self.completed_tasks,
                "total": self.total_tasks,
                "breadcrumb": self.current_breadcrumb,
                "phase": self.current_phase,
                "session_id": self.current_session_id,
                "mode": self.mode,
                "current_command": self.current_command,
                "current_state": self.current_phase,
                "last_sync_display": self.last_sync_at or "--",
                "recent_commits": list(self.recent_commits),
                "recent_events": list(self.recent_events),
                "recent_commands": list(self.recent_commands),
                "opencode_output": list(self.opencode_output),
                "directory_rows": list(self.directory_rows),
            }


def timestamp_local() -> str:
    return datetime.now().strftime("%H:%M:%S")


def format_duration(seconds: float) -> str:
    if seconds <= 0:
        return "--"
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {sec}s"
    if minutes:
        return f"{minutes}m {sec}s"
    return f"{sec}s"


def render_bar(completed: int, total: int, width: int = 32) -> str:
    if total <= 0:
        return "[" + ("-" * width) + "]"
    ratio = completed / total
    filled = min(width, max(0, int(ratio * width)))
    return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"


def update_dashboard_directories(root: Path, state: DashboardState, plan: dict[Path, DirectoryPlan]) -> None:
    rows: list[str] = []
    for directory in sorted(plan.keys(), key=lambda value: safe_relpath(value, root)):
        current = read_json(state_path(directory))
        if not current:
            continue
        label = current.get("relative_directory", ".")
        rows.append(f"{label:<36} {current.get('status', 'pending'):<12} {current.get('tldr', '')}")
    with state.lock:
        state.directory_rows = rows


def recent_output_text(state: DashboardState, limit: int = 40) -> str:
    with state.lock:
        lines = list(state.opencode_output)[-limit:]
        command = state.current_command
    sections: list[str] = []
    if command:
        sections.append(f"Last command: {command}")
    if lines:
        sections.append("Recent opencode output:")
        sections.extend(lines)
    return "\n".join(sections).strip()


@dataclass
class OpencodeResult:
    exit_code: int
    session_id: str | None
    text_output: str
    raw_lines: list[str]


def run_opencode_json(
    args: argparse.Namespace,
    run_dir: Path,
    message: str,
    attached_files: list[Path],
    dashboard: DashboardState,
    phase: str,
    runtime: RuntimeControl,
) -> OpencodeResult:
    runtime.check()
    cmd = [args.opencode_bin, "run", "--format", "json", "--dir", str(run_dir)]
    if args.model:
        cmd.extend(["--model", args.model])
    if args.agent:
        cmd.extend(["--agent", args.agent])
    if args.auto:
        cmd.append("--auto")
    if attached_files:
        for file_path in attached_files:
            cmd.extend(["-f", str(file_path)])
    cmd.append(message)

    proc = subprocess.Popen(
        cmd,
        cwd=str(run_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=(os.name != "nt"),
    )
    runtime.set_active_process(proc, f"{phase} {run_dir}")

    session_id: str | None = None
    text_chunks: list[str] = []
    raw_lines: list[str] = []

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            runtime.check()
            raw_lines.append(line.rstrip("\n"))
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                dashboard.add_event(f"{phase}: {compact_text(stripped, 120)}")
                continue

            if not session_id:
                session_id = event.get("sessionID")
            event_type = event.get("type")
            if event_type == "text":
                part = event.get("part") or {}
                chunk = part.get("text", "")
                if chunk:
                    text_chunks.append(chunk)
                    dashboard.add_output(chunk)
                    dashboard.add_event(f"{phase}: {compact_text(chunk, 120)}")
            elif event_type == "step_start":
                dashboard.add_event(f"{phase}: session step started")
            elif event_type == "tool_use":
                part = event.get("part") or {}
                tool_name = part.get("tool")
                tool_state = part.get("state") or {}
                if tool_name:
                    dashboard.add_event(f"{phase}: tool {tool_name}")
                    input_state = tool_state.get("input") or {}
                    command_text = ""
                    if isinstance(input_state, dict):
                        command_text = input_state.get("command") or input_state.get("filePath") or ""
                    if not command_text:
                        command_text = tool_state.get("title") or tool_name
                    dashboard.add_command(f"{tool_name}: {command_text}")
                    tool_output = tool_state.get("output")
                    if isinstance(tool_output, str) and tool_output:
                        dashboard.add_output(f"[{tool_name}] {tool_output}")

            with dashboard.lock:
                dashboard.current_session_id = session_id or dashboard.current_session_id

        proc.wait()
        runtime.check()
        return OpencodeResult(
            exit_code=proc.returncode,
            session_id=session_id,
            text_output="\n".join(text_chunks).strip(),
            raw_lines=raw_lines,
        )
    except RetryCurrentRunRequested:
        terminate_process_tree(proc)
        raise
    except ShutdownRequested:
        terminate_process_tree(proc)
        raise
    finally:
        runtime.clear_active_process(proc)


def retry_message(base_message: str, error_text: str) -> str:
    return (
        f"{base_message}\n\n"
        "The previous attempt failed.\n"
        f"Error: {compact_text(error_text, 800)}\n"
        "Do it again and address that failure. This is the only retry."
    )


def run_opencode_with_single_retry(
    args: argparse.Namespace,
    run_dir: Path,
    message: str,
    dashboard: DashboardState,
    phase: str,
    runtime: RuntimeControl,
    *,
    validate: Callable[[OpencodeResult], None] | None = None,
    allow_retry: bool = True,
) -> OpencodeResult:
    attempts = [message]
    last_error = ""
    for attempt_index, attempt_message in enumerate(attempts, start=1):
        try:
            result = run_opencode_json(
                args,
                run_dir,
                attempt_message,
                [],
                dashboard,
                phase=phase,
                runtime=runtime,
            )
        except RetryCurrentRunRequested:
            dashboard.add_event(f"{phase}: current run manually restarted")
            dashboard.add_output(f"=== {phase} manually restarted ===")
            continue
        try:
            if result.exit_code != 0:
                raise RuntimeError(f"opencode exited with code {result.exit_code}")
            if validate is not None:
                validate(result)
            return result
        except RuntimeError as exc:
            last_error = f"{exc}; output={compact_text(' '.join(result.raw_lines) or result.text_output, 400)}"
            dashboard.add_event(f"{phase}: attempt {attempt_index} failed")
            dashboard.add_output(f"=== {phase} attempt {attempt_index} failed: {compact_text(last_error, 240)} ===")
            if attempt_index == 1 and allow_retry:
                attempts.append(retry_message(message, last_error))
                continue
            raise RuntimeError(last_error) from exc
    raise RuntimeError(last_error or f"{phase} failed")


def extract_commit_message(review_text: str, fallback: str) -> str:
    match = re.search(rf"(?im)^\s*{re.escape(COMMIT_PREFIX)}\s*(.+?)\s*$", review_text)
    if match:
        message = match.group(1).strip()
    else:
        message = fallback
    message = re.sub(r"\s+", " ", message).strip()
    if not message:
        message = fallback
    if len(message) > 72:
        message = message[:69].rstrip() + "..."
    return message


def commit_all(repo_root: Path, message: str, excluded_paths: set[str]) -> None:
    paths = stageable_paths(repo_root, excluded_paths)
    if not paths:
        return
    run_command(["git", "add", "-A", "--", *paths], repo_root)
    run_command(["git", "commit", "-m", message], repo_root)


def update_recent_commits(repo_root: Path, dashboard: DashboardState, directory_state: dict[str, Any]) -> None:
    commits = git_recent_commits(repo_root, limit=5)
    with dashboard.lock:
        dashboard.recent_commits = commits
    directory_state["latest_commits"] = commits


def task_entry_for(state: dict[str, Any], relative_path: str) -> dict[str, Any]:
    for entry in state.get("files", []):
        if entry.get("relative_path") == relative_path:
            return entry
    raise KeyError(f"Task entry missing for {relative_path}")


def implementation_message(relative_path: str, task_text: str, context_text: str) -> str:
    return textwrap.dedent(
        f"""
        /ralph-loop Implement the feature described below.
        Source task file: {relative_path}
        Treat the task content as the source of truth.
        Use any provided directory context as supporting guidance only.
        Work in the current repository as needed.
        Run relevant verification before declaring completion.
        When and only when the task is fully complete, output <promise>DONE</promise>.

        {context_text}

        Task content:
        --- BEGIN TASK ---
        {task_text.rstrip()}
        --- END TASK ---
        """
    ).strip()


def review_prompt(relative_path: str) -> str:
    return textwrap.dedent(
        f"""
        /review Review the current working tree for the task from `{relative_path}`.
        Apply any remediation needed directly in the repository.
        If the tree is ready to commit, finish with exactly one line in this form:
        {COMMIT_PREFIX} <concise commit message>
        """
    ).strip()


def sync_state_prompt(relative_path: str, task_text: str, context_text: str) -> str:
    return textwrap.dedent(
        f"""
        /ralph-loop Inspect the current repository state for the task below.
        Source task file: {relative_path}
        Do not modify any files, do not run review, and do not commit anything.
        Only assess implementation status based on the current repository state.
        Use any provided directory context as supporting guidance only.
        Return exactly one XML block named <{SYNC_STATE_TAG}> containing JSON with these keys:
        status: one of pending, in_progress, done, blocked
        summary: short implementation summary
        blockers: short blocker summary or empty string
        next_step: short next action or empty string
        Then output <promise>DONE</promise>.

        {context_text}

        Task content:
        --- BEGIN TASK ---
        {task_text.rstrip()}
        --- END TASK ---
        """
    ).strip()


def extract_sync_state(text: str) -> dict[str, str]:
    match = re.search(
        rf"<{SYNC_STATE_TAG}>\s*(\{{.*?\}})\s*</{SYNC_STATE_TAG}>",
        text,
        flags=re.DOTALL,
    )
    if not match:
        raise RuntimeError(f"missing <{SYNC_STATE_TAG}> block in sync output")
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise RuntimeError("invalid JSON in implementation state block") from exc

    status = str(payload.get("status", "pending")).strip()
    if status not in SYNC_STATUS_VALUES:
        raise RuntimeError(f"invalid sync status '{status}'")
    return {
        "status": status,
        "summary": compact_text(str(payload.get("summary", "")), 320),
        "blockers": compact_text(str(payload.get("blockers", "")), 320),
        "next_step": compact_text(str(payload.get("next_step", "")), 320),
    }


def apply_sync_state(entry: dict[str, Any], sync_state: dict[str, str]) -> None:
    entry["implementation_state"] = sync_state["status"]
    entry["summary"] = sync_state["summary"]
    entry["blockers"] = sync_state["blockers"]
    entry["next_step"] = sync_state["next_step"]
    entry["status"] = {
        "pending": "pending",
        "in_progress": "in_progress",
        "done": "done",
        "blocked": "failed",
    }[sync_state["status"]]


def sync_task_state(
    task: TaskFile,
    repo_root: Path,
    plan_map: dict[Path, DirectoryPlan],
    root: Path,
    directory_state: dict[str, Any],
    args: argparse.Namespace,
    dashboard: DashboardState,
    runtime: RuntimeControl,
) -> None:
    runtime.check()
    started = time.time()
    entry = task_entry_for(directory_state, task.relative_path)
    entry["attempts"] = int(entry.get("attempts") or 0) + 1
    entry["started_at"] = entry.get("started_at") or now_utc()
    entry["updated_at"] = now_utc()
    directory_state["current"] = {
        "phase": "syncing",
        "file": task.relative_path,
        "session_id": entry.get("sync_session_id"),
    }
    entry["status"] = "in_progress"
    write_directory_state(task.directory, directory_state)

    with dashboard.lock:
        dashboard.current_breadcrumb = slugify_path(Path(task.relative_path))
        dashboard.current_phase = "syncing"
        dashboard.current_session_id = entry.get("sync_session_id") or ""
    dashboard.add_event(f"syncing {task.relative_path}")
    dashboard.add_output(f"=== Sync start: {task.relative_path} ===")
    context_text = render_context_block(context_files_for_task(task, root, plan_map))

    if not args.dry_run:
        def validate_sync(result: OpencodeResult) -> None:
            extract_sync_state(result.text_output)

        try:
            result = run_opencode_with_single_retry(
                args,
                task.directory,
                sync_state_prompt(task.relative_path, task.path.read_text(encoding="utf-8"), context_text),
                dashboard,
                phase="sync",
                runtime=runtime,
                validate=validate_sync,
            )
        except RuntimeError as exc:
            entry["status"] = "failed"
            entry["implementation_state"] = "blocked"
            entry["updated_at"] = now_utc()
            entry["final_text"] = compact_text(str(exc), 320)
            write_directory_state(task.directory, directory_state)
            raise RuntimeError(f"opencode sync failed for {task.relative_path}: {exc}") from exc
        entry["sync_session_id"] = result.session_id
        entry["last_synced_at"] = now_utc()
        entry["final_text"] = compact_text(result.text_output, 320)
        sync_state = extract_sync_state(result.text_output)
        apply_sync_state(entry, sync_state)
    else:
        entry["last_synced_at"] = now_utc()
        apply_sync_state(
            entry,
            {
                "status": entry.get("implementation_state", "pending"),
                "summary": entry.get("summary", "dry run only"),
                "blockers": entry.get("blockers", ""),
                "next_step": entry.get("next_step", ""),
            },
        )

    elapsed = time.time() - started
    entry["elapsed_seconds"] = float(entry.get("elapsed_seconds") or 0.0) + elapsed
    entry["updated_at"] = now_utc()
    update_recent_commits(repo_root, dashboard, directory_state)
    directory_state["current"] = {"phase": None, "file": None, "session_id": None}
    write_directory_state(task.directory, directory_state)
    with dashboard.lock:
        dashboard.completed_tasks += 1
        dashboard.task_durations.append(elapsed)
        dashboard.current_phase = "idle"
        dashboard.current_session_id = ""
        dashboard.current_command = ""
    dashboard.add_event(f"synced {task.relative_path}: {entry.get('implementation_state', 'pending')}")
    dashboard.add_output(f"=== Sync result: {task.relative_path} -> {entry.get('implementation_state', 'pending')} ===")
    dashboard.mark_sync()


def export_session_summary(opencode_bin: str, run_dir: Path, session_id: str) -> str:
    proc = run_command([opencode_bin, "export", session_id], run_dir)
    payload = proc.stdout
    brace_index = payload.find("{")
    if brace_index == -1:
        return ""
    try:
        data = json.loads(payload[brace_index:])
    except json.JSONDecodeError:
        return ""

    messages = data.get("messages", [])
    assistant_texts: list[str] = []
    for message in reversed(messages):
        info = message.get("info", {})
        if info.get("role") != "assistant":
            continue
        for part in message.get("parts", []):
            if part.get("type") == "text" and part.get("text"):
                assistant_texts.append(part["text"])
        if assistant_texts:
            break
    return compact_text("\n".join(reversed(assistant_texts)), 320)


def process_task(
    task: TaskFile,
    root: Path,
    repo_root: Path,
    ignored_repo_paths: set[str],
    plan_map: dict[Path, DirectoryPlan],
    directory_state: dict[str, Any],
    args: argparse.Namespace,
    dashboard: DashboardState,
    runtime: RuntimeControl,
) -> None:
    runtime.check()
    entry = task_entry_for(directory_state, task.relative_path)
    if args.sync_mode:
        sync_task_state(task, repo_root, plan_map, root, directory_state, args, dashboard, runtime)
        return
    if entry.get("status") == "done":
        return

    started = time.time()
    entry["status"] = "in_progress"
    entry["attempts"] = int(entry.get("attempts") or 0) + 1
    entry["started_at"] = entry.get("started_at") or now_utc()
    entry["updated_at"] = now_utc()
    directory_state["current"] = {
        "phase": "implementing",
        "file": task.relative_path,
        "session_id": entry.get("implementation_session_id"),
    }
    write_directory_state(task.directory, directory_state)

    with dashboard.lock:
        dashboard.current_breadcrumb = slugify_path(Path(task.relative_path))
        dashboard.current_phase = "implementing"
        dashboard.current_session_id = entry.get("implementation_session_id") or ""
    dashboard.add_event(f"starting {task.relative_path}")
    dashboard.add_output(f"=== Task start: {task.relative_path} ===")
    context_text = render_context_block(context_files_for_task(task, root, plan_map))

    if not args.dry_run:
        try:
            result = run_opencode_with_single_retry(
                args,
                task.directory,
                implementation_message(
                    task.relative_path,
                    task.path.read_text(encoding="utf-8"),
                    context_text,
                ),
                dashboard,
                phase="implement",
                runtime=runtime,
            )
        except RuntimeError as exc:
            entry["status"] = "failed"
            entry["updated_at"] = now_utc()
            entry["final_text"] = compact_text(str(exc), 320)
            write_directory_state(task.directory, directory_state)
            raise RuntimeError(f"opencode implementation failed for {task.relative_path}: {exc}") from exc
        entry["implementation_session_id"] = result.session_id
        entry["final_text"] = compact_text(result.text_output, 320)
        if result.session_id:
            entry["summary"] = export_session_summary(args.opencode_bin, task.directory, result.session_id)
    else:
        entry["summary"] = "dry run only"

    review_and_commit(task, repo_root, ignored_repo_paths, directory_state, args, dashboard, runtime)

    elapsed = time.time() - started
    entry["elapsed_seconds"] = float(entry.get("elapsed_seconds") or 0.0) + elapsed
    entry["status"] = "done"
    entry["updated_at"] = now_utc()
    directory_state["current"] = {"phase": None, "file": None, "session_id": None}
    write_directory_state(task.directory, directory_state)
    with dashboard.lock:
        dashboard.completed_tasks += 1
        dashboard.task_durations.append(elapsed)
        dashboard.current_phase = "idle"
        dashboard.current_session_id = ""
        dashboard.current_command = ""
    dashboard.add_event(f"completed {task.relative_path}")
    dashboard.add_output(f"=== Task complete: {task.relative_path} ===")


def review_and_commit(
    task: TaskFile,
    repo_root: Path,
    ignored_repo_paths: set[str],
    directory_state: dict[str, Any],
    args: argparse.Namespace,
    dashboard: DashboardState,
    runtime: RuntimeControl,
) -> None:
    entry = task_entry_for(directory_state, task.relative_path)
    review_ids: list[str] = entry.setdefault("review_session_ids", [])
    base_review_message = review_prompt(task.relative_path)

    for review_pass in range(1, args.max_review_passes + 1):
        runtime.check()
        dirty_before = git_status_lines(repo_root, ignored_repo_paths)
        if not dirty_before:
            update_recent_commits(repo_root, dashboard, directory_state)
            write_directory_state(task.directory, directory_state)
            return

        entry["review_passes"] = review_pass
        entry["updated_at"] = now_utc()
        directory_state["current"] = {
            "phase": "reviewing",
            "file": task.relative_path,
            "session_id": None,
        }
        write_directory_state(task.directory, directory_state)

        with dashboard.lock:
            dashboard.current_phase = f"review pass {review_pass}"
            dashboard.current_breadcrumb = slugify_path(Path(task.relative_path))
        dashboard.add_event(f"review pass {review_pass} for {task.relative_path}")

        review_text = ""
        if not args.dry_run:
            try:
                result = run_opencode_with_single_retry(
                    args,
                    task.directory,
                    base_review_message,
                    dashboard,
                    phase="review",
                    runtime=runtime,
                )
            except RuntimeError as exc:
                entry["status"] = "failed"
                entry["updated_at"] = now_utc()
                write_directory_state(task.directory, directory_state)
                raise RuntimeError(f"opencode review failed for {task.relative_path}: {exc}") from exc
            if result.session_id:
                review_ids.append(result.session_id)
            review_text = result.text_output
        else:
            review_text = f"{COMMIT_PREFIX} Dry run commit for {task.relative_path}"

        dirty_after_review = git_status_lines(repo_root, ignored_repo_paths)
        if not dirty_after_review:
            update_recent_commits(repo_root, dashboard, directory_state)
            write_directory_state(task.directory, directory_state)
            return

        commit_message = extract_commit_message(
            review_text,
            fallback=f"Chief Wiggum Loop: {task.relative_path}",
        )
        entry["last_commit"] = commit_message
        directory_state["current"] = {
            "phase": "committing",
            "file": task.relative_path,
            "session_id": review_ids[-1] if review_ids else None,
        }
        write_directory_state(task.directory, directory_state)
        with dashboard.lock:
            dashboard.current_phase = f"committing pass {review_pass}"
        dashboard.add_event(f"commit: {commit_message}")

        try:
            if not args.dry_run:
                commit_all(repo_root, commit_message, ignored_repo_paths)
            update_recent_commits(repo_root, dashboard, directory_state)
        except RuntimeError as exc:
            if args.dry_run:
                raise
            dashboard.add_event(f"commit failed for {task.relative_path}")
            dashboard.add_output(f"=== commit failed: {compact_text(str(exc), 240)} ===")
            retry_result = run_opencode_with_single_retry(
                args,
                task.directory,
                retry_message(base_review_message, str(exc)),
                dashboard,
                phase="review-retry",
                runtime=runtime,
                allow_retry=False,
            )
            if retry_result.session_id:
                review_ids.append(retry_result.session_id)
            review_text = retry_result.text_output
            commit_message = extract_commit_message(
                review_text,
                fallback=f"Chief Wiggum Loop: {task.relative_path}",
            )
            entry["last_commit"] = commit_message
            commit_all(repo_root, commit_message, ignored_repo_paths)
            update_recent_commits(repo_root, dashboard, directory_state)
        write_directory_state(task.directory, directory_state)

        if not git_status_lines(repo_root, ignored_repo_paths):
            return

    raise RuntimeError(
        f"working tree still dirty after {args.max_review_passes} review passes for {task.relative_path}"
    )


def count_completed_tasks(plan: dict[Path, DirectoryPlan]) -> int:
    completed = 0
    for directory in plan.keys():
        state = read_json(state_path(directory))
        if not state:
            continue
        completed += sum(1 for item in state.get("files", []) if item.get("status") == "done")
    return completed


def process_directory(
    directory: Path,
    root: Path,
    repo_root: Path,
    ignored_repo_paths: set[str],
    plan_map: dict[Path, DirectoryPlan],
    args: argparse.Namespace,
    dashboard_state: DashboardState,
    dashboard_ui: Dashboard,
    runtime: RuntimeControl,
) -> None:
    runtime.check()
    plan = plan_map[directory]
    current_state = ensure_directory_state(directory, root, plan)
    update_dashboard_directories(root, dashboard_state, plan_map)
    dashboard_ui.notify()

    for task in plan.files:
        runtime.check()
        current_state = read_json(state_path(directory)) or current_state
        process_task(
            task,
            root,
            repo_root,
            ignored_repo_paths,
            plan_map,
            current_state,
            args,
            dashboard_state,
            runtime,
        )
        current_state = read_json(state_path(directory)) or current_state
        update_dashboard_directories(root, dashboard_state, plan_map)
        dashboard_ui.notify()

    for child in plan.children:
        process_directory(child, root, repo_root, ignored_repo_paths, plan_map, args, dashboard_state, dashboard_ui, runtime)
        sync_child_into_parent(directory, child, root)
        update_dashboard_directories(root, dashboard_state, plan_map)
        dashboard_ui.notify()

    current_state = read_json(state_path(directory)) or current_state
    current_state["current"] = {"phase": None, "file": None, "session_id": None}
    write_directory_state(directory, current_state)


def print_dry_run_summary(root: Path, plan_map: dict[Path, DirectoryPlan]) -> None:
    print("Chief Wiggum Loop dry run")
    print(f"Root: {root}")
    total = 0
    for directory in sorted(plan_map.keys(), key=lambda value: safe_relpath(value, root)):
        plan = plan_map[directory]
        print(f"\n[{plan.relative_path}]")
        if plan.files:
            for task in plan.files:
                total += 1
                print(f"  - {task.relative_path}")
        else:
            print("  - no task files")
    print(f"\nTotal task files: {total}")


def validate_instruction_set(root: Path, plan_map: dict[Path, DirectoryPlan]) -> None:
    runnable_tasks = 0
    context_files = 0
    unreadable: list[str] = []

    for plan in plan_map.values():
        runnable_tasks += len(plan.files)
        context_files += len(plan.context_files)
        for task in [*plan.files, *plan.context_files]:
            try:
                task.path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                unreadable.append(f"{task.relative_path}: {exc}")

    if runnable_tasks == 0:
        raise RuntimeError(f"no runnable instruction files found under {root}")
    if unreadable:
        raise RuntimeError("instruction validation failed:\n" + "\n".join(unreadable))

    print("Chief Wiggum instruction validation passed")
    print(f"Root: {root}")
    print(f"Runnable task files: {runnable_tasks}")
    print(f"Context index files: {context_files}")
    print(f"Directories scanned: {len(plan_map)}")


def apply_startup_selection(args: argparse.Namespace, selection: str | None) -> str:
    if selection == "q":
        return "quit"
    if selection == "h":
        return "help"
    if selection == "2":
        return "validate"
    if selection == "3":
        args.dry_run = True
        args.sync_mode = False
        return "dry-run"
    if selection == "4":
        args.sync_mode = True
        args.dry_run = False
        return "sync"
    if selection == "1":
        args.dry_run = False
        args.sync_mode = False
        return "run"
    return "sync" if args.sync_mode else ("dry-run" if args.dry_run else "run")


def validate_environment(
    args: argparse.Namespace,
    root: Path,
    repo_root: Path,
    ignored_repo_paths: set[str],
) -> None:
    try:
        run_command([args.opencode_bin, "--version"], root)
    except Exception as exc:
        raise RuntimeError(f"opencode not available: {exc}") from exc
    if args.sync_mode:
        return
    if not args.allow_dirty_start and git_status_lines(repo_root, ignored_repo_paths):
        raise RuntimeError(
            "git working tree is already dirty. Commit or stash unrelated changes, or rerun with --allow-dirty-start."
        )


def main() -> int:
    args = parse_args()
    if args.help_requested:
        show_help()
        return 0

    while True:
        current_root = str(Path(args.target_dir or os.getcwd()).expanduser())
        selection = show_startup_menu(current_root, startup_config_summary(args))
        if selection == "c":
            updated = show_config_editor(startup_config_entries(args))
            if updated is not None:
                apply_startup_config(args, updated)
            continue
        startup_mode = apply_startup_selection(args, selection)
        if startup_mode == "quit":
            return 0
        if startup_mode == "help":
            show_help()
            return 0
        break

    if not args.target_dir:
        raise RuntimeError("target_dir is required. Use --help for usage.")
    runtime = RuntimeControl()
    root = Path(args.target_dir).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise RuntimeError(f"target directory does not exist: {root}")

    plan_map = build_plan(root, args)
    if root not in plan_map:
        plan_map[root] = DirectoryPlan(path=root, relative_path=".")
    if startup_mode == "validate":
        validate_instruction_set(root, plan_map)
        return 0

    repo_root = git_root(root)

    ignored_repo_paths = {
        safe_relpath(task.path, repo_root)
        for plan in plan_map.values()
        for task in plan.files
    }

    ensure_git_excludes(repo_root, root)
    validate_environment(args, root, repo_root, ignored_repo_paths)

    for directory, plan in plan_map.items():
        ensure_directory_state(directory, root, plan)

    total_tasks = sum(len(plan.files) for plan in plan_map.values())
    dashboard_state = DashboardState(
        root=root,
        total_tasks=total_tasks,
        refresh_seconds=max(1, args.refresh_seconds),
        mode="sync" if args.sync_mode else ("dry-run" if args.dry_run else "run"),
        completed_tasks=count_completed_tasks(plan_map),
    )
    update_dashboard_directories(root, dashboard_state, plan_map)
    dashboard_state.recent_commits = git_recent_commits(repo_root, limit=5)
    dashboard = Dashboard(dashboard_state)

    previous_sigint = signal.getsignal(signal.SIGINT)

    def handle_sigint(signum: int, frame: Any) -> None:
        del signum, frame
        runtime.request_shutdown()
        dashboard_state.add_event("shutdown requested")
        dashboard_state.add_output("=== Shutdown requested ===")
        dashboard.notify()

    signal.signal(signal.SIGINT, handle_sigint)

    if args.dry_run:
        signal.signal(signal.SIGINT, previous_sigint)
        print_dry_run_summary(root, plan_map)
        return 0

    dashboard.start()
    start_intervention_listener(runtime, dashboard_state, dashboard)
    try:
        process_directory(root, root, repo_root, ignored_repo_paths, plan_map, args, dashboard_state, dashboard, runtime)
        update_dashboard_directories(root, dashboard_state, plan_map)
        dashboard_state.add_event("run completed")
        dashboard.notify()
        return 0
    except ShutdownRequested:
        return 130
    except Exception as exc:
        dashboard.stop(clear=False)
        signal.signal(signal.SIGINT, previous_sigint)
        print(f"Chief Wiggum Loop failed: {exc}", file=sys.stderr)
        recent_output = recent_output_text(dashboard_state)
        if recent_output:
            print("", file=sys.stderr)
            print(recent_output, file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        dashboard.stop()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"Chief Wiggum Loop failed: {exc}", file=sys.stderr)
        raise SystemExit(1)