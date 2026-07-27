#!/usr/bin/env python3
"""Terminal dashboard renderer for Chief Wiggum Loop."""

from __future__ import annotations

import atexit
import os
import re
import shutil
import sys
import textwrap
import threading
import time
from typing import Any

try:
    import termios
    import tty
except ImportError:
    termios = None
    tty = None

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
FG_CYAN = "\033[38;5;45m"
FG_BLUE = "\033[38;5;39m"
FG_GREEN = "\033[38;5;78m"
FG_YELLOW = "\033[38;5;221m"
FG_RED = "\033[38;5;203m"
FG_MAGENTA = "\033[38;5;177m"
FG_WHITE = "\033[38;5;255m"
FG_MUTED = "\033[38;5;246m"
BG_HEADER = "\033[48;5;236m"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
WIGGUM_QUOTE = '"Bake em away, toys!"'

CHIEF_WIGGUM_ART = [
    "  ▀▀▀▀▀  ▀▀   ▀▀ ▀▀▀▀▀▀ ▀▀▀▀▀▀▀ ▀▀▀▀▀▀▀      ▀▀     ▀▀ ▀▀▀▀▀▀   ▀▀▀▀▀     ▀▀▀▀▀   ▀▀   ▀▀ ▀▀▀   ▀▀▀",
    " ▀▀   ▀▀ ▀▀   ▀▀   ▀▀   ▀▀      ▀▀           ▀▀  ▀  ▀▀   ▀▀    ▀▀        ▀▀       ▀▀   ▀▀ ▀▀▀▀ ▀▀▀▀",
    "▀▀▀      ▀▀▀▀▀▀▀   ▀▀   ▀▀▀▀▀   ▀▀▀▀▀        ▀▀ ▀▀▀ ▀▀   ▀▀   ▀▀▀  ▀▀▀▀ ▀▀▀  ▀▀▀▀ ▀▀   ▀▀ ▀▀ ▀▀▀ ▀▀",
    " ▀▀   ▀▀ ▀▀   ▀▀   ▀▀   ▀▀      ▀▀           ▀▀▀▀ ▀▀▀▀   ▀▀    ▀▀   ▀▀   ▀▀   ▀▀  ▀▀   ▀▀ ▀▀  ▀  ▀▀",
    "  ▀▀▀▀▀  ▀▀   ▀▀ ▀▀▀▀▀▀ ▀▀▀▀▀▀▀ ▀▀            ▀▀   ▀▀  ▀▀▀▀▀▀   ▀▀▀▀▀     ▀▀▀▀▀    ▀▀▀▀▀  ▀▀     ▀▀",
]
TITLE_ART = []
MENU_DESCRIPTION = (
    "A wrapper around the opencode-ralph-loop plugin, designed to automate large implementations on limited local LLMs. "
    "Break your project down into small scope markdown files and let Police Chief Wiggum handle the rest. "
    "Your project will make sergeant with this if you do it right. If you are lazy it'll get busted back down to sergeant so fast it'll make your head spin."
)
MENU_WARNING = "Use at your own risk. Any complaints will be written up on my invisible typewriter."
MENU_LICENSE = "Copyright (c) 2026 Donald Carnegie. Apache with Commons Clause License. See LICENSE file for details."
WIGGUM_ART = [

    "###############################-----###########################",
    "###########################----------------------##############",
    "################----------------------------------#############",
    "###############-------------------------------------###########",
    "#############-------------------------------------------#######",
    "############-------------------------------#---------------####",
    "##########-----------------------------####--##-----------#####",
    "#######--------------------------------##-##--#---------#######",
    "###---------------------------#########-##---#--------#########",
    "####-------------------#-----------------------#---############",
    "########---------------#----------------------------###########",
    "#############-----######--##--------------------------#########",
    "#############--##########-###---####-------------------########",
    "############--##########--##-##########--#######--#############",
    "#############--########--##--###-#######-#########-############",
    "##############---#--##--###-############--###-####-############",
    "##############-#####-#-####-############--########-############",
    "##############-###-#--######--#######--#######----#############",
    "#############-----############--------##--##---#-##############",
    "#############-#######################-#-##-###------###########",
    "############--######################################--#########",
    "############--###############--#######################-########",
    "############--################-----##################--########",
    "#############-###############--#######---------------##########",
    "##############--##############################--###############",
    "################--############################--###############",
    "##################---#########################-################",
    "######################----###################--################",
    "############################-----------------##################",
]


def strip_ansi(value: str) -> str:
    return ANSI_RE.sub("", value)


def visible_len(value: str) -> int:
    return len(strip_ansi(value))


