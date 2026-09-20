"""One OpenAI/open-weight-backed agent's daemon.

    kuska daemon <agent-name>

Same shape as the Codex daemon, but using OpenAI's API directly with a custom
tool-calling loop and a generic MCP client. Works with:
  - OpenAI API (GPT-4, GPT-3.5, etc.)
  - Local models via ollama or llama.cpp (via a compatible OpenAI-like API)

No in-process tool registration - reaches kuska via a stdio MCP server just
like Codex does (`kuska mcp`), but with a home-rolled tool-calling loop
instead of relying on SDK support.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import kuska as core


def log(line: str, error: bool = False) -> None:
    """Daemons usually run under nohup or systemd, so never buffer their log."""
    print(line, file=sys.stderr if error else sys.stdout, flush=True)


def mcp_command() -> list[str]:
    """How to start this project's MCP server: the frozen binary, or python -m."""
    if getattr(sys, "frozen", False):  # PyInstaller build
        return [sys.executable]
    return [sys.executable, "-m", "kuska"]


async def run_agent(
    project: Path,
    agent_name: str,
    cfg: dict,
    prompt: str,
    mono,
) -> tuple[str, int, int, float]:
    """One fresh invocation with tool-calling loop.

    Returns (text, input_tokens, output_tokens, cost).
    """
    import os

    import openai
    from mcp.client.session import ClientSession
    from mcp.client.stdio import stdio_client

    # Initialize OpenAI client
    api_key = cfg.get("api_key") or os.getenv("OPENAI_API_KEY", "")
    base_url = cfg.get("base_url")  # For local models or proxies
    model = cfg.get("model", "gpt-4")

    if base_url:
        client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
    else:
        client = openai.AsyncOpenAI(api_key=api_key)

    # Set up MCP server connection for tool calls
    command, *head = mcp_command()
    mcp_args = [*head, "--project", str(project), "mcp", "--agent", agent_name]

    async with stdio_client(
        [command, *mcp_args], timeout=30
    ) as (read, write):
        async with ClientSession(read, write) as session:
            # List available tools from MCP server
            tools_response = await session.list_tools()
            mcp_tools = tools_response.tools if tools_response else []

            # Convert MCP tools to OpenAI format
            openai_tools = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or "",
                        "parameters": tool.input_schema or {"type": "object", "properties": {}},
                    },
                }
                for tool in mcp_tools
            ]

            # System prompt with available tools
            system = f"""{core.read_prompt(project, agent_name)}

You have access to MCP tools. Use them to accomplish your task."""

            messages = [{"role": "user", "content": prompt}]
            total_input_tokens = 0
            total_output_tokens = 0

            max_iterations = 20  # Prevent infinite loops
            for iteration in range(max_iterations):
                # Call the model
                response = await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "system", "content": system}, *messages],
                    tools=openai_tools if openai_tools else None,
                    tool_choice="auto" if openai_tools else None,
                    temperature=0.0,
                )

                # Track tokens
                if response.usage:
                    total_input_tokens += response.usage.prompt_tokens
                    total_output_tokens += response.usage.completion_tokens

                # Process response
                if not response.choices:
                    break

                choice = response.choices[0]
                assistant_message = {"role": "assistant", "content": choice.message.content or ""}
                messages.append(assistant_message)

                # Check for tool calls
                tool_calls = choice.message.tool_calls or []
                if not tool_calls:
                    # No tool calls - we're done
                    mono.record("text", choice.message.content or "(no output)")
                    break

                # Process each tool call
                tool_results = []
                for tool_call in tool_calls:
                    tool_name = tool_call.function.name
                    tool_args = json.loads(tool_call.function.arguments or "{}")

                    mono.tool_call(tool_name, tool_args)

                    # Call the MCP tool
                    try:
                        result = await session.call_tool(tool_name, tool_args)
                        result_text = ""
                        if result.content:
                            for block in result.content:
                                if hasattr(block, "text"):
                                    result_text = block.text
                                    break
                        is_error = result.is_error or False
                    except Exception as e:
                        result_text = f"error: {e}"
                        is_error = True

                    mono.tool_result(tool_name, result_text, is_error=is_error)
                    tool_results.append(
                        {
                            "tool_call_id": tool_call.id,
                            "content": result_text,
                        }
                    )

                # Add tool results to messages for next iteration
                if tool_results:
                    messages.append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_call_id": tr["tool_call_id"],
                                    "content": tr["content"],
                                }
                                for tr in tool_results
                            ],
                        }
                    )

            # Get final response (last assistant message)
            final_text = ""
            for msg in reversed(messages):
                if msg["role"] == "assistant" and msg.get("content"):
                    final_text = msg["content"]
                    break

            # Calculate cost
            price_in = float(cfg.get("price_in_per_mtok", 0) or 0)
            price_out = float(cfg.get("price_out_per_mtok", 0) or 0)
            cost = (total_input_tokens * price_in + total_output_tokens * price_out) / 1_000_000

            return final_text or "(no output)", total_input_tokens, total_output_tokens, cost


