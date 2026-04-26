#!/usr/bin/env python3
"""Repository-local watcher that asks Codex to organize changed documents."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


STATE_DIR = ".codex-doc-watch"
STATE_FILE = "state.json"
PID_FILE = "watcher.pid"
LOG_DIR = "logs"

DOCUMENT_EXTENSIONS = {
    ".adoc",
    ".markdown",
    ".md",
    ".mdx",
    ".org",
    ".rst",
    ".txt",
}
IGNORED_PREFIXES = (
    ".git/",
    f"{STATE_DIR}/",
    "scripts/",
    "tests/",
    "docs/superpowers/",
)
IGNORED_SUFFIXES = (
    ".bak",
    ".part",
    ".swp",
    ".tmp",
    "~",
)
DEFAULT_CODEX_ARGS = "exec --full-auto --sandbox danger-full-access"
DEFAULT_COMMIT_MESSAGE = "docs: organize knowledge base updates"


def repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        text=True,
        capture_output=True,
    )
    return Path(result.stdout.strip())


def state_dir(root: Path) -> Path:
    return root / STATE_DIR


def state_path(root: Path) -> Path:
    return state_dir(root) / STATE_FILE


def pid_path(root: Path) -> Path:
    return state_dir(root) / PID_FILE


def watcher_log_path(root: Path) -> Path:
    return state_dir(root) / LOG_DIR / "watcher.log"


def ensure_runtime_dirs(root: Path) -> None:
    (state_dir(root) / LOG_DIR).mkdir(parents=True, exist_ok=True)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_git_path(path: str) -> str:
    return path.strip().strip('"').replace("\\", "/")


def is_document_path(path: str) -> bool:
    normalized = normalize_git_path(path)
    if not normalized:
        return False
    if normalized.startswith(IGNORED_PREFIXES):
        return False
    if normalized.endswith(IGNORED_SUFFIXES):
        return False
    return Path(normalized).suffix.lower() in DOCUMENT_EXTENSIONS


def parse_changed_document_paths(status_output: bytes | str) -> list[str]:
    if isinstance(status_output, bytes):
        text = status_output.decode("utf-8", errors="replace")
    else:
        text = status_output

    tokens = [token for token in text.split("\0") if token]
    changed: list[str] = []
    index = 0
    while index < len(tokens):
        entry = tokens[index]
        if len(entry) < 4:
            index += 1
            continue

        status = entry[:2]
        path = normalize_git_path(entry[3:])
        paths = [path]

        if "R" in status or "C" in status:
            index += 1
            if index < len(tokens):
                paths.append(normalize_git_path(tokens[index]))

        for candidate in paths:
            if is_document_path(candidate) and candidate not in changed:
                changed.append(candidate)

        index += 1

    return changed


def changed_document_paths(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return parse_changed_document_paths(result.stdout)


def seconds_until_allowed(state: dict, now: float, cooldown: int) -> int:
    last_attempt = state.get("last_attempt_epoch")
    if not last_attempt:
        return 0
    remaining = int(cooldown - (now - float(last_attempt)))
    return max(0, remaining)


def load_state(root: Path) -> dict:
    path = state_path(root)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(root: Path, state: dict) -> None:
    ensure_runtime_dirs(root)
    target = state_path(root)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(target)


def current_branch(root: Path) -> str:
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    )
    branch = result.stdout.strip()
    if not branch:
        raise RuntimeError("Cannot determine current Git branch.")
    return branch


def build_codex_prompt(changed_paths: Iterable[str], remote: str, branch: str) -> str:
    paths = "\n".join(f"- `{path}`" for path in changed_paths)
    return f"""你是 `howindocs` 仓库的文档整理代理。监视器检测到这些文档发生变化：

{paths}

请在当前 Git 仓库内完成一次内容整理，严格按下面流程执行：