def pad_visible(value: str, width: int) -> str:
    plain = strip_ansi(value)
    if len(plain) > width:
        plain = plain[: max(0, width - 1)] + ("…" if width else "")
        value = plain
    padding = max(0, width - visible_len(value))
    return value + (" " * padding)


def wrap_block(lines: list[str], width: int) -> list[str]:
    wrapped: list[str] = []
    for line in lines:
        source = strip_ansi(line)
        if not source:
            wrapped.append("")
            continue
        wrapped.extend(
            textwrap.wrap(
                source,
                width=max(1, width),
                replace_whitespace=False,
                drop_whitespace=False,
                break_long_words=True,
                break_on_hyphens=False,
            )
            or [""]
        )
    return wrapped


def box(title: str, lines: list[str], width: int, height: int, color: str, *, tail: bool = False) -> list[str]:
    width = max(12, width)
    height = max(3, height)
    inner_width = width - 2
    inner_height = height - 2
    title_text = f" {title} "
    title_trim = title_text[: max(0, inner_width)]
    top_fill = max(0, inner_width - len(strip_ansi(title_trim)))
    top = f"{color}╭{title_trim}{'─' * top_fill}╮{RESET}"
    wrapped = wrap_block(lines, inner_width)
    content = wrapped[-inner_height:] if tail else wrapped[:inner_height]
    while len(content) < inner_height:
        content.append("")
    body = [f"{color}│{RESET}{pad_visible(line, inner_width)}{color}│{RESET}" for line in content]
    bottom = f"{color}╰{'─' * inner_width}╯{RESET}"
    return [top, *body, bottom]


def join_columns(left: list[str], right: list[str], gap: int = 1) -> list[str]:
    rows = max(len(left), len(right))
    left_width = max((visible_len(line) for line in left), default=0)
    output: list[str] = []
    for index in range(rows):
        left_line = left[index] if index < len(left) else " " * left_width
        right_line = right[index] if index < len(right) else ""
        output.append(left_line + (" " * gap) + right_line)
    return output


def render_progress_bar(completed: int, total: int, width: int) -> str:
    width = max(10, width)
    if total <= 0:
        return "[" + ("·" * width) + "]"
    ratio = min(1.0, max(0.0, completed / total))
    filled = int(ratio * width)
    bar = f"{FG_CYAN}{'█' * filled}{RESET}{FG_MUTED}{'░' * (width - filled)}{RESET}"
    return f"[{bar}]"


def splash_lines() -> list[str]:
    return [f"{FG_YELLOW}{line}{RESET}" for line in WIGGUM_ART] + ["", f"{BOLD}{FG_WHITE}{WIGGUM_QUOTE}{RESET}"]


def _paint_static_lines(lines: list[str]) -> None:
    for index, line in enumerate(lines):
        print(f"\r\033[2K{line}", end="")
        if index < len(lines) - 1:
            print()
    print("", flush=True)


def _clear_static_lines(line_count: int) -> None:
    if line_count <= 0:
        return
    print(f"\033[{line_count}A", end="")
    for index in range(line_count):
        print("\r\033[2K", end="")
        if index < line_count - 1:
            print()
    print("\r", end="", flush=True)


def read_single_key() -> str:
    if os.name == "nt":
        import msvcrt

        while True:
            key = msvcrt.getwch()
            if key:
                if key == "\x03":
                    raise KeyboardInterrupt()
                return key.lower()

    if termios is None or tty is None:
        value = input().strip().lower()
        return value[:1] if value else ""

    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while True:
            key = sys.stdin.read(1)
            if not key:
                continue
            if key == "\x03":
                raise KeyboardInterrupt()
            if key == "\x1b":
                next_one = sys.stdin.read(1)
                if next_one == "[":
                    next_two = sys.stdin.read(1)
                    if next_two == "A":
                        return "up"
                    if next_two == "B":
                        return "down"
                return "escape"
            if key == "\r":
                key = "\n"
            return key.lower()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original)


