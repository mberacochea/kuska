"""The OpenAI-compatible backend: a home-rolled tool-calling loop, run by loop.py.

Works with the OpenAI API and anything that speaks its chat-completions
dialect (ollama, llama.cpp, vLLM via `base_url`). There is no in-process tool
registration: it reaches kuska through the stdio MCP server (`kuska mcp`),
like Codex does.

Its only tools are kuska's own - no file or shell access - so the workdir a
task runs in does not reach the model.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import openai
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import kuska as core

from . import loop

# the loop has no other way out, so it keeps a turn cap even when
# config.toml sets none
DEFAULT_MAX_TURNS = 20


def mcp_command() -> list[str]:
    """How to start this project's MCP server: the frozen binary, or python -m."""
    if getattr(sys, "frozen", False):  # PyInstaller build
        return [sys.executable]
    return [sys.executable, "-m", "kuska"]


def openai_tool(tool) -> dict:
    """An MCP tool as a chat-completions function definition."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.input_schema or {"type": "object", "properties": {}},
        },
    }


async def call_tool(session, name: str, raw_args: str | None, mono) -> tuple[str, bool]:
    """Run one tool call over MCP; (result text, is_error). Never raises:
    a bad call is the model's to see and recover from."""
    try:
        args = json.loads(raw_args or "{}")
    except json.JSONDecodeError as exc:
        text = f"error: arguments are not valid JSON: {exc}"
        mono.tool_result(name, text, is_error=True)
        return text, True
    mono.tool_call(name, args)
    try:
        result = await session.call_tool(name, args)
        text = "\n".join(block.text for block in result.content or [] if hasattr(block, "text"))
        is_error = bool(result.is_error)
    except Exception as exc:
        text, is_error = f"error: {exc}", True
    mono.tool_result(name, text, is_error=is_error)
    return text, is_error


async def converse(client, session, model: str, system: str, prompt: str, cfg: dict, mono) -> tuple[str, dict]:
    """The tool-calling loop: ask, run the tools it asks for, repeat until it
    answers without any. Returns (text, usage) in the ledger's terms."""
    tools = [openai_tool(t) for t in (await session.list_tools()).tools]
    tool_args = {"tools": tools, "tool_choice": "auto"} if tools else {}
    limits = core.run_limits(cfg)
    max_turns = limits["max_turns"] or DEFAULT_MAX_TURNS
    messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    tokens_in = tokens_out = rounds = 0

    def spent() -> dict:
        return {
            "input_tokens": tokens_in, "output_tokens": tokens_out, "tool_rounds": rounds,
            "cost_usd": core.estimate_cost(cfg, tokens_in, tokens_out),
        }

    for _ in range(max_turns):
        response = await client.chat.completions.create(model=model, messages=messages, **tool_args)
        if response.usage:
            tokens_in += response.usage.prompt_tokens or 0
            tokens_out += response.usage.completion_tokens or 0
        mono.spent = spent()
        if not response.choices:
            raise core.RunAborted("the model returned no choices", spent())
        message = response.choices[0].message
        if message.content:
            mono.record("text", message.content)
        calls = message.tool_calls or []
        if not calls:
            return message.content or "(no output)", spent()
        if limits["max_budget_usd"] and spent()["cost_usd"] >= limits["max_budget_usd"]:
            raise core.RunAborted("hit its max_budget_usd limit", spent())

        # the assistant turn must carry its tool_calls, and every call needs a
        # role "tool" answer with the same id, or the next request is rejected
        messages.append({
            "role": "assistant",
            "content": message.content,
            "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in calls
            ],
        })
        for c in calls:
            rounds += 1
            mono.spent = spent()
            text, _ = await call_tool(session, c.function.name, c.function.arguments, mono)
            messages.append({"role": "tool", "tool_call_id": c.id, "content": text})

    raise core.RunAborted(f"hit its max_turns limit ({max_turns})", spent())


async def run_agent(project: Path, agent_name: str, cfg: dict, prompt: str, mono) -> tuple[str, dict]:
    """One fresh invocation: a client, an MCP session acting as this agent,
    and the conversation between them."""
    client_args = {"api_key": cfg.get("api_key") or os.getenv("OPENAI_API_KEY", "")}
    if cfg.get("base_url"):  # local models or proxies
        client_args["base_url"] = cfg["base_url"]
    client = openai.AsyncOpenAI(**client_args)

    command, *head = mcp_command()
    server = StdioServerParameters(
        command=command, args=[*head, "--project", str(project), "mcp", "--agent", agent_name],
    )
    system = f"{core.read_prompt(project, agent_name)}\n\nYou have access to MCP tools. Use them to accomplish your task."
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        return await converse(client, session, cfg.get("model") or "gpt-4", system, prompt, cfg, mono)


def make_runner(db, project: Path, agent_name: str, cfg: dict):
    if not (cfg.get("api_key") or os.getenv("OPENAI_API_KEY") or cfg.get("base_url")):
        raise SystemExit(
            f"[{agent_name}] OpenAI daemon requires 'api_key' in config.toml or OPENAI_API_KEY env var"
        )

    async def run(prompt: str, workdir: Path, mono) -> tuple[str, dict]:
        return await run_agent(project, agent_name, cfg, prompt, mono)

    return run


def run_daemon(
    project: Path,
    agent_name: str,
    poll_interval: float = 2.0,
    max_tasks: int | None = None,
    quiet: bool = False,
    stop=None,
) -> None:
    loop.run_daemon(project, agent_name, "openai", make_runner, poll_interval, max_tasks, quiet, stop=stop)
