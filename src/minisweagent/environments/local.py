import os
import platform
import subprocess
from typing import Any
#from yaya_companion import *


from pydantic import BaseModel

from minisweagent.exceptions import Submitted
from minisweagent.utils.serialize import recursive_merge

#this function takes a command as a string, as well as a path to pipe the strace output to, and outputs 
#["strace", strace_args, "--", "bash", "-c", command]
#hopefully in a format suitable for passing into a subprocess.run call
def yaya_command(command: str, strace_output_path: str) -> list[str]:
    tracked_args = "openat,open,execve,creat,mkdir,rename,unlink,connect,bind,sendto,sendmsg,chdir" # Trace these syscalls
    strace_args  = ["-f", # Follow forks
                    "-s", "256", # Increase max string size for DNS packet capture
                    "-e", tracked_args,
                    "-y", # print out absolute paths on the end
                    f"--output={strace_output_path}" # output to some path so we can capture it,
                    ]
    res = ["strace"] + strace_args + ["--", "bash", "-c", command]
    return res

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
            with open("/subdir_session_id.txt", "r") as f:
                s = f.read()
            tmp = s.split("\n")
            subdir = tmp[0]
            session_id = tmp[1]
            #cmd_output_path = f"/temp/{subdir}/{session_id}_cmd.txt"
            learn_path = f"/temp/{subdir}/{session_id}_learn.txt"
            dump_path = f"/temp/{subdir}/{session_id}_dump.txt"

            try:
                subprocess.run(
                    ["mkdir", "-p", f"/temp/{subdir}"]
                )
                subprocess.run(
                    ["touch", learn_path, dump_path]
                )
            except Exception as e:
                print(f"Error making subdir directory \"{subdir}\" or learn or dump files, exception: {e}")

            '''
            try:
                subprocess.run(
                    ["PATH=\"$PATH:/this_nono/target/release/\""]
                )
            except Exception as e:
                print(f"Error pathing nono. {e}")
            '''

            #yayaed_cmd = ["python3", "/temp/yaya_companion_2026-06-17.py", f"\"{subcmd}\"" ,subdir, session_id]
            #base_list_cmd = subcmd.split(" ")
            #yayaed_cmd = f"python3 /temp/yaya_companion_2026-06-17.py \"{command}\" \"{subdir}\" \"{session_id}\""

            use_command = []
            yayaed = False
            if "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in command:
                use_command = ["bash", "-c", command]
            elif "pip" in command or "apt" in command:
                use_command = ["bash", "-c", command] #nothing changes
            else:
                use_command = yaya_command(command, learn_path)
                #use_command = ["bash", "-c", command]
                yayaed = True

            result = subprocess.run(
                use_command,
                #shell=True,
                #shell=doshell,
                shell=False,
                text=True,
                cwd=cwd,
                env=os.environ | self.config.env,
                timeout=timeout or self.config.timeout,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE, #idk why its like this by default, im gonna try messing with this?
                stderr=subprocess.STDOUT,
                #stdout=subprocess.STDOUT, #try sending stdout to stdout yknow what im saying
                #stderr=subprocess.PIPE, #this is jank thrown in rn, we'll see if this raises issues with hte rest of the harbor implementation?
                #capture_output=True,
            )
            output = {"output": result.stdout, "returncode": result.returncode, "exception_info": ""}

            #if yayaed, append strace results to end of dump file
            if yayaed:
                try:
                    with open(learn_path, "r") as learn_f, open(dump_path, "a") as dump_f:
                        dump_f.write(f"Bash: {command}\n")
                        dump_f.write(learn_f.read() + "\n")
                except Exception as e:
                    print(f"Error appending learn results to dump file: {e}")
            else: # i.e. it was an installer line, just append that line to the file
                try:
                    with open(dump_path, "a") as dump_f:
                        dump_f.write(f"Bash: {command}\n")
                except Exception as e:
                    print(f"Error appending package bash command to dump file: {e}")

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
