import asyncio
import json
import logging
import os
from typing import Callable, Optional, Tuple, Dict, Any

from config import AGY_PATH

logger = logging.getLogger(__name__)

def format_tool_status(tool_name: str, tool_info: Dict[str, Any]) -> str:
    params = tool_info.get("parameters", {})
    if tool_name == "run_command":
        cmd = params.get("CommandLine", "")
        if len(cmd) > 35:
            cmd = cmd[:32] + "..."
        return f"💻 Выполняю команду: <code>{cmd}</code>"
    elif tool_name in ("view_file", "read_file"):
        path = params.get("AbsolutePath", params.get("TargetFile", ""))
        return f"📖 Читаю файл: <code>{os.path.basename(path)}</code>"
    elif tool_name in ("write_to_file", "replace_file_content"):
        path = params.get("TargetFile", "")
        return f"✏️ Редактирую файл: <code>{os.path.basename(path)}</code>"
    elif tool_name in ("search_web", "web_search"):
        q = params.get("query", "")
        return f"🌐 Поиск в сети: <code>{q[:25]}</code>"
    elif tool_name == "list_dir":
        path = params.get("DirectoryPath", "")
        return f"📁 Просмотр папки: <code>{os.path.basename(path) or '.'}</code>"
    elif tool_name == "grep_search":
        q = params.get("query", "")
        return f"🔍 Поиск в коде: <code>{q[:25]}</code>"
    else:
        return f"⚙️ Работаю с инструментом: <code>{tool_name}</code>"

