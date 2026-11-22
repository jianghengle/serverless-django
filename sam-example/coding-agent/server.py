"""Coding agent WebSocket server.

Receives chat messages from the frontend, calls Claude on Bedrock (Converse API)
with file/command/dev-server tools, and streams progress events back.

Protocol (JSON text frames)
  client -> server: {"type": "auth", "token": "..."}   (must be the first message)
                    {"type": "user_message", "text": "..."}
                    {"type": "commit_deploy"}   (commit, push, then deploy changed parts)
  server -> client: {"type": "auth_ok"} | {"type": "auth_failed", "message": "..."}
  server -> client: {"type": "git_status", "files": [{status, path}], "backend": bool, "frontend": bool}
  server -> client: {"type": "tool_call", "name": "...", "status": "calling" | "done"}
                    {"type": "assistant_message", "message": "..."}
                    {"type": "error", "message": "..."}

Security model: binds to loopback only and rejects browser connections whose
Origin is not allow-listed. File tools are confined to the project root, and
commands run without this process's AWS credentials in their environment.
"""
import asyncio
import collections
import hashlib
import json
import os
import re
import shlex
import signal
import sys
import urllib.error
import urllib.request

import boto3
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

# ─── Configuration ────────────────────────────────────────────────────────────
BACKEND_URL = os.environ.get("CODING_AGENT_BACKEND_URL", "http://localhost:3000")
AUTH_TIMEOUT = 10
DEPLOY_TIMEOUT = 1800
SESSIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions")
HISTORY_SENT = 10      # display messages replayed to the client on connect
MAX_DISPLAY_KEPT = 200  # display messages stored per user
MAX_LLM_TURNS = 20      # user turns kept in the model's context
MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
REGION = "us-west-2"
HOST = "127.0.0.1"
PORT = 8765
ALLOWED_ORIGINS = os.environ.get(
    "CODING_AGENT_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
).split(",")

ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))  # sam-example/
FRONTEND = os.path.join(ROOT, "frontend")

MAX_ITERATIONS = 25
MAX_TOOL_OUTPUT = 20000
COMMAND_TIMEOUT = 120
# Path components the agent may never touch (secrets, VCS, deps, and this server itself).
DENIED_PARTS = {".git", ".env", "node_modules", "venv", ".venv", "coding-agent", ".aws-sam", ".aws", ".ssh"}
SKIP_DIRS = DENIED_PARTS | {"__pycache__", "dist"}

SYSTEM_PROMPT = f"""You are a coding agent working on a web app in {ROOT}.
- backend/: Python AWS SAM application (template.yaml in the project root).
- frontend/: Vue 2 + Vite app. The Vite dev server hot-reloads, so frontend edits need no restart.
Paths you pass to tools are relative to the project root. Read code before changing it and keep
edits minimal. Prefer edit_file over rewriting whole files. After backend changes that need a
rebuild (template.yaml, dependencies, new handlers), call restart_dev_server with target "backend".
Use get_dev_server_logs to check for errors. Be concise in your final answer."""

TOOLS = [
    {
        "name": "read_file",
        "description": "Read a text file. Returns numbered lines.",
        "schema": {"path": "string"},
        "required": ["path"],
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a file with the given content. Creates parent directories.",
        "schema": {"path": "string", "content": "string"},
        "required": ["path", "content"],
    },
    {
        "name": "edit_file",
        "description": "Replace one exact occurrence of old_str with new_str in a file. Fails if old_str is missing or appears more than once.",
        "schema": {"path": "string", "old_str": "string", "new_str": "string"},
        "required": ["path", "old_str", "new_str"],
    },
    {
        "name": "list_dir",
        "description": "List entries of a directory ('.' for the project root).",
        "schema": {"path": "string"},
        "required": ["path"],
    },
    {
        "name": "search",
        "description": "Search file contents for a plain-text substring. Returns path:line:text matches.",
        "schema": {"pattern": "string", "path": "string"},
        "required": ["pattern"],
    },
    {
        "name": "run_command",
        "description": "Run a command (no shell: pipes, redirects and && are not supported) with a 120s timeout. cwd is relative to the project root.",
        "schema": {"command": "string", "cwd": "string"},
        "required": ["command"],
    },
    {
        "name": "restart_dev_server",
        "description": "Restart a dev server. 'frontend' runs `npm run dev` in frontend/. 'backend' runs `sam build` then `sam local start-api` in the project root. 'both' restarts both.",
        "schema": {"target": "string"},
        "required": ["target"],
    },
    {
        "name": "get_dev_server_logs",
        "description": "Return the most recent output of a dev server started by restart_dev_server.",
        "schema": {"target": "string"},
        "required": ["target"],
    },
]


