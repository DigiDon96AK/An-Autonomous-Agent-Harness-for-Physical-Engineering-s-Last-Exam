# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Production-grade Plan-Work-Verify Agent Harness for Physical-Engineering's Last Exam (PLE)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import posixpath
import re
import sys
import time
from typing import Any

import openai
import prompts
import taskbridge
from tools import (
    BUILTIN_TOOLS,
    MCP_CATALOG_TOOLS,
    READ_ONLY_TOOLS,
    TOOL_RESULT_CHARS,
    clip,
)

# Configuration from environment
MODEL = os.environ.get("MODEL_NAME", "gemini-3.8-flash")
MAX_EFFORT_MODELS = ("claude", "glm", "gpt-6", "deepseek-v4")
EFFORT = os.environ.get("REASONING_EFFORT") or (
    "max" if any(m in MODEL.lower() for m in MAX_EFFORT_MODELS) else "high"
)
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "32000"))
COMPACT_AT_TOKENS = int(os.environ.get("COMPACT_AT_TOKENS", "250000"))

# Time budgeting fractions (sum <= 1.0)
PLAN_SHARE = 0.12
CHECK_SHARE = 0.25
WRAP_UP_SEC = 240
MIN_STEP_SEC = 120
MAX_FIX_ROUNDS = 2
MAX_NUDGES = 3

# Retry settings for transient API issues
RETRY_WAITS = (5, 15, 30, 60, 120)
CONTEXT_FULL = re.compile(
    r"context.?(length|window)|prompt is too long|maximum prompt length|exceed[^.]{0,40}(token|context)",
    re.I,
)

DIRECT_MCP_CHARS = 40_000
MAX_DIRECT_TOOLS = 64

# OpenAI Async Client configured to talk to the local Model Proxy / Vertex AI endpoint
client = openai.AsyncOpenAI(max_retries=5, timeout=1800)


class MCPServer:
  """Client interface to a task-provided Model Context Protocol (MCP) server."""

  def __init__(self, session: taskbridge.Session, spec: dict[str, Any]):
    self.session = session
    self.name = spec.get("name", "apps")
    self.url = spec.get("url", "")
    self.tools: list[dict[str, Any]] = []
    self._msg_id = 0

  async def post(self, payload: dict[str, Any]) -> dict[str, Any]:
    """Posts a JSON-RPC request to the MCP endpoint via taskbridge http."""
    body_bytes = json.dumps(payload).encode("utf-8")
    resp = await self.session.http(
        "POST",
        self.url,
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        body=body_bytes,
        timeout_sec=120,
    )
    if not resp.get("ok"):
      raise taskbridge.TaskError(f"HTTP request to MCP server {self.name} failed: status {resp.get('status')}")
    body_raw = resp.get("body") or b""
    text = body_raw.decode("utf-8", "replace")
    # Parse SSE or direct JSON response
    for line in text.splitlines():
      if line.startswith("data:"):
        text = line[5:].strip()
        break
    try:
      return json.loads(text)
    except Exception as exc:
      raise taskbridge.TaskError(f"Invalid JSON from MCP server {self.name}: {exc}")

  async def start(self) -> None:
    """Initializes the MCP session and fetches tool schemas."""
    init_res = await self.post({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "gemini-engineering-harness", "version": "1.0"},
        },
    })
    tools_res = await self.post({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {},
    })
    res_obj = tools_res.get("result") or {}
    self.tools = res_obj.get("tools") or []

  async def call(self, tool_name: str, arguments: dict[str, Any]) -> str:
    """Executes a tool on this MCP server."""
    self._msg_id += 1
    res = await self.post({
        "jsonrpc": "2.0",
        "id": 100 + self._msg_id,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    })
    if "error" in res:
      return f"MCP tool error: {res['error']}"
    content = (res.get("result") or {}).get("content") or []
    out = []
    for item in content:
      if isinstance(item, dict) and item.get("type") == "text":
        out.append(item.get("text", ""))
      elif isinstance(item, str):
        out.append(item)
    return "\n".join(out) or "(no output)"