def show_paged_text(text: str) -> None:
    lines = text.splitlines()
    term = os.environ.get("TERM", "")
    if not (sys.stdin.isatty() and sys.stdout.isatty()) or term.lower() == "dumb":
        print(text)
        return

    _, height = shutil.get_terminal_size((140, 42))
    view_height = max(5, height - 1)
    offset = 0

    def prompt(label: str) -> str:
        return f"{FG_CYAN}:{RESET} {label}"

    while True:
        max_offset = max(0, len(lines) - view_height)
        offset = min(max(offset, 0), max_offset)
        chunk = lines[offset : offset + view_height]
        _paint_static_lines(chunk)
        if len(chunk) < view_height:
            for _ in range(view_height - len(chunk)):
                print("\r\033[2K", end="")
                print()

        if len(lines) <= view_height:
            label = "q=quit, Enter=return"
        elif offset >= max_offset:
            label = "end | Enter/j/down=stay, k/up=back, q=quit"
        else:
            label = "Enter/j/down=next line, k/up=back, q=quit"

        print(prompt(label), end="", flush=True)
        key = read_single_key()
        print("", flush=True)
        _clear_static_lines(view_height + 1)

        if key == "q":
            return
        if len(lines) <= view_height and key in {"\n", "j", "down"}:
            return
        if key in {"\n", "j", "down"}:
            if offset < max_offset:
                offset += 1
            continue
        if key in {"k", "up"}:
            if offset > 0:
                offset -= 1
            continue


def _format_config_value(entry: dict[str, Any], value: Any) -> str:
    kind = entry.get("kind")
    if kind == "bool":
        return "on" if value else "off"
    if kind == "list":
        return ", ".join(value) if value else "-"
    if value in {None, ""}:
        return "-"
    return str(value)