def bedrock_tool_config():
    return {
        "tools": [
            {
                "toolSpec": {
                    "name": t["name"],
                    "description": t["description"],
                    "inputSchema": {
                        "json": {
                            "type": "object",
                            "properties": {k: {"type": v} for k, v in t["schema"].items()},
                            "required": t["required"],
                        }
                    },
                }
            }
            for t in TOOLS
        ]
    }


bedrock = boto3.client("bedrock-runtime", region_name=REGION)


# ─── Path sandbox ─────────────────────────────────────────────────────────────
def safe_path(rel):
    """Resolve rel inside ROOT (following symlinks) and reject denied locations."""
    full = os.path.realpath(os.path.join(ROOT, rel or "."))
    if full != ROOT and not full.startswith(ROOT + os.sep):
        raise ValueError(f"Path escapes the project root: {rel}")
    parts = set(os.path.relpath(full, ROOT).split(os.sep))
    denied = parts & DENIED_PARTS
    if denied or any(p.startswith(".env") for p in parts):
        raise ValueError(f"Access to this path is not allowed: {rel}")
    return full


def truncate(text):
    if len(text) <= MAX_TOOL_OUTPUT:
        return text
    return text[:MAX_TOOL_OUTPUT] + f"\n... [truncated {len(text) - MAX_TOOL_OUTPUT} chars]"


# ─── Subprocess helpers ───────────────────────────────────────────────────────
def clean_env():
    """Environment for child processes: no AWS credentials from this server."""
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TERM", "TMPDIR") if k in os.environ}
    env.setdefault("TERM", "dumb")
    return env


async def kill_group(proc):
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        await asyncio.wait_for(proc.wait(), 10)
    except (ProcessLookupError, asyncio.TimeoutError):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def exec_capture(argv, cwd, timeout=COMMAND_TIMEOUT):
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env=clean_env(), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        await kill_group(proc)
        return f"[timed out after {timeout}s]"
    return f"[exit code {proc.returncode}]\n" + out.decode(errors="replace")


# ─── Dev server management ────────────────────────────────────────────────────
DEV_SERVERS = {
    "frontend": {"cwd": FRONTEND, "pre": [], "port": 5173,
                 "cmd": ["npm", "run", "dev", "--", "--port", "5173", "--strictPort"]},
    "backend": {"cwd": ROOT, "pre": [["sam", "build"]], "port": 3000,
                "cmd": ["sam", "local", "start-api", "--port", "3000"]},
}
dev_procs = {}
dev_logs = {name: collections.deque(maxlen=300) for name in DEV_SERVERS}


async def free_port(port):
    """Stop whatever is listening on a port, including servers started outside this process."""
    out = await exec_capture(["lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"], ROOT, timeout=10)
    pids = [int(p) for p in out.split("\n", 1)[-1].split() if p.isdigit()]
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for _ in range(20):  # wait up to 10s for the port to be released
        out = await exec_capture(["lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"], ROOT, timeout=10)
        remaining = [int(p) for p in out.split("\n", 1)[-1].split() if p.isdigit()]
        if not remaining:
            return
        await asyncio.sleep(0.5)
    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await asyncio.sleep(0.5)


async def pump_logs(name, proc):
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        dev_logs[name].append(line.decode(errors="replace").rstrip())
    dev_logs[name].append(f"[process exited with code {await proc.wait()}]")


