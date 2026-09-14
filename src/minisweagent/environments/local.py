import json
import os
import platform
import signal
import subprocess
import time
from collections.abc import Callable
from typing import Any
#from yaya_companion import *


from pydantic import BaseModel

from minisweagent.exceptions import Submitted
from minisweagent.utils.serialize import recursive_merge

SYSCALLS_FILE = "/strace_syscalls.txt"
SESSION_FILE = "/subdir_session_id.txt"

SCHEMA_VERSION = 1
TURNS_DIR_NAME = "turns"
TURN_DIR_FORMAT = "%04d"
TRACE_PREFIX_NAME = "trace"
META_NAME = "meta.json"
META_TMP_NAME = "meta.json.tmp"
MAX_TURNS = 10000

PRODUCER = "mini-swe-agent"

SUBMIT_SENTINEL = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"

ROOT_PID_POLL_SECONDS = 1.0
ROOT_PID_POLL_INTERVAL = 0.005

TIMEOUT_GRACE_SECONDS = 2.0


def tracked_syscalls() -> str:
    try:
        with open(SYSCALLS_FILE) as f:
            return f.read().strip()
    except FileNotFoundError as e:
        raise RuntimeError(
            f"{SYSCALLS_FILE} is missing: it is written by trace_generation/"
            "docker_strace.py at environment start and holds the syscall set to trace. "
            "Running this fork outside StraceDockerEnvironment is not supported."
        ) from e


def read_session() -> tuple[str, str, str]:
    with open(SESSION_FILE) as f:
        lines = f.read().split("\n")
    return lines[0], lines[1], lines[2]


def yaya_command(command: str, trace_prefix: str) -> list[str]:
    strace_args = [
        "-ff",
        "-ttt",
        "-T",
        "-s", "1024",
        "-e", tracked_syscalls(),
        "-y",
        "-A",
        "-I1",
        "-o", trace_prefix,
    ]
    return ["strace"] + strace_args + ["--", "bash", "-c", command]


def should_trace(command: str) -> tuple[bool, str | None]:
    if SUBMIT_SENTINEL in command:
        return False, "submit-command"
    if "pip" in command or "apt" in command:
        return False, "installer-command"
    return True, None


def allocate_turn_dir(turns_dir: str, start: int = 0) -> tuple[int, str]:
    os.makedirs(turns_dir, exist_ok=True)
    for turn in range(start, MAX_TURNS):
        candidate = os.path.join(turns_dir, TURN_DIR_FORMAT % turn)
        try:
            os.mkdir(candidate)
        except FileExistsError:
            continue
        return turn, candidate
    raise RuntimeError(f"{turns_dir} already holds {MAX_TURNS} turns")


def write_meta(turn_dir: str, meta: dict) -> None:
    tmp = os.path.join(turn_dir, META_TMP_NAME)
    with open(tmp, "w") as f:
        json.dump(meta, f)
    os.replace(tmp, os.path.join(turn_dir, META_NAME))


def first_child_pid(process: subprocess.Popen) -> int | None:
    deadline = time.monotonic() + ROOT_PID_POLL_SECONDS
    path = f"/proc/{process.pid}/task/{process.pid}/children"
    while time.monotonic() < deadline:
        try:
            with open(path) as f:
                pids = f.read().split()
        except OSError:
            return None
        if pids:
            return int(pids[0])
        if process.poll() is not None:
            return None
        time.sleep(ROOT_PID_POLL_INTERVAL)
    return None


class LocalEnvironmentConfig(BaseModel):
    cwd: str = ""
    env: dict[str, str] = {}
    timeout: int = 30
    subdir: str = "unset"
    session_id: str = "unset"



class LocalEnvironment:
    def __init__(self, *, config_class: type = LocalEnvironmentConfig, **kwargs):
        """This class executes bash commands directly on the local machine."""
        self.config = config_class(**kwargs)
        #var to prevent it from trying the end submit too many times
        self.been_here = False
        self._next_turn = 0

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        """Execute a command in the local environment and return the result as a dict."""
        command = action.get("command", "")
        try:
            cwd = cwd or self.config.cwd or os.getcwd()
        except FileNotFoundError:
            # Triggers when the agent deleted the directory the process was launched
            # in (e.g., `rm -rf /app`), and os.getcwd() resolves the deleted inode,
            # so it keeps failing even if the path is recreated.
            cwd = "/"
        try:
            _, _, log_dir = read_session()
            turn, turn_dir = allocate_turn_dir(
                os.path.join(log_dir, TURNS_DIR_NAME), self._next_turn
            )
            self._next_turn = turn + 1

            traced, untraced_reason = should_trace(command)
            if traced:
                use_command = yaya_command(command, os.path.join(turn_dir, TRACE_PREFIX_NAME))
            else:
                use_command = ["bash", "-c", command]

            meta = {
                "schema": SCHEMA_VERSION,
                "turn": turn,
                "producer": PRODUCER,
                "command": command,
                "traced": traced,
                "untraced_reason": untraced_reason,
                "cwd": cwd,
                "timeout_sec": timeout or self.config.timeout,
                "started_at": time.time(),
                "complete": False,
            }
            write_meta(turn_dir, meta)

            root_pid: list[int | None] = [None]

            def capture_root(process: subprocess.Popen) -> None:
                root_pid[0] = first_child_pid(process) if traced else process.pid

            # The complete record is written in a finally so a command killed by the
            # timeout still leaves a readable turn.
            try:
                result = _run(
                    use_command,
                    cwd,
                    os.environ | self.config.env,
                    timeout or self.config.timeout,
                    on_start=capture_root,
                )
                meta["returncode"] = result.returncode
                meta["timed_out"] = False
            except subprocess.TimeoutExpired:
                meta["returncode"] = None
                meta["timed_out"] = True
                raise
            finally:
                meta["root_pid"] = root_pid[0]
                meta["root_pid_source"] = "proc-children" if root_pid[0] else None
                meta["ended_at"] = time.time()
                meta["complete"] = True
                try:
                    write_meta(turn_dir, meta)
                except Exception as e:
                    print(f"Error writing {META_NAME} for turn {turn}: {e}")
            output = {"output": result.stdout, "returncode": result.returncode, "exception_info": ""}

        except Exception as e:
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


def _run(
    args: list[str],
    cwd: str,
    env: dict[str, str],
    timeout: int,
    on_start: Callable[[subprocess.Popen], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Like subprocess.run, but kills the whole process group on timeout.

    Invariant: on timeout the tracer is given SIGTERM and a grace period before
    SIGKILL, so it flushes its per-pid buffers. The direct child is strace, so
    killing only it would leave bash and its descendants alive holding the stdout
    pipe, and communicate() would block until they exited on their own.
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
        try:
            on_start(process)
        except Exception as e:
            print(f"Error capturing the root pid: {e}")
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_group(process)
        stdout, _ = process.communicate()
        raise subprocess.TimeoutExpired(args, timeout, output=stdout)
    return subprocess.CompletedProcess(args, process.returncode, stdout=stdout)


def _terminate_group(process: subprocess.Popen) -> None:
    if os.name != "posix":
        process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + TIMEOUT_GRACE_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return
        time.sleep(ROOT_PID_POLL_INTERVAL)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