class AGYBridge:
    def __init__(self, executable_path: str = AGY_PATH):
        self.executable_path = executable_path
        self._active_processes: Dict[str, asyncio.subprocess.Process] = {}
        self._cancelled_keys: set = set()

    def cancel_active_process(self, key: Any) -> bool:
        """Cancels a running subprocess for the specified session_key or user_id."""
        k = str(key)
        proc = self._active_processes.get(k)
        if proc and proc.returncode is None:
            try:
                self._cancelled_keys.add(k)
                proc.kill()
                self._active_processes.pop(k, None)
                logger.info(f"Killed process for key {k}")
                return True
            except Exception as e:
                logger.error(f"Failed to kill process for key {k}: {e}")
        return False

    def is_process_running(self, key: Any) -> bool:
        """Checks if a process is currently running for the given session_key/user_id."""
        k = str(key)
        proc = self._active_processes.get(k)
        return bool(proc and proc.returncode is None)

    async def execute_prompt(
        self,
        prompt: str,
        session_key: Optional[str] = None,
        user_id: Optional[int] = None,
        conversation_id: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        execution_mode: Optional[str] = None,
        working_dir: Optional[str] = None,
        auto_approve: bool = True,
        disable_slash_commands: bool = False,
        on_status: Optional[Callable[[str], Any]] = None
    ) -> Tuple[str, Optional[str], Dict[str, Any]]:
        """
        Executes a prompt via Antigravity CLI and returns (response_text, new_conversation_id, stats).
        Calls on_status(status_str) whenever tool actions or state changes occur.
        """
        cmd = [self.executable_path, "-p", prompt, "--output-format", "stream-json"]

        if conversation_id:
            cmd.extend(["--conversation", conversation_id])

        if model:
            cmd.extend(["--model", model])

        if effort and effort in ("low", "medium", "high", "xhigh", "max"):
            cmd.extend(["--effort", effort])

        if execution_mode and execution_mode in ("accept-edits", "plan"):
            cmd.extend(["--mode", execution_mode])

        if auto_approve:
            cmd.append("--dangerously-skip-permissions")

        if disable_slash_commands:
            cmd.append("--disable-slash-commands")

        cwd = working_dir if (working_dir and os.path.exists(working_dir)) else os.getcwd()

        logger.info(f"Running AGY command: {' '.join(cmd[:6])}... in {cwd}")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        proc_key = str(session_key if session_key is not None else (user_id if user_id is not None else "default"))
        self._active_processes[proc_key] = proc

        response_text = ""
        final_conv_id = conversation_id
        stats = {}
        last_status = ""

        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break

                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                try:
                    event_data = json.loads(line_str)
                except json.JSONDecodeError:
                    continue

                event = event_data.get("event")
                
                # Check for init event
                if event == "init":
                    init_conv = event_data.get("conversation_id")
                    if init_conv:
                        final_conv_id = init_conv

                # Check for step updates (tools, agent response, thinking)
                elif event == "step_update":
                    step = event_data.get("step_update", {})
                    conv_id = step.get("conversation_id")
                    if conv_id:
                        final_conv_id = conv_id

                    step_type = step.get("step_type")
                    state = step.get("state")

                    if state == "ACTIVE" and on_status:
                        if step_type == "tool":
                            tool_name = step.get("tool_name", "")
                            tool_info = step.get("tool_info", {})
                            status_msg = format_tool_status(tool_name, tool_info)
                            if status_msg != last_status:
                                last_status = status_msg
                                await on_status(status_msg)
                        elif step_type == "agent_response":
                            if last_status != "✍️ Формирую ответ...":
                                last_status = "✍️ Формирую ответ..."
                                await on_status(last_status)

                # Check for final result
                elif event == "result":
                    res = event_data.get("result", {})
                    conv_id = res.get("conversation_id")
                    if conv_id:
                        final_conv_id = conv_id
                    response_text = res.get("response", "")
                    stats = {
                        "status": res.get("status"),
                        "duration_seconds": res.get("duration_seconds"),
                        "usage": res.get("usage", {})
                    }

            stdout_extra, stderr = await proc.communicate()
            if proc_key in self._cancelled_keys:
                self._cancelled_keys.discard(proc_key)
                return "🛑 Операция была прервана пользователем.", final_conv_id, {}

            if proc.returncode != 0 and not response_text:
                err_msg = stderr.decode("utf-8", errors="replace").strip()
                if not err_msg and stdout_extra:
                    err_msg = stdout_extra.decode("utf-8", errors="replace").strip()

                # If the command failed because it's not available in print mode, and we didn't pass --disable-slash-commands yet:
                if "is not available in print mode" in err_msg and not disable_slash_commands:
                    logger.warning("Retrying prompt with --disable-slash-commands...")
                    return await self.execute_prompt(
                        prompt=prompt,
                        session_key=session_key,
                        user_id=user_id,
                        conversation_id=conversation_id,
                        model=model,
                        effort=effort,
                        execution_mode=execution_mode,
                        working_dir=working_dir,
                        auto_approve=auto_approve,
                        disable_slash_commands=True,
                        on_status=on_status
                    )

                logger.error(f"AGY process failed with code {proc.returncode}: {err_msg}")
                return f"⚠️ Ошибка выполнения Antigravity CLI (код {proc.returncode}):\n{err_msg}", final_conv_id, {}

            return response_text or "Ответ пуст.", final_conv_id, stats

        except asyncio.CancelledError:
            try:
                proc.kill()
            except Exception:
                pass
            return "🛑 Операция была прервана пользователем.", final_conv_id, {}
        except Exception as e:
            logger.exception("Exception during AGY execution")
            try:
                proc.kill()
            except Exception:
                pass
            return f"❌ Ошибка вызова Antigravity: {str(e)}", final_conv_id, {}
        finally:
            self._active_processes.pop(proc_key, None)
            self._cancelled_keys.discard(proc_key)

    async def execute_shell_command(self, command: str, cwd: str) -> Tuple[str, int]:
        """
        Executes a shell command directly on the host operating system.
        """
        working_directory = cwd if (cwd and os.path.exists(cwd)) else os.getcwd()
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=working_directory,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            out_str = stdout.decode("utf-8", errors="replace")
            err_str = stderr.decode("utf-8", errors="replace")
            combined = ""
            if out_str:
                combined += out_str
            if err_str:
                if combined:
                    combined += "\n--- STDERR ---\n"
                combined += err_str
            return combined.strip() or "(Команда выполнена успешно, вывод пуст)", proc.returncode
        except Exception as e:
            return f"Ошибка запуска команды: {e}", -1

agy_bridge = AGYBridge()