def run_daemon(
    project: Path,
    agent_name: str,
    poll_interval: float = 2.0,
    max_tasks: int | None = None,
    quiet: bool = False,
) -> None:
    """Main daemon loop."""
    import os

    db = core.connect(core.db_path(project))
    core.init_db(db)
    core.sync_agents_from_config(db, project)
    cfg = core.agent_config(project, agent_name)

    # Validate required config - api_key can come from env var
    has_api_key = cfg.get("api_key") or os.getenv("OPENAI_API_KEY")
    has_base_url = cfg.get("base_url")
    if not has_api_key and not has_base_url:
        raise SystemExit(
            f"[{agent_name}] OpenAI daemon requires 'api_key' in config.toml or OPENAI_API_KEY env var"
        )

    log(f"[{agent_name}] openai daemon up on {project} (model={cfg.get('model') or 'gpt-4'})")
    core.heartbeat(db, agent_name, "idle")
    handled = 0
    try:
        while max_tasks is None or handled < max_tasks:
            task = core.wait_for_task(db, agent_name, poll_interval)
            handled += 1
            log(f"[{agent_name}] task {task['id']}: {task['title']}")
            core.heartbeat(db, agent_name, "working", task["id"])
            started = core.now()
            prompt = core.compose_task_prompt(db, agent_name, task)
            mono = core.Monologue(db, agent_name, task["id"], quiet=quiet)
            mono.record("prompt", prompt)

            try:
                text, tok_in, tok_out, cost = asyncio.run(
                    run_agent(project, agent_name, cfg, prompt, mono)
                )
                text = text.strip()
            except Exception as exc:
                mono.record("error", f"run failed: {exc}")
                core.send_message(db, agent_name, core.HUMAN, task["id"], "blocker", f"run failed: {exc}")
                core.update_task_status(db, task["id"], "blocked")
                log(f"[{agent_name}] task {task['id']} failed: {exc}", error=True)
            else:
                # keyword args: finish_task also takes cache and round counts,
                # which this backend does not report
                core.finish_task(
                    db, agent_name, task["id"], text, started,
                    input_tokens=tok_in, output_tokens=tok_out, cost_usd=cost,
                )
                final = (core.get_task(db, task["id"]) or task)["status"]
                mono.record("result", text, label=f"{final} - ${cost:.4f}, {tok_in}/{tok_out} tok")
                log(f"[{agent_name}] task {task['id']} {final} (${cost:.4f}, {tok_in}/{tok_out} tok)")
            core.release_run(db, mono.run_id)
            core.heartbeat(db, agent_name, "idle")
    finally:
        core.release_files(db, agent_name)
        core.heartbeat(db, agent_name, "offline")
        db.close()
