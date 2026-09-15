import contextlib
import json
import os
import platform
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from minisweagent.exceptions import Submitted
from minisweagent.utils.serialize import recursive_merge

STRACE_ARGV_FILE = Path("/strace_argv.txt")
SESSION_FILE = Path("/subdir_session_id.txt")

SCHEMA_VERSION = 1
TURNS_DIR_NAME = "turns"
TURN_DIR_FORMAT = "%04d"
TRACE_PREFIX_NAME = "trace"
META_NAME = "meta.json"
META_TMP_NAME = "meta.json.tmp"
MAX_TURNS = 10000

PRODUCER = "mini-swe-agent"

ROOT_PID_TIMEOUT = 1.0
POLL_INTERVAL = 0.005
TERMINATE_GRACE = 2.0


def harness_file(path: Path) -> str:
    if not path.exists():
        raise RuntimeError(
            f"{path} is missing: it is written by trace_generation/docker_strace.py at "
            "environment start. Running this fork outside StraceDockerEnvironment is "
            "not supported."
        )
    return path.read_text()


def strace_argv(command: str, trace_prefix: Path) -> list[str]:
    """Invariant: every strace flag comes from the harness, so none is restated here."""
    return [
        "strace",
        *harness_file(STRACE_ARGV_FILE).splitlines(),
        "-o",
        str(trace_prefix),
        "--",
        "bash",
        "-c",
        command,
    ]


def log_dir() -> Path:
    return Path(harness_file(SESSION_FILE).splitlines()[2])


def allocate_turn_dir(turns_dir: Path, start: int = 0) -> tuple[int, Path]:
    turns_dir.mkdir(parents=True, exist_ok=True)
    for turn in range(start, MAX_TURNS):
        directory = turns_dir / (TURN_DIR_FORMAT % turn)
        try:
            directory.mkdir()
        except FileExistsError:
            continue
        return turn, directory
    raise RuntimeError(f"{turns_dir} already holds {MAX_TURNS} turns")


def write_meta(directory: Path, meta: dict) -> None:
    (directory / META_TMP_NAME).write_text(json.dumps(meta))
    os.replace(directory / META_TMP_NAME, directory / META_NAME)


def traced_pid(tracer: subprocess.Popen) -> int | None:
    """Root of this turn's process tree: strace's only child, since ptrace never reparents."""
    deadline = time.monotonic() + ROOT_PID_TIMEOUT
    children = Path(f"/proc/{tracer.pid}/task/{tracer.pid}/children")
    while time.monotonic() < deadline:
        try:
            pids = children.read_text().split()
        except OSError:
            return None
        if pids:
            return int(pids[0])
        if tracer.poll() is not None:
            return None
        time.sleep(POLL_INTERVAL)
    return None


class LocalEnvironmentConfig(BaseModel):
    cwd: str = ""
    env: dict[str, str] = {}
    timeout: int = 30


class LocalEnvironment:
    def __init__(self, *, config_class: type = LocalEnvironmentConfig, **kwargs):
        """This class executes bash commands directly on the local machine."""
        self.config = config_class(**kwargs)
        self._next_turn = 0

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        """Execute a command in the local environment and return the result as a dict."""
        command = action.get("command", "")
        cwd = cwd or self.config.cwd or _current_dir()
        timeout = timeout or self.config.timeout

        turn, directory = allocate_turn_dir(log_dir() / TURNS_DIR_NAME, self._next_turn)
        self._next_turn = turn + 1
        meta = {
            "schema": SCHEMA_VERSION,
            "turn": turn,
            "producer": PRODUCER,
            "command": command,
            "traced": True,
            "cwd": cwd,
            "timeout_sec": timeout,
            "started_at": time.time(),
            "complete": False,
        }
        write_meta(directory, meta)

        root: list[int | None] = []
        try:
            result = _run(
                strace_argv(command, directory / TRACE_PREFIX_NAME),
                cwd,
                os.environ | self.config.env,
                timeout,
                on_start=lambda tracer: root.append(traced_pid(tracer)),
            )
            meta |= {"returncode": result.returncode, "timed_out": False}
            output = {"output": result.stdout, "returncode": result.returncode, "exception_info": ""}
        except Exception as e:
            meta |= {"returncode": None, "timed_out": isinstance(e, subprocess.TimeoutExpired)}
            raw_output = getattr(e, "output", None)
            raw_output = (
                raw_output.decode("utf-8", errors="replace") if isinstance(raw_output, bytes) else (raw_output or "")
            )
            output = {
                "output": raw_output,
                "returncode": -1,
                "exception_info": f"An error occurred while executing the command: {e}",
                "extra": {"exception_type": type(e).__name__, "exception": str(e)},
            }
        finally:
            meta |= {
                "root_pid": root[0] if root else None,
                "root_pid_source": "proc-children" if root and root[0] else None,
                "ended_at": time.time(),
                "complete": True,
            }
            write_meta(directory, meta)

        self._check_finished(output)
        return output

    def _check_finished(self, output: dict):
        """Raises Submitted if the output indicates task completion."""
        lines = output.get("output", "").lstrip().splitlines(keepends=True)
        if lines and lines[0].strip() == "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" and output["returncode"] == 0:
            submission = "".join(lines[1:])
            raise Submitted(
                {
                    "role": "exit",
                    "content": submission,
                    "extra": {"exit_status": "Submitted", "submission": submission},
                }
            )

    def get_template_vars(self, **kwargs) -> dict[str, Any]:
        return recursive_merge(self.config.model_dump(), platform.uname()._asdict(), os.environ, kwargs)

    def serialize(self) -> dict:
        return {
            "info": {
                "config": {
                    "environment": self.config.model_dump(mode="json"),
                    "environment_type": f"{self.__class__.__module__}.{self.__class__.__name__}",
                }
            }
        }


def _current_dir() -> str:
    # An agent that deletes the directory it was launched in (rm -rf /app) leaves getcwd()
    # resolving a deleted inode, so it keeps failing even once the path is recreated.
    try:
        return os.getcwd()
    except FileNotFoundError:
        return "/"


def _run(
    args: list[str],
    cwd: str,
    env: dict[str, str],
    timeout: int,
    on_start: Callable[[subprocess.Popen], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Like subprocess.run, but kills the whole process group on timeout so no children are orphaned.

    Invariant: the tracer is asked to exit before it is killed, so it flushes its per-pid
    buffers. The direct child is strace, so killing only it would leave bash and its
    descendants alive holding the stdout pipe open.
    """
    process = subprocess.Popen(
        args,
        shell=False,
        text=True,
        cwd=cwd,
        env=env,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=os.name == "posix",
    )
    if on_start is not None:
        on_start(process)
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate(process)
        stdout, _ = process.communicate()
        raise subprocess.TimeoutExpired(args, timeout, output=stdout)
    return subprocess.CompletedProcess(args, process.returncode, stdout=stdout)


def _terminate(process: subprocess.Popen) -> None:
    if os.name != "posix":
        process.kill()
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
        deadline = time.monotonic() + TERMINATE_GRACE
        while time.monotonic() < deadline and process.poll() is None:
            time.sleep(POLL_INTERVAL)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
