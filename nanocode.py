#!/usr/bin/env python3
"""nanocode - minimal xai code alternative"""

import glob as globlib
import json
import os
import re
import subprocess
import threading
import time

from xai_sdk import Client
from xai_sdk.chat import system, tool, tool_result, user
from xai_sdk.tools import code_execution, web_search, x_search

MODEL = os.environ.get("MODEL", "grok-4-1-fast")

# ANSI colors
RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
BLUE, CYAN, GREEN, YELLOW, RED = (
    "\033[34m",
    "\033[36m",
    "\033[32m",
    "\033[33m",
    "\033[31m",
)

# --- Tool implementations ---


def read(args):
    lines = open(args["path"]).readlines()
    offset = args.get("offset", 0)
    limit = args.get("limit", len(lines))
    selected = lines[offset : offset + limit]
    return "".join(f"{offset + idx + 1:4}| {line}" for idx, line in enumerate(selected))


def write(args):
    with open(args["path"], "w") as f:
        f.write(args["content"])
    return "ok"


def edit(args):
    text = open(args["path"]).read()
    old, new = args["old"], args["new"]
    if old not in text:
        return "error: old_string not found"
    count = text.count(old)
    if not args.get("all") and count > 1:
        return f"error: old_string appears {count} times, must be unique (use all=true)"
    replacement = (
        text.replace(old, new) if args.get("all") else text.replace(old, new, 1)
    )
    with open(args["path"], "w") as f:
        f.write(replacement)
    return "ok"


def glob(args):
    pattern = (args.get("path", ".") + "/" + args["pat"]).replace("//", "/")
    files = globlib.glob(pattern, recursive=True)
    files = sorted(
        files,
        key=lambda f: os.path.getmtime(f) if os.path.isfile(f) else 0,
        reverse=True,
    )
    return "\n".join(files) or "none"


def grep(args):
    pattern = re.compile(args["pat"])
    hits = []
    for filepath in globlib.glob(args.get("path", ".") + "/**", recursive=True):
        try:
            for line_num, line in enumerate(open(filepath), 1):
                if pattern.search(line):
                    hits.append(f"{filepath}:{line_num}:{line.rstrip()}")
        except Exception:
            pass
    return "\n".join(hits[:50]) or "none"


def bash(args):
    proc = subprocess.Popen(
        args["cmd"],
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output_lines = []
    try:
        while True:
            if not proc.stdout:
                break
            line = proc.stdout.readline()
            if not line and proc.poll() is not None:
                break
            if line:
                print(f"  {DIM}│ {line.rstrip()}{RESET}", flush=True)
                output_lines.append(line)
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        output_lines.append("\n(timed out after 30s)")
    return "".join(output_lines).strip() or "(empty)"


# --- Tool definitions: (description, schema, function) ---

TOOLS = {
    "read": (
        "Read file with line numbers (file path, not directory)",
        {"path": "string", "offset": "number?", "limit": "number?"},
        read,
    ),
    "write": (
        "Write content to file",
        {"path": "string", "content": "string"},
        write,
    ),
    "edit": (
        "Replace old with new in file (old must be unique unless all=true)",
        {"path": "string", "old": "string", "new": "string", "all": "boolean?"},
        edit,
    ),
    "glob": (
        "Find files by pattern, sorted by mtime",
        {"pat": "string", "path": "string?"},
        glob,
    ),
    "grep": (
        "Search files for regex pattern",
        {"pat": "string", "path": "string?"},
        grep,
    ),
    "bash": (
        "Run shell command",
        {"cmd": "string"},
        bash,
    ),
}


def run_tool(name, args):
    try:
        return TOOLS[name][2](args)
    except Exception as err:
        return f"error: {err}"


def make_schema():
    result = []
    for name, (description, params, _fn) in TOOLS.items():
        properties = {}
        required = []
        for param_name, param_type in params.items():
            is_optional = param_type.endswith("?")
            base_type = param_type.rstrip("?")
            properties[param_name] = {
                "type": "integer" if base_type == "number" else base_type
            }
            if not is_optional:
                required.append(param_name)
        result.append(
            tool(
                name=name,
                description=description,
                parameters={
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            )
        )
    return result


def separator():
    return f"{DIM}{'─' * min(os.get_terminal_size().columns, 80)}{RESET}"


def spinner(spinner_event):
    while not spinner_event.is_set():
        for spin in ["/", "-", "\\", "|"]:
            if spinner_event.is_set():
                break
            print(f"\rLoading... {spin}", end="", flush=True)
            time.sleep(0.1)


def render_markdown(text):
    return re.sub(r"\*\*(.+?)\*\*", f"{BOLD}\\1{RESET}", text)


def main():
    print(f"{BOLD}nanocode{RESET} | {DIM}{MODEL} (xAI) | {os.getcwd()}{RESET}\n")
    client = Client(api_key=os.environ.get("XAI_API_KEY"))
    chat = client.chat.create(
        model=MODEL,
        tools=make_schema() + [web_search(), x_search()],
        use_encrypted_content=True,
    )
    system_prompt = (
        f"Concise assistant with coding and search capabilities. cwd: {os.getcwd()}"
    )
    chat.append(system(system_prompt))

    while True:
        try:
            print(separator())
            user_input = input(f"{BOLD}{BLUE}❯{RESET} ").strip()
            print(separator())
            if not user_input:
                continue
            if user_input in ("/q", "exit"):
                break
            if user_input == "/c":
                chat = client.chat.create(
                    model=MODEL,
                    tools=make_schema()
                    + [
                        web_search(),
                        x_search(enable_image_understanding=True),
                        code_execution(),
                    ],
                    use_encrypted_content=True,
                )
                chat.append(system(system_prompt))
                print(f"{GREEN}⏺ Cleared conversation{RESET}")
                continue

            chat.append(user(user_input))

            # agentic loop: keep calling API until no more tool calls
            while True:
                tool_calls = []
                response_content = ""
                spinner_event = threading.Event()
                t = threading.Thread(target=spinner, args=(spinner_event,))
                t.start()
                response = None
                for response, chunk in chat.stream():
                    if not spinner_event.is_set():
                        spinner_event.set()
                        print("\r" + " " * 20 + "\r", end="", flush=True)
                    if chunk.content:
                        response_content += chunk.content
                        print(chunk.content, end="", flush=True)
                    for tool_call in chunk.tool_calls:
                        tool_calls.append(tool_call)

                t.join()

                if not response or not tool_calls:
                    break

                chat.append(response)

                for tool_call in tool_calls:
                    tool_name = tool_call.function.name
                    if tool_name in TOOLS:
                        tool_args = json.loads(tool_call.function.arguments)
                        arg_preview = str(list(tool_args.values())[0])[:50]
                        print(
                            f"\n{GREEN}⏺ {tool_name.capitalize()}{RESET}({DIM}{arg_preview}{RESET})"
                        )

                        result = run_tool(tool_name, tool_args)
                        result_lines = result.split("\n")
                        preview = result_lines[0][:60]
                        if len(result_lines) > 1:
                            preview += f" ... +{len(result_lines) - 1} lines"
                        elif len(result_lines[0]) > 60:
                            preview += "..."
                        print(f"  {DIM}⎿  {preview}{RESET}")

                        chat.append(tool_result(result))
                    else:
                        print(f"\n{GREEN}⏺ Unknown tool: {tool_name}{RESET}")

            print()

        except (KeyboardInterrupt, EOFError):
            break
        except Exception as err:
            print(f"{RED}⏺ Error: {err}{RESET}")


if __name__ == "__main__":
    main()