1. 先运行 `git status --short`、`git diff --stat`、`git diff`，并查看未跟踪文档内容。以 Git 差异为准，不要只依赖文件名判断。
2. 根据内容把文档整理到合适子目录：`inbox/`、`notes/`、`projects/`、`references/`、`maps/`。目录不存在时可以创建。
3. 保留原始信息，不要删减关键内容；可以改进标题、文件名、段落结构、摘要和内部链接。
4. 更新根目录 `index.md`，用分类链接和简短摘要说明当前知识库内容。
5. 不要编辑 `.codex-doc-watch/`、`scripts/`、`tests/`、`docs/superpowers/`，也不要修改 Git 配置。
6. 整理后运行 `git diff --check`。
7. 如果有文档变更需要提交，执行：
   - `git add -A -- README.md index.md`
   - `for dir in inbox notes projects references maps; do [ -e "$dir" ] && git add -A -- "$dir"; done`
   - 对本次整理仍留在其他位置的文档，也要用 `git add -A -- <path>` 纳入提交
   - `git diff --cached --stat`
   - `git commit -m "{DEFAULT_COMMIT_MESSAGE}"`
   - `git push {remote} HEAD:{branch}`
8. 如果没有需要整理或提交的文档变更，请说明原因，不要创建空提交。

目标是让仓库保持可检索、可迭代、可连接的个人知识库结构。
"""


def log(message: str) -> None:
    print(f"[{utc_now_iso()}] {message}", flush=True)


def run_command(
    root: Path,
    command: list[str],
    *,
    check: bool = True,
    text: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=root,
        check=check,
        text=text,
        capture_output=capture_output,
    )


def codex_log_path(root: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return state_dir(root) / LOG_DIR / f"codex-{timestamp}.log"


def run_codex(root: Path, prompt: str) -> int:
    ensure_runtime_dirs(root)
    if os.environ.get("CODEX_DOC_WATCH_DRY_RUN") == "1":
        path = codex_log_path(root)
        path.write_text(prompt, encoding="utf-8")
        log(f"Dry run enabled; wrote Codex prompt to {path}")
        return 0

    executable = os.environ.get("CODEX_DOC_WATCH_CODEX", "codex")
    configured_args = os.environ.get("CODEX_DOC_WATCH_CODEX_ARGS", DEFAULT_CODEX_ARGS)
    command = [executable, *shlex.split(configured_args), "--cd", str(root), "-"]
    path = codex_log_path(root)

    log(f"Starting Codex run; log={path}")
    with path.open("w", encoding="utf-8") as log_file:
        log_file.write("Command: " + " ".join(shlex.quote(part) for part in command) + "\n\n")
        log_file.flush()
        process = subprocess.run(
            command,
            cwd=root,
            input=prompt,
            text=True,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            check=False,
        )
    log(f"Codex run exited with code {process.returncode}")
    return process.returncode


def fallback_commit_and_push(root: Path, remote: str, branch: str) -> bool:
    paths = changed_document_paths(root)
    if not paths:
        return False

    log("Codex left document changes uncommitted; attempting fallback commit.")
    run_command(root, ["git", "add", "-A", "--", *paths], capture_output=True)

    cached = run_command(
        root,
        ["git", "diff", "--cached", "--quiet"],
        check=False,
        capture_output=True,
    )
    if cached.returncode == 0:
        log("No staged document changes after fallback add.")
        return False

    run_command(root, ["git", "diff", "--cached", "--check"], capture_output=True)
    run_command(root, ["git", "commit", "-m", DEFAULT_COMMIT_MESSAGE], capture_output=True)
    run_command(root, ["git", "push", remote, f"HEAD:{branch}"], capture_output=True)
    log("Fallback commit and push completed.")
    return True


def process_once(root: Path, *, force: bool = False) -> int:
    ensure_runtime_dirs(root)
    state = load_state(root)
    cooldown = int(os.environ.get("CODEX_DOC_WATCH_COOLDOWN_SECONDS", "900"))
    remote = os.environ.get("CODEX_DOC_WATCH_REMOTE", "origin")
    branch = os.environ.get("CODEX_DOC_WATCH_BRANCH") or current_branch(root)
    paths = changed_document_paths(root)

    if not paths:
        log("No document changes detected.")
        state.pop("pending_paths", None)
        state.pop("next_run_epoch", None)
        save_state(root, state)
        return 0

    now = time.time()
    wait_seconds = 0 if force else seconds_until_allowed(state, now=now, cooldown=cooldown)
    if wait_seconds > 0:
        state["pending_paths"] = paths
        state["next_run_epoch"] = int(now + wait_seconds)
        state["next_run_at"] = datetime.fromtimestamp(
            now + wait_seconds,
            tz=timezone.utc,
        ).replace(microsecond=0).isoformat()
        save_state(root, state)
        log(f"Document changes pending; cooldown has {wait_seconds}s remaining.")
        return 2

    state["last_attempt_epoch"] = int(now)
    state["last_attempt_at"] = utc_now_iso()
    state["pending_paths"] = paths
    save_state(root, state)

    prompt = build_codex_prompt(paths, remote=remote, branch=branch)
    result = run_codex(root, prompt)
    if result == 0:
        try:
            fallback_commit_and_push(root, remote=remote, branch=branch)
        except subprocess.CalledProcessError as error:
            log(f"Fallback commit/push failed: {error}")
            result = error.returncode or 1

    state = load_state(root)
    state["last_result_code"] = result
    state["last_result_at"] = utc_now_iso()
    if result == 0:
        state.pop("pending_paths", None)
        state.pop("next_run_epoch", None)
        state.pop("next_run_at", None)
    save_state(root, state)
    return result


def is_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pid(root: Path) -> int | None:
    path = pid_path(root)
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def write_pid(root: Path, pid: int) -> None:
    ensure_runtime_dirs(root)
    pid_path(root).write_text(f"{pid}\n", encoding="utf-8")


def remove_pid(root: Path) -> None:
    try:
        pid_path(root).unlink()
    except FileNotFoundError:
        pass


def start_daemon(root: Path) -> int:
    ensure_runtime_dirs(root)
    existing = read_pid(root)
    if existing and is_pid_running(existing):
        print(f"doc watcher already running with pid {existing}")
        return 0

    log_path = watcher_log_path(root)
    with log_path.open("a", encoding="utf-8") as output:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "daemon"],
            cwd=root,
            stdout=output,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    write_pid(root, process.pid)
    print(f"doc watcher started with pid {process.pid}")
    print(f"log: {log_path}")
    return 0


def stop_daemon(root: Path) -> int:
    pid = read_pid(root)
    if not pid:
        print("doc watcher is not running")
        return 0
    if not is_pid_running(pid):
        remove_pid(root)
        print("doc watcher pid file was stale; removed it")
        return 0

    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 10
    while time.time() < deadline:
        if not is_pid_running(pid):
            remove_pid(root)
            print(f"doc watcher stopped pid {pid}")
            return 0
        time.sleep(0.2)

    os.kill(pid, signal.SIGKILL)
    remove_pid(root)
    print(f"doc watcher killed pid {pid}")
    return 0


def status_daemon(root: Path) -> int:
    pid = read_pid(root)
    if pid and is_pid_running(pid):
        print(f"doc watcher running with pid {pid}")
        return 0
    if pid:
        print(f"doc watcher not running; stale pid {pid}")
        return 1
    print("doc watcher not running")
    return 1


def daemon(root: Path) -> int:
    ensure_runtime_dirs(root)
    write_pid(root, os.getpid())
    stop_requested = False

    def request_stop(signum, frame):  # noqa: ARG001
        nonlocal stop_requested
        stop_requested = True
        log(f"Received signal {signum}; stopping after current loop.")

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    interval = int(os.environ.get("CODEX_DOC_WATCH_INTERVAL_SECONDS", "30"))
    log(f"Document watcher daemon started in {root}")
    try:
        while not stop_requested:
            try:
                process_once(root)
            except Exception as error:  # Keep the daemon alive and make failures visible.
                log(f"Watcher loop failed: {error}")
            time.sleep(max(1, interval))
    finally:
        remove_pid(root)
        log("Document watcher daemon stopped.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Watch this docs repository and invoke Codex to organize changes.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("start", help="Start the watcher in the background.")
    subparsers.add_parser("stop", help="Stop the background watcher.")
    subparsers.add_parser("status", help="Show watcher status.")
    subparsers.add_parser("daemon", help="Run the watcher loop in the foreground.")
    run_once = subparsers.add_parser("run-once", help="Process pending changes once.")
    run_once.add_argument(
        "--force",
        action="store_true",
        help="Ignore the cooldown and run immediately if documents changed.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = repo_root()

    if args.command == "start":
        return start_daemon(root)
    if args.command == "stop":
        return stop_daemon(root)
    if args.command == "status":
        return status_daemon(root)
    if args.command == "daemon":
        return daemon(root)
    if args.command == "run-once":
        return process_once(root, force=args.force)

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