def show_config_editor(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    term = os.environ.get("TERM", "")
    if not (sys.stdin.isatty() and sys.stdout.isatty()) or term.lower() == "dumb":
        return None

    values = {entry["key"]: entry.get("value") for entry in entries}
    message = "Toggle booleans with the item number. Save with s."

    while True:
        width, height = shutil.get_terminal_size((140, 42))
        width = max(100, width)
        height = max(24, height)
        lines = []
        for index, entry in enumerate(entries, start=1):
            value = _format_config_value(entry, values.get(entry["key"]))
            lines.append(f"{index:>2}. {entry['label']}: {value}")
        lines.extend(
            [
                "",
                message,
                "",
                "Commands: number=edit or toggle, s=save, q=cancel",
                "For list values, enter a comma-separated list.",
                "Use '-' to clear optional string or list values.",
            ]
        )
        box_lines = box("Chief Config", lines, width, height - 1, FG_CYAN, tail=False)
        _paint_static_lines(box_lines)
        try:
            command = input(": ").strip()
        finally:
            _clear_static_lines(len(box_lines) + 1)

        if command.lower() in {"q", "quit"}:
            return None
        if command.lower() in {"s", "save"}:
            return values
        if not command.isdigit():
            message = "Enter an item number, s, or q."
            continue

        index = int(command) - 1
        if index < 0 or index >= len(entries):
            message = "Item number out of range."
            continue

        entry = entries[index]
        key = entry["key"]
        kind = entry.get("kind")
        current = values.get(key)
        if kind == "bool":
            values[key] = not bool(current)
            message = f"{entry['label']} set to {_format_config_value(entry, values[key])}."
            continue

        prompt = f": {entry['label']} [{_format_config_value(entry, current)}] "
        raw = input(prompt).strip()
        if raw == "":
            message = f"{entry['label']} unchanged."
            continue

        try:
            if raw == "-":
                if kind == "list":
                    values[key] = []
                elif entry.get("allow_empty"):
                    values[key] = None
                else:
                    raise ValueError("value cannot be cleared")
            elif kind == "int":
                values[key] = int(raw)
            elif kind == "list":
                values[key] = [part.strip() for part in raw.split(",") if part.strip()]
            else:
                values[key] = raw
            message = f"{entry['label']} updated."
        except ValueError as exc:
            message = f"Invalid value: {exc}"


def show_startup_menu(root: str, config_lines: list[str] | None = None) -> str | None:
    term = os.environ.get("TERM", "")
    if not (sys.stdin.isatty() and sys.stdout.isatty()) or term.lower() == "dumb":
        return None

    width, height = shutil.get_terminal_size((140, 42))
    width = max(100, width)
    height = max(28, height)
    inner_width = width - 2
    left_width = min(max(44, inner_width // 2), 66)
    right_width = max(24, inner_width - left_width - 2)

    banner_lines = [""] + [f"{FG_YELLOW}{line}{RESET}" for line in CHIEF_WIGGUM_ART]
    left_lines = [f"{FG_YELLOW}{line}{RESET}" for line in WIGGUM_ART]
    right_lines: list[str] = [f"{FG_CYAN}{line}{RESET}" for line in TITLE_ART]
    right_lines.extend(wrap_block([MENU_DESCRIPTION], right_width))
    right_lines.extend(["", MENU_WARNING, MENU_LICENSE, "", f"Target: {root}", ""])
    right_lines.extend(
        [
            f"{BOLD}{FG_WHITE}1...{RESET} Bake em away, toys! - Run until complete",
            f"{BOLD}{FG_WHITE}2...{RESET} Turn on the ol' Wiggum Charm - Validate instructions",
            f"{BOLD}{FG_WHITE}3...{RESET} Its a Ghost car! - Dry run",
            f"{BOLD}{FG_WHITE}4...{RESET} The harder you push, the faster we will all get out of here - Sync status",
            f"{BOLD}{FG_WHITE}c...{RESET} Config book 'em - Edit target path and switches",
            f"{BOLD}{FG_WHITE}h...{RESET} Suspect is hatless, repeat hatless! - Help",
            f"{BOLD}{FG_WHITE}q...{RESET} Holy moses this does taste like Grandma - Quit",
        ]
    )
    if config_lines:
        right_lines.extend(["", "Current config:", *config_lines])
    right_lines.extend(["", f"{FG_CYAN}Press 1, 2, 3, 4, c, h, or q.{RESET}"])

    combined_lines: list[str] = []
    combined_lines.extend(banner_lines)
    combined_lines.append("")
    shared_rows = min(len(left_lines), len(right_lines))
    for index in range(shared_rows):
        combined_lines.append(f"{pad_visible(left_lines[index], left_width)}  {right_lines[index]}")
    for index in range(shared_rows, len(left_lines)):
        combined_lines.append(left_lines[index])
    if shared_rows < len(right_lines):
        combined_lines.append("")
        combined_lines.extend(wrap_block(right_lines[shared_rows:], inner_width))

    panel_height = max(10, height - 1)
    inner_width = width - 2
    inner_height = max(1, panel_height - 2)
    title_text = " Chief Wiggum Loop "
    title_trim = title_text[:inner_width]
    top_fill = max(0, inner_width - len(strip_ansi(title_trim)))
    top = f"{FG_CYAN}╭{title_trim}{'─' * top_fill}╮{RESET}"
    body = []
    for line in combined_lines[:inner_height]:
        body.append(f"{FG_CYAN}│{RESET}{pad_visible(line, inner_width)}{FG_CYAN}│{RESET}")
    while len(body) < inner_height:
        body.append(f"{FG_CYAN}│{RESET}{' ' * inner_width}{FG_CYAN}│{RESET}")
    bottom = f"{FG_CYAN}╰{'─' * inner_width}╯{RESET}"
    lines = [top, *body, bottom]
    _paint_static_lines(lines)

    try:
        while True:
            key = read_single_key()
            if key in {"1", "2", "3", "4", "c", "h", "q"}:
                return key
    finally:
        _clear_static_lines(len(lines))


class Dashboard:
    def __init__(self, state: Any) -> None:
        self.state = state
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.enabled = True
        self.last_frame: str = ""
        self.last_size: tuple[int, int] = (0, 0)
        self.printed_line_count: int = 0
        self.stopped = False
        self.paused = False
        self.lock = threading.Lock()
        self.started_at = time.monotonic()

    def start(self) -> None:
        term = os.environ.get("TERM", "")
        self.enabled = (
            hasattr(self.state, "snapshot")
            and sys.stdout.isatty()
            and term.lower() != "dumb"
            and shutil.get_terminal_size((120, 40)).columns > 0
        )
        if not self.enabled:
            return
        atexit.register(self.stop)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self, *, clear: bool = True) -> None:
        with self.lock:
            if self.stopped:
                return
            self.stopped = True
        self.stop_event.set()
        self.wake_event.set()
        if self.thread and self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(timeout=1)
        if self.enabled and clear:
            self._clear_lines()

    def notify(self) -> None:
        self.wake_event.set()

    def pause(self) -> None:
        with self.lock:
            self.paused = True
        if self.enabled:
            self._clear_lines()

    def resume(self) -> None:
        with self.lock:
            self.paused = False
        self.notify()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            with self.lock:
                paused = self.paused
            if paused:
                self.wake_event.wait(timeout=0.2)
                self.wake_event.clear()
                continue
            self._render()
            self.wake_event.wait(timeout=getattr(self.state, "refresh_seconds", 10))
            self.wake_event.clear()

    def _render(self) -> None:
        if not self.enabled:
            return
        snap = self.state.snapshot()
        width, height = shutil.get_terminal_size((140, 42))
        width = max(100, width)
        height = max(28, height)

        left_width = (width - 1) // 2
        right_width = width - left_width - 1

        header_height = 5
        top_row_height = 6
        mid_row_height = 8
        lower_row_height = 8
        output_height = max(8, height - header_height - top_row_height - mid_row_height - lower_row_height)

        completed = snap.get("completed", 0)
        total = snap.get("total", 0)
        progress_bar = render_progress_bar(completed, total, max(18, left_width - 20))

        phase = snap.get("phase", "idle")
        phase_color = FG_GREEN if phase == "idle" else FG_YELLOW
        session = snap.get("session_id") or "--"
        header_lines = [
            f"{BG_HEADER}{BOLD}{FG_WHITE} Chief Wiggum Loop {RESET}  {FG_MUTED}Root:{RESET} {snap.get('root', '--')}",
            f"{FG_CYAN}Mode:{RESET} {snap.get('mode', 'run')}    {FG_BLUE}Phase:{RESET} {phase_color}{phase}{RESET}",
            f"{FG_MAGENTA}Working:{RESET} {snap.get('breadcrumb', 'idle')}",
            f"{FG_MUTED}Session:{RESET} {session}    {FG_MUTED}q:{RESET} intervene",
        ]
        header_box = box("Overview", header_lines, width, header_height, FG_BLUE)

        progress_lines = [
            f"{progress_bar} {completed}/{total}",
            f"{FG_MUTED}Done:{RESET} {completed}",
            f"{FG_MUTED}Remaining:{RESET} {max(total - completed, 0)}",
            f"{FG_MUTED}Current state:{RESET} {snap.get('current_state', 'n/a')}",
        ]
        metrics_lines = [
            f"{FG_MUTED}Elapsed:{RESET} {snap.get('elapsed_display', '--')}",
            f"{FG_MUTED}Avg/task:{RESET} {snap.get('avg_task_display', '--')}",
            f"{FG_MUTED}ETA:{RESET} {snap.get('eta_display', '--')}",
            f"{FG_MUTED}Last sync:{RESET} {snap.get('last_sync_display', '--')}",
        ]
        progress_box = box("Progress", progress_lines, left_width, top_row_height, FG_CYAN)
        metrics_box = box("Timing", metrics_lines, right_width, top_row_height, FG_MAGENTA)
        top_row = join_columns(progress_box, metrics_box)

        current_command = snap.get("current_command") or "No active tool command"
        command_lines = [f"{FG_YELLOW}{current_command}{RESET}", ""]
        for line in snap.get("recent_commands", [])[:5]:
            command_lines.append(f"{FG_MUTED}{line}{RESET}")
        commands_box = box("Current + Recent Commands", command_lines, left_width, mid_row_height, FG_YELLOW)

        commit_lines = snap.get("recent_commits", [])[:5] or ["No commits yet."]
        commits_box = box("Recent Commits", commit_lines, right_width, mid_row_height, FG_GREEN)
        mid_row = join_columns(commands_box, commits_box)

        directory_lines = snap.get("directory_rows", [])[: lower_row_height - 2] or ["No directories scanned yet."]
        directories_box = box("Directory Status", directory_lines, left_width, lower_row_height, FG_BLUE)

        event_lines = snap.get("recent_events", [])[: lower_row_height - 2] or ["No events yet."]
        events_box = box("Recent Events", event_lines, right_width, lower_row_height, FG_RED)
        lower_row = join_columns(directories_box, events_box)

        output_lines = snap.get("opencode_output", []) or ["No opencode output yet."]
        output_box = box("Opencode Output", output_lines, width, output_height, FG_WHITE, tail=True)

        screen_lines = [*header_box, *top_row, *mid_row, *lower_row, *output_box]
        screen_lines = screen_lines[:height]
        while len(screen_lines) < height:
            screen_lines.append(" " * width)
        frame = "\n".join(screen_lines)
        size = (width, height)
        if frame == self.last_frame and size == self.last_size:
            return
        self.last_frame = frame
        self.last_size = size
        self._paint_lines(screen_lines)

    def _paint_lines(self, lines: list[str]) -> None:
        if self.printed_line_count:
            print(f"\033[{self.printed_line_count}A", end="")
        max_lines = max(self.printed_line_count, len(lines))
        for index in range(max_lines):
            line = lines[index] if index < len(lines) else ""
            print(f"\r\033[2K{line}", end="")
            if index < max_lines - 1:
                print()
        print("", flush=True)
        self.printed_line_count = len(lines)

    def _clear_lines(self) -> None:
        if not self.printed_line_count:
            return
        print(f"\033[{self.printed_line_count}A", end="")
        for index in range(self.printed_line_count):
            print("\r\033[2K", end="")
            if index < self.printed_line_count - 1:
                print()
        print("\r", end="", flush=True)
        self.printed_line_count = 0