class HarnessRunner:
  """Manages tool execution, MCP relaying, and agent turn loops for a session."""

  def __init__(self, session: taskbridge.Session):
    self.session = session
    self.mcp: dict[str, tuple[MCPServer, dict[str, Any]]] = {}
    self.tools: list[dict[str, Any]] = list(BUILTIN_TOOLS)
    self.finish_margin = 60.0

  def path(self, p: str) -> str:
    """Resolves relative paths against the task working directory."""
    return p if p.startswith("/") else posixpath.join(self.session.workdir, p)

  async def setup_mcp(self) -> str:
    """Discovers task MCP servers and registers tools."""
    for s_spec in self.session.mcp_servers:
      server = MCPServer(self.session, s_spec)
      try:
        await server.start()
      except Exception as exc:
        await self.session.log(f"MCP server {server.name} skipped: {exc}")
        continue
      for t in server.tools:
        safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", f"{server.name}__{t['name']}")
        self.mcp[safe_name] = (server, t)
      await self.session.log(f"Registered MCP server {server.name} ({len(server.tools)} tools)")

    if not self.mcp:
      return ""

    direct_tools = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": (t.get("description") or t["name"])[:1024],
                "parameters": (t.get("inputSchema") or {"type": "object", "properties": {}}),
            },
        }
        for name, (_, t) in self.mcp.items()
    ]
    if (
        len(json.dumps(direct_tools)) <= DIRECT_MCP_CHARS
        and len(self.tools) + len(direct_tools) <= MAX_DIRECT_TOOLS
    ):
      self.tools.extend(direct_tools)
      return "\n\nTask-provided MCP tools are available directly as functions."

    self.tools.extend(MCP_CATALOG_TOOLS)
    catalog_lines = []
    for name, (server, t) in self.mcp.items():
      desc = (t.get("description") or "").split("\n")[0][:80]
      catalog_lines.append(f"- {name}: {desc}")
    return (
        "\n\nTask MCP tools available via mcp_find and mcp_call:\n"
        + "\n".join(catalog_lines)
    )

  def find_mcp(self, query: str) -> str:
    """Searches MCP tools matching query."""
    q_words = [w for w in re.split(r"[^a-z0-9]+", query.lower()) if w]
    results = []
    for name, (_, t) in self.mcp.items():
      text = f"{name} {t.get('description') or ''}".lower()
      score = sum(3 for w in q_words if w in name.lower()) + sum(1 for w in q_words if w in text)
      if score > 0 or not q_words:
        results.append((score, name, t))
    results.sort(key=lambda x: x[0], reverse=True)
    out = []
    for _, name, t in results[:8]:
      out.append(f"Tool: {name}\nDescription: {t.get('description', '')}\nSchema: {json.dumps(t.get('inputSchema', {}))}\n")
    return "\n".join(out) or "No matching MCP tools found."

  async def bash(self, command: str, timeout_sec: int = 120) -> str:
    """Runs a shell command in the task container."""
    cap = int(self.session.limits.get("exec_timeout_sec") or 600)
    left = self.session.time_left()
    if left is not None:
      cap = min(cap, max(1, int(left - self.finish_margin)))
    res = await self.session.exec(command, taskbridge.positive_int(timeout_sec, 120, cap))
    note = " (timed out)" if res.get("timed_out") else ""
    return f"exit code {res['exit_code']}{note}\n{res['output'] or '(no output)'}"

  async def read_file(self, path: str) -> str:
    """Reads a text file from the task container."""
    res = await self.session.read_file(self.path(path))
    content_bytes = res.get("content") or b""
    try:
      return content_bytes.decode("utf-8")
    except UnicodeDecodeError:
      return f"(binary file, {len(content_bytes)} bytes)"

  async def write_file(self, path: str, content: str, append: bool = False) -> str:
    """Writes or appends to a file in the task container."""
    data = content.encode("utf-8")
    target = self.path(path)
    res = await self.session.write_file(target, data, append=append)
    verb = "appended" if append else "wrote"
    return f"ok: {verb} {res.get('size', len(data))} bytes to {target}"

  async def edit_file(self, path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
    """Performs exact text replacement in a file."""
    target = self.path(path)
    current = await self.read_file(target)
    if current.startswith("error:") or current.startswith("(binary"):
      return f"error: cannot edit {target}"
    count = current.count(old_text)
    if count == 0:
      return f"error: old_text not found in {target}"
    if count > 1 and not replace_all:
      return f"error: old_text occurs {count} times in {target}; set replace_all=true to replace all"
    updated = current.replace(old_text, new_text) if replace_all else current.replace(old_text, new_text, 1)
    await self.session.write_file(target, updated.encode("utf-8"), append=False)
    return f"ok: replaced {count} occurrence(s) in {target}"

  async def python_calc(self, code: str, timeout_sec: int = 60) -> str:
    """Executes a Python snippet in the task container for deterministic math."""
    b64 = base64.b64encode(code.encode("utf-8")).decode("ascii")
    cmd = f"python3 -c \"import base64; exec(base64.b64decode('{b64}').decode('utf-8'))\""
    return await self.bash(cmd, timeout_sec=timeout_sec)

  async def run_tool(self, name: str, args: dict[str, Any]) -> str:
    """Dispatches tool calls."""
    await self.session.log(f"tool: {name} {json.dumps(args)[:200]}")
    try:
      if name == "mcp_call":
        t_name = str(args.get("tool", ""))
        t_args = args.get("arguments") or {}
        if isinstance(t_args, str):
          t_args = json.loads(t_args)
        if t_name in self.mcp:
          srv, t = self.mcp[t_name]
          return clip(await srv.call(t["name"], t_args))
        return f"error: unknown MCP tool {t_name}"

      if name in self.mcp:
        srv, t = self.mcp[name]
        return clip(await srv.call(t["name"], args))

      if name == "mcp_find":
        return self.find_mcp(str(args.get("query", "")))

      handler = {
          "bash": self.bash,
          "read_file": self.read_file,
          "write_file": self.write_file,
          "edit_file": self.edit_file,
          "python_calc": self.python_calc,
      }.get(name)

      if handler:
        return clip(await handler(**args))
      return f"error: no tool named {name}"
    except Exception as exc:
      return f"error running tool {name}: {type(exc).__name__}: {exc}"


def marked_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
  """Adds ephemeral cache breakpoints for Claude endpoints if needed."""
  if "claude" not in MODEL.lower():
    return messages
  out = list(messages)
  for idx in {0, len(out) - 1}:
    if 0 <= idx < len(out):
      content = out[idx].get("content")
      if isinstance(content, str) and content:
        out[idx] = {**out[idx], "content": [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]}
  return out


async def compact_context(
    session: taskbridge.Session,
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
  """Compacts earlier turns with an engineering summary when near token limits."""
  await session.log("Context compaction triggered.")
  summary_msgs = [
      *messages,
      {"role": "user", "content": prompts.SUMMARY_PROMPT},
  ]
  try:
    resp = await client.chat.completions.create(
        model=MODEL,
        messages=marked_messages(summary_msgs),
        reasoning_effort="low",
        max_completion_tokens=4096,
    )
    notes = resp.choices[0].message.content or "No notes produced."
  except Exception as exc:
    await session.log(f"Compaction failed: {exc}; dropping early turns directly.")
    notes = "Early turns omitted due to context limits."

  system_turn = messages[0] if messages and messages[0].get("role") == "system" else None
  new_convo = []
  if system_turn:
    new_convo.append(system_turn)
  new_convo.append({
      "role": "user",
      "content": f"[CONTINUING PREVIOUS WORK - ENGINEERING STATE MEMO]\n\n{notes}",
  })
  return new_convo


async def run_agent_role(
    runner: HarnessRunner,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    until_sec: float,
    *,
    is_final: bool = False,
) -> str:
  """Runs an agent loop until completion or time budget expiration."""
  session = runner.session
  nudges = 0
  accumulated_text = ""
  waits = iter(RETRY_WAITS)

  while True:
    left = session.time_left()
    if left is not None and left <= until_sec:
      await session.log(f"Step reached allocated time budget ({left:.0f}s left). Wrapping up.")
      return accumulated_text or session.final_message

    timeout = left - until_sec if left is not None else 1800

    try:
      reply = await asyncio.wait_for(
          client.chat.completions.create(
              model=MODEL,
              messages=marked_messages(messages),
              tools=tools,
              tool_choice="auto",
              reasoning_effort=EFFORT,
              max_completion_tokens=MAX_OUTPUT_TOKENS,
          ),
          timeout=timeout,
      )
    except asyncio.TimeoutError:
      await session.log("LLM API call timed out.")
      return accumulated_text or session.final_message
    except openai.APIError as exc:
      err_msg = str(exc)
      await session.log(f"API Error: {err_msg}")
      if CONTEXT_FULL.search(err_msg):
        messages[:] = await compact_context(session, messages)
        continue
      wait_time = next(waits, None)
      if wait_time and (left is None or left > wait_time + 60):
        await asyncio.sleep(wait_time)
        continue
      raise

    waits = iter(RETRY_WAITS)
    msg = reply.choices[0].message
    cut_off = reply.choices[0].finish_reason == "length"
    text = accumulated_text + (msg.content or "")

    if is_final and text:
      session.final_message = text

    turn = msg.model_dump(exclude_none=True)
    messages.append(turn)

    if not msg.tool_calls:
      if cut_off and nudges < MAX_NUDGES:
        nudges += 1
        accumulated_text = text
        messages.append({"role": "user", "content": "Your response was cut off; please continue."})
        continue
      return text

    nudges = 0
    accumulated_text = ""
    for call in msg.tool_calls:
      fn_name = call.function.name
      fn_args = json.loads(call.function.arguments) if call.function.arguments else {}
      result_str = await runner.run_tool(fn_name, fn_args)
      messages.append({
          "role": "tool",
          "tool_call_id": call.id,
          "content": result_str,
      })

    if left is not None:
      rem_mins = int((session.time_left() - until_sec) // 60)
      messages[-1]["content"] += f"\n[{rem_mins} minutes remaining for this step]"


async def solve(session: taskbridge.Session) -> str:
  """Executes the complete Plan-Work-Verify engineering loop."""
  total_time = session.time_left()

  def until(share: float) -> float:
    return 0.0 if total_time is None else total_time * share

  runner = HarnessRunner(session)
  mcp_info = await runner.setup_mcp()

  task_instruction = session.instruction
  workdir = session.workdir
  mins = int(total_time // 60) if total_time else 120

  # -------------------------------------------------------------
  # STAGE 1: PLANNER
  # -------------------------------------------------------------
  await session.log("--- STAGE 1: PLANNER ---")
  planner_prompt = prompts.PLANNER_SYSTEM.format(workdir=workdir)
  planner_messages = [
      {"role": "system", "content": planner_prompt},
      {"role": "user", "content": f"TASK SPECIFICATION:\n{task_instruction}"},
  ]
  plan = await run_agent_role(
      runner,
      planner_messages,
      READ_ONLY_TOOLS,
      until(1.0 - PLAN_SHARE),
  )
  await session.log(f"Plan generated ({len(plan)} chars)")

  # -------------------------------------------------------------
  # STAGE 2: WORKER
  # -------------------------------------------------------------
  await session.log("--- STAGE 2: WORKER ---")
  worker_prompt = prompts.WORKER_SYSTEM.format(
      workdir=workdir,
      minutes=mins,
      mcp_info=mcp_info,
  )
  worker_messages = [
      {"role": "system", "content": worker_prompt},
      {
          "role": "user",
          "content": f"TASK:\n{task_instruction}\n\nENGINEERING EXECUTION PLAN:\n{plan}\n\nExecute the plan and generate all required deliverables.",
      },
  ]
  summary = await run_agent_role(
      runner,
      worker_messages,
      runner.tools,
      until(CHECK_SHARE),
      is_final=True,
  )

  # -------------------------------------------------------------
  # STAGE 3: VERIFIER & SELF-CORRECTION
  # -------------------------------------------------------------
  for fix_round in range(MAX_FIX_ROUNDS):
    left = session.time_left()
    if left is not None and left < MIN_STEP_SEC + WRAP_UP_SEC:
      await session.log("Insufficient time for further verification rounds. Finalizing.")
      break

    await session.log(f"--- STAGE 3: VERIFIER (Round {fix_round + 1}) ---")
    verifier_prompt = prompts.VERIFIER_SYSTEM.format(workdir=workdir)
    verifier_review = (
        f"TASK:\n{task_instruction}\n\n"
        f"PLAN:\n{plan}\n\n"
        f"ENGINEER'S DELIVERABLE SUMMARY:\n{summary}"
    )
    verifier_messages = [
        {"role": "system", "content": verifier_prompt},
        {"role": "user", "content": verifier_review},
    ]

    verdict = await run_agent_role(
        runner,
        verifier_messages,
        READ_ONLY_TOOLS,
        until(CHECK_SHARE / 2),
    )
    await session.log(f"Verifier verdict: {verdict[:400]}")

    clean_verdict = verdict.strip("*_. \r\n\t").upper()
    if clean_verdict in ("", "PASS"):
      await session.log("Verifier passed all deliverables!")
      break

    # If gaps identified, instruct Worker to fix
    await session.log("Verifier found gaps. Engaging Worker for correction.")
    worker_messages.append({
        "role": "user",
        "content": (
            f"A quality assurance engineer inspected your deliverables and identified these gaps:\n\n"
            f"{verdict}\n\n"
            "Please fix every issue identified, verify the updated files, and provide your updated final summary."
        ),
    })
    summary = (
        await run_agent_role(
            runner,
            worker_messages,
            runner.tools,
            WRAP_UP_SEC,
            is_final=True,
        )
        or summary
    )

  return summary or session.final_message or "Task deliverables generated and verified."


if __name__ == "__main__":
  taskbridge.run(solve)

