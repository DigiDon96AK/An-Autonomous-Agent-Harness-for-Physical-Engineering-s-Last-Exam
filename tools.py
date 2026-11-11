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

"""Tool definitions and JSON Schema specifications for the engineering harness."""

from __future__ import annotations

import json
from typing import Any

TOOL_RESULT_CHARS = 25_000


def make_tool(
    name: str,
    description: str,
    required: list[str],
    **properties: Any,
) -> dict[str, Any]:
  """Constructs an OpenAI/Gemini compatible function tool definition.

  Args:
    name: Function name.
    description: Description of tool utility.
    required: List of mandatory argument names.
    **properties: JSON Schema attribute definitions.

  Returns:
    Tool schema dictionary.
  """
  return {
      "type": "function",
      "function": {
          "name": name,
          "description": description,
          "parameters": {
              "type": "object",
              "properties": properties,
              "required": required,
          },
      },
  }


BUILTIN_TOOLS = [
    make_tool(
        "bash",
        "Execute a shell command inside the task container. Each command starts in a fresh "
        "subshell in the current working directory. Files persist across calls.",
        ["command"],
        command={"type": "string", "description": "The bash command line to run."},
        timeout_sec={
            "type": "integer",
            "description": "Timeout in seconds (default 120, max 600).",
        },
    ),
    make_tool(
        "read_file",
        "Read contents of a text file from the task container. For large files, returns the "
        "head and tail.",
        ["path"],
        path={"type": "string", "description": "Path to the file to read (relative or absolute)."},
    ),
    make_tool(
        "write_file",
        "Write content to a file in the task container. Automatically creates missing parent directories.",
        ["path", "content"],
        path={"type": "string", "description": "Destination file path."},
        content={"type": "string", "description": "Text content to write."},
        append={
            "type": "boolean",
            "description": "If true, append to the file rather than overwriting.",
        },
    ),
    make_tool(
        "edit_file",
        "Replace an exact string in a file with new content. old_text must match exactly once "
        "unless replace_all is true.",
        ["path", "old_text", "new_text"],
        path={"type": "string", "description": "File path to modify."},
        old_text={"type": "string", "description": "The exact character sequence to replace."},
        new_text={"type": "string", "description": "The replacement string."},
        replace_all={
            "type": "boolean",
            "description": "If true, replaces all occurrences of old_text.",
        },
    ),
    make_tool(
        "python_calc",
        "Execute a Python engineering calculation snippet inside the task container. Use this "
        "for closed-form equations, unit conversions, and data extraction without shell escaping issues.",
        ["code"],
        code={"type": "string", "description": "Python 3 code to execute."},
        timeout_sec={"type": "integer", "description": "Timeout in seconds (default 60)."},
    ),
]

# Read-only tools for the Planner and Verifier roles
READ_ONLY_TOOLS = [
    make_tool(
        "bash",
        "Execute a non-destructive read-only command (e.g. ls, find, cat, grep, python -c) in the container.",
        ["command"],
        command={"type": "string", "description": "Shell command to run."},
        timeout_sec={"type": "integer", "description": "Timeout in seconds (default 60)."},
    ),
    make_tool(
        "read_file",
        "Read contents of a text file from the task container.",
        ["path"],
        path={"type": "string", "description": "Path to the file to inspect."},
    ),
    make_tool(
        "python_calc",
        "Execute a Python calculation snippet to inspect or verify numbers without modifying files.",
        ["code"],
        code={"type": "string", "description": "Python 3 code to execute."},
        timeout_sec={"type": "integer", "description": "Timeout in seconds (default 60)."},
    ),
]

MCP_CATALOG_TOOLS = [
    make_tool(
        "mcp_find",
        "Search task-provided MCP tools by name or description keywords.",
        ["query"],
        query={"type": "string", "description": "Search query or keyword."},
    ),
    make_tool(
        "mcp_call",
        "Invoke a task-provided MCP tool.",
        ["tool", "arguments"],
        tool={"type": "string", "description": "Full name of the tool as listed."},
        arguments={"type": "object", "description": "Arguments object for the tool."},
    ),
]


def clip(text: str, limit: int = TOOL_RESULT_CHARS) -> str:
  """Clips text preserving head and tail if it exceeds limit.

  Args:
    text: Input string.
    limit: Maximum characters allowed.

  Returns:
    Truncated or original string.
  """
  if len(text) <= limit:
    return text
  keep = limit // 2 - 40
  omitted = len(text) - 2 * keep
  return f"{text[:keep]}\n... [{omitted} characters truncated] ...\n{text[-keep:]}"