async def restart_one(name):
    spec = DEV_SERVERS[name]
    old = dev_procs.pop(name, None)
    dev_logs[name].clear()
    for pre in spec["pre"]:
        result = await exec_capture(pre, spec["cwd"], timeout=600)
        dev_logs[name].append(f"$ {' '.join(pre)}\n{result}")
        if not result.startswith("[exit code 0]"):
            if old:
                dev_procs[name] = old  # keep the running server
            return f"{name}: `{' '.join(pre)}` failed:\n{truncate(result)}"
    if old:
        await kill_group(old)
    await free_port(spec["port"])  # after the build, so a failed build leaves the old server up
    proc = await asyncio.create_subprocess_exec(
        *spec["cmd"], cwd=spec["cwd"], env=clean_env(), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    dev_procs[name] = proc
    asyncio.create_task(pump_logs(name, proc))
    await asyncio.sleep(4)
    status = "running" if proc.returncode is None else f"exited with code {proc.returncode}"
    return f"{name}: {status}\n" + truncate("\n".join(list(dev_logs[name])[-30:]))


async def stop_all_dev_servers():
    for proc in list(dev_procs.values()):
        await kill_group(proc)


# ─── Tool implementations ─────────────────────────────────────────────────────
def tool_read_file(path):
    full = safe_path(path)
    with open(full, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    return truncate("\n".join(f"{i}: {line}" for i, line in enumerate(lines, 1)))


def tool_write_file(path, content):
    full = safe_path(path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Wrote {len(content)} chars to {path}"


def tool_edit_file(path, old_str, new_str):
    full = safe_path(path)
    with open(full, encoding="utf-8") as f:
        text = f.read()
    count = text.count(old_str)
    if count != 1:
        raise ValueError(f"old_str must match exactly once, found {count} matches")
    with open(full, "w", encoding="utf-8") as f:
        f.write(text.replace(old_str, new_str, 1))
    return f"Edited {path}"


def tool_list_dir(path):
    full = safe_path(path)
    entries = []
    for name in sorted(os.listdir(full)):
        if name in SKIP_DIRS or name.startswith(".env"):
            continue
        entries.append(name + ("/" if os.path.isdir(os.path.join(full, name)) else ""))
    return "\n".join(entries) or "(empty)"


def tool_search(pattern, path="."):
    base = safe_path(path)
    matches = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.startswith(".env"):
                continue
            fp = os.path.join(dirpath, fn)
            try:
                with open(fp, encoding="utf-8") as f:
                    for i, line in enumerate(f, 1):
                        if pattern in line:
                            matches.append(f"{os.path.relpath(fp, ROOT)}:{i}:{line.rstrip()[:200]}")
                            if len(matches) >= 200:
                                return "\n".join(matches) + "\n[stopped at 200 matches]"
            except (UnicodeDecodeError, OSError):
                continue
    return "\n".join(matches) or "No matches"


async def tool_run_command(command, cwd="."):
    argv = shlex.split(command)
    if not argv:
        raise ValueError("Empty command")
    if any(".aws" in a or ".ssh" in a for a in argv):
        raise ValueError("Commands may not reference credential directories")
    return truncate(await exec_capture(argv, safe_path(cwd)))


async def tool_restart_dev_server(target):
    names = list(DEV_SERVERS) if target == "both" else [target]
    if any(n not in DEV_SERVERS for n in names):
        raise ValueError("target must be 'frontend', 'backend' or 'both'")
    return "\n\n".join([await restart_one(n) for n in names])


def tool_get_dev_server_logs(target):
    if target not in DEV_SERVERS:
        raise ValueError("target must be 'frontend' or 'backend'")
    lines = list(dev_logs[target])[-80:]
    return truncate("\n".join(lines)) or "(no output; server not started via restart_dev_server?)"


async def run_tool(name, args):
    """Run a tool. Returns (text, is_error)."""
    try:
        if name == "read_file":
            out = await asyncio.to_thread(tool_read_file, args["path"])
        elif name == "write_file":
            out = await asyncio.to_thread(tool_write_file, args["path"], args["content"])
        elif name == "edit_file":
            out = await asyncio.to_thread(tool_edit_file, args["path"], args["old_str"], args["new_str"])
        elif name == "list_dir":
            out = await asyncio.to_thread(tool_list_dir, args.get("path", "."))
        elif name == "search":
            out = await asyncio.to_thread(tool_search, args["pattern"], args.get("path", "."))
        elif name == "run_command":
            out = await tool_run_command(args["command"], args.get("cwd", "."))
        elif name == "restart_dev_server":
            out = await tool_restart_dev_server(args["target"])
        elif name == "get_dev_server_logs":
            out = tool_get_dev_server_logs(args["target"])
        else:
            return f"Unknown tool: {name}", True
        return out, False
    except Exception as e:  # report to the model so it can correct itself
        return f"{type(e).__name__}: {e}", True


# ─── Sessions (per user, persisted) ───────────────────────────────────────────
class Session:
    """Conversation state for one user. Survives reconnects and server restarts.

    llm:     Bedrock Converse messages (includes tool_use / tool_result turns).
    display: user-visible messages [{role, text, toolCalls}], replayed to the client on connect.
    """

    def __init__(self, key):
        self.path = os.path.join(SESSIONS_DIR, hashlib.sha256(key.encode()).hexdigest() + ".json")
        self.llm = []
        self.display = []
        self.ws = None      # the currently connected client, if any
        self.busy = False
        self.task = None
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.llm, self.display = data["llm"], data["display"]
        except (OSError, ValueError, KeyError):
            pass

    def save(self):
        self.display = self.display[-MAX_DISPLAY_KEPT:]
        os.makedirs(SESSIONS_DIR, mode=0o700, exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"llm": self.llm, "display": self.display}, f)
        os.replace(tmp, self.path)

    def trim_llm(self):
        """Keep the last MAX_LLM_TURNS user turns. Cut only at a turn start so tool_use/tool_result pairs stay intact."""
        starts = [i for i, m in enumerate(self.llm)
                  if m["role"] == "user" and any("text" in b for b in m["content"])]
        if len(starts) > MAX_LLM_TURNS:
            del self.llm[:starts[-MAX_LLM_TURNS]]

    async def emit(self, payload):
        ws = self.ws
        if ws is None:
            return
        try:
            await ws.send(json.dumps(payload))
        except ConnectionClosed:
            pass  # client went away; the turn keeps running and the result is saved


SESSIONS = {}


def get_session(key):
    if key not in SESSIONS:
        SESSIONS[key] = Session(key)
    return SESSIONS[key]


# ─── Agent loop ───────────────────────────────────────────────────────────────
def converse(messages):
    return bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": SYSTEM_PROMPT}],
        messages=messages,
        toolConfig=bedrock_tool_config(),
        inferenceConfig={"maxTokens": 4096},
    )


async def run_agent(session, user_text):
    """Run one user turn. Appends to session.llm and session.display."""
    messages = session.llm
    messages.append({"role": "user", "content": [{"text": user_text}]})
    tool_calls = []
    final = "I reached the maximum number of steps. Please try a more specific request."
    for _ in range(MAX_ITERATIONS):
        response = await asyncio.to_thread(converse, messages)
        message = response["output"]["message"]
        messages.append(message)

        tool_uses = [b["toolUse"] for b in message["content"] if "toolUse" in b]
        if response["stopReason"] != "tool_use" or not tool_uses:
            final = "\n".join(b["text"] for b in message["content"] if "text" in b).strip() or "(no response)"
            break

        results = []
        for tu in tool_uses:
            await session.emit({"type": "tool_call", "name": tu["name"], "status": "calling"})
            text, is_error = await run_tool(tu["name"], tu["input"])
            await session.emit({"type": "tool_call", "name": tu["name"], "status": "done"})
            tool_calls.append({"name": tu["name"], "status": "done"})
            result = {"toolUseId": tu["toolUseId"], "content": [{"text": text or "(no output)"}]}
            if is_error:
                result["status"] = "error"
            results.append({"toolResult": result})
        messages.append({"role": "user", "content": results})
    else:
        # Ran out of steps with a dangling tool_result turn; add a closing assistant turn so the history stays valid.
        messages.append({"role": "assistant", "content": [{"text": final}]})

    session.display.append({"role": "assistant", "text": final, "toolCalls": tool_calls})
    await session.emit({"type": "assistant_message", "message": final})


async def run_turn(session, user_text):
    start = len(session.llm)
    try:
        await run_agent(session, user_text)
    except Exception as e:
        print(f"Agent error: {e!r}", file=sys.stderr)
        del session.llm[start:]  # drop the incomplete turn so the history stays valid
        message = f"{type(e).__name__}: {e}"
        session.display.append({"role": "assistant", "text": "Error: " + message, "toolCalls": []})
        await session.emit({"type": "error", "message": message})
    finally:
        session.busy = False
        session.trim_llm()
        session.save()
        await push_git_status(session)


# ─── Git status and "Commit & Deploy" ─────────────────────────────────────────
async def run_raw(argv, cwd, env=None, timeout=COMMAND_TIMEOUT):
    """Run a command; returns (exit_code, combined_output)."""
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env=env or clean_env(), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,  # no controlling tty, so ssh/git can't block on a prompt
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        await kill_group(proc)
        return -1, f"[timed out after {timeout}s]"
    return proc.returncode, out.decode(errors="replace")


def is_backend_path(path):
    return path.startswith("backend/") or path in ("template.yaml", "samconfig.toml")


def is_frontend_path(path):
    return path.startswith("frontend/")


async def get_git_status():
    """Uncommitted changes under the project root, or None if this isn't a git repo."""
    code, prefix = await run_raw(["git", "rev-parse", "--show-prefix"], ROOT)
    if code != 0:
        return None
    prefix = prefix.strip()
    code, out = await run_raw(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", "."], ROOT)
    if code != 0:
        return None
    files = []
    tokens = out.split("\0")
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if len(tok) < 4:
            continue
        xy, path = tok[:2], tok[3:]
        if xy[0] in "RC":
            i += 1  # skip the original path of a rename/copy
        if prefix and path.startswith(prefix):
            path = path[len(prefix):]
        if xy == "??" or "A" in xy:
            status = "new"
        elif "D" in xy:
            status = "deleted"
        else:
            status = "modified"
        files.append({"status": status, "path": path})
    return {
        "files": files,
        "backend": any(is_backend_path(f["path"]) for f in files),
        "frontend": any(is_frontend_path(f["path"]) for f in files),
    }


async def push_git_status(session):
    try:
        status = await get_git_status()
    except Exception as e:
        print(f"git status failed: {e!r}", file=sys.stderr)
        status = None
    await session.emit({"type": "git_status", "files": status["files"] if status else [],
                        "backend": bool(status and status["backend"]),
                        "frontend": bool(status and status["frontend"])})


def _generate_commit_message(summary):
    response = bedrock.converse(
        modelId=MODEL_ID,
        messages=[{"role": "user", "content": [{"text":
            "Write a one-line git commit message (imperative mood, max 72 characters, no quotes, "
            "no prefix) for these changes. Reply with the message only.\n\n" + summary[:4000]}]}],
        inferenceConfig={"maxTokens": 60},
    )
    text = "".join(b.get("text", "") for b in response["output"]["message"]["content"])
    return text.strip().splitlines()[0].strip().strip("\"'`")[:72] if text.strip() else ""


async def make_commit_message(status):
    names = "\n".join(f"{f['status']}: {f['path']}" for f in status["files"][:100])
    _, stat = await run_raw(["git", "diff", "HEAD", "--stat", "--", "."], ROOT)
    try:
        message = await asyncio.to_thread(_generate_commit_message, names + "\n\n" + stat)
    except Exception as e:
        print(f"commit message generation failed: {e!r}", file=sys.stderr)
        message = ""
    return message or "Update via coding agent"


def tail(text, n=25):
    return "\n".join(text.strip().splitlines()[-n:])


async def run_deploy(session):
    """Commit and push, then deploy the backend and/or frontend if they changed."""
    env = os.environ.copy()  # deliberate: git push needs the SSH agent, deploys need AWS credentials
    env["GIT_TERMINAL_PROMPT"] = "0"
    tool_calls = []
    try:
        status = await get_git_status()
        if not status or not status["files"]:
            final = "There are no uncommitted changes."
        else:
            message = await make_commit_message(status)
            steps = [
                ("git add", ["git", "add", "."]),
                # Pathspec limits the commit to this project even if other paths are staged.
                ("git commit", ["git", "commit", "-m", message, "--", "."]),
                ("git push", ["git", "push"]),
            ]
            if status["backend"]:
                steps += [("sam build", ["sam", "build"]),
                          ("sam deploy", ["sam", "deploy", "--no-confirm-changeset",
                                          "--no-fail-on-empty-changeset"])]
            if status["frontend"]:
                steps.append(("deploy-frontend.sh", ["./deploy-frontend.sh"]))

            final = None
            frontend_url = None
            for label, argv in steps:
                await session.emit({"type": "tool_call", "name": label, "status": "calling"})
                code, out = await run_raw(argv, ROOT, env=env, timeout=DEPLOY_TIMEOUT)
                await session.emit({"type": "tool_call", "name": label, "status": "done"})
                tool_calls.append({"name": label, "status": "done"})
                if code != 0:
                    final = f"**`{label}` failed** (exit code {code}). Later steps were skipped.\n\n```\n{tail(out)}\n```"
                    break
                if label == "deploy-frontend.sh":
                    m = re.search(r"==> Done:\s*(\S+)", out)       # printed by deploy-frontend.sh
                elif label == "sam deploy":
                    m = re.search(r"Key\s+FrontendUrl\b[\s\S]*?\bValue\s+(\S+)", out)  # stack outputs
                else:
                    m = None
                if m and (label == "deploy-frontend.sh" or not frontend_url):
                    frontend_url = m.group(1)
            if final is None:
                done = [f"Committed and pushed: \"{message}\""]
                if status["backend"]:
                    done.append("Backend deployed")
                if status["frontend"]:
                    done.append("Frontend deployed")
                final = "\n".join(f"- {d}" for d in done)
                if frontend_url:
                    final += f"\n\nFrontend URL: {frontend_url}"
    except Exception as e:
        print(f"Deploy error: {e!r}", file=sys.stderr)
        final = f"Error: {type(e).__name__}: {e}"
    finally:
        session.busy = False
    session.display.append({"role": "assistant", "text": final, "toolCalls": tool_calls})
    session.save()
    await session.emit({"type": "assistant_message", "message": final})
    await push_git_status(session)


def verify_admin(token):
    """Ask the backend to validate the token. Returns a stable user key, or None. Fails closed."""
    if not token or not isinstance(token, str):
        return None
    req = urllib.request.Request(
        BACKEND_URL.rstrip("/") + "/api/user/verify-token",
        data=b"{}", method="POST",
        headers={"Authorization": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                return None
            user = json.loads(resp.read().decode())
    except (urllib.error.URLError, ValueError, OSError):
        return None
    if user.get("isAdmin") is not True:
        return None
    return user.get("email") or user.get("username")


async def handler(ws):
    # The first message must be {type: "auth", token}, verified by the backend as an admin.
    try:
        raw = await asyncio.wait_for(ws.recv(), AUTH_TIMEOUT)
        data = json.loads(raw)
        token = data.get("token") if data.get("type") == "auth" else None
    except (asyncio.TimeoutError, ValueError, AttributeError):
        token = None
    user_key = await asyncio.to_thread(verify_admin, token)
    if not user_key:
        await ws.send(json.dumps({"type": "auth_failed", "message": "Admin authentication required"}))
        await ws.close(1008, "unauthorized")
        return

    session = get_session(user_key)
    session.ws = ws  # a newer connection takes over event delivery from an older one
    await ws.send(json.dumps({"type": "auth_ok"}))
    await ws.send(json.dumps({"type": "history", "busy": session.busy,
                              "messages": session.display[-HISTORY_SENT:]}))
    await push_git_status(session)
    try:
        async for raw in ws:
            try:
                data = json.loads(raw)
                kind = data.get("type")
                text = data["text"].strip() if kind == "user_message" else ""
            except (ValueError, KeyError, AttributeError):
                kind, text = None, ""
            if kind == "commit_deploy":
                if session.busy:
                    await session.emit({"type": "error", "message": "Still working on the previous request"})
                    continue
                session.busy = True
                session.display.append({"role": "user", "text": "Commit & Deploy"})
                session.save()
                session.task = asyncio.create_task(run_deploy(session))
                continue
            if not text:
                await session.emit({"type": "error", "message": "Expected {type: 'user_message', text}"})
                continue
            if session.busy:
                await session.emit({"type": "error", "message": "Still working on the previous request"})
                continue
            session.busy = True
            session.display.append({"role": "user", "text": text})
            session.save()
            # Run as its own task so a disconnect doesn't cancel an in-flight turn.
            session.task = asyncio.create_task(run_turn(session, text))
    finally:
        if session.ws is ws:
            session.ws = None


async def main():
    print(f"Coding agent listening on ws://{HOST}:{PORT}  (project root: {ROOT})")
    try:
        async with serve(handler, HOST, PORT, origins=ALLOWED_ORIGINS):
            await asyncio.Future()  # run forever
    finally:
        await stop_all_dev_servers()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
