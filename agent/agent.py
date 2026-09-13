"""
Malaysia Economic Intelligence Agent — orchestrator.

Connects to the MCP server (server/server.py) as a client, exposes its
tools to Claude, runs an agentic tool-call loop, then runs a SEPARATE
reflection pass that checks the draft answer against the actual tool
results before returning a final answer. 

Requires:
  - The MCP server running separately (`python ../server/server.py`).
  - ANTHROPIC_API_KEY set in the .env file.

Usage:
    python agent.py "What is the current OPR and how has it changed
    over the last 12 months?"
"""

import asyncio
import json
import os
import sys

from anthropic import Anthropic
from dotenv import load_dotenv
from mcp import Client

load_dotenv()  # reads agent/.env if present; real environment variables still take precedence

MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://localhost:8000/mcp")
MODEL = "claude-sonnet-5"
MAX_TOOL_ITERATIONS = 6

WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}

PLANNER_SYSTEM_PROMPT = """You are an assistant answering questions about \
Malaysian economic data (exchange rates, interest rates, the Overnight \
Policy Rate) using the tools provided.

You have two categories of tool:
1. Structured data tools (get_myr_exchange_rate, get_interest_rate_history, \
get_opr_history) — use these for any question about specific historical \
figures, trends, or "what is the current rate" questions. These are the \
ONLY source for numeric claims.
2. web_search — use this ONLY for qualitative context the structured \
tools cannot provide (e.g. "why did BNM cut rates", recent policy \
commentary, news about upcoming decisions). NEVER use web_search to find \
or substitute a numeric rate/exchange figure that the structured tools \
are meant to provide — if a structured tool doesn't have it, say so \
explicitly rather than searching for a number to fill the gap.

Rules:
- Every factual claim must trace to a tool result. Never state a figure \
from general knowledge.
- If a tool returns no data for the requested range, or flags \
out_of_range/found=false, say so explicitly. Do not estimate or \
extrapolate a plausible-sounding number instead.
- get_interest_rate_history does NOT contain OPR — use get_opr_history \
for OPR questions.
- Do not mix different `indicator` values (high/low/avg/start/end) in a \
single exchange-rate comparison without saying so.
- A gap between OPR records is normal (rate unchanged at that meeting), \
not missing data.
- When citing a web search result, note it's from a web source (not the \
structured government data) so the two are never confused in the answer.
"""

REFLECTION_SYSTEM_PROMPT = """You are reviewing a draft answer for \
correctness against the tool calls that were actually made. You are not \
answering the question yourself — you are checking someone else's work.

Check for, and flag, any of the following:
1. A factual claim in the draft that does NOT trace to a specific tool \
result in the log.
2. Mixing incompatible categories (e.g. two different `indicator` values \
treated as comparable, or OPR conflated with fixed-deposit/BLR rates).
3. A case where a tool returned no data / out_of_range / found=false, but \
the draft did not clearly flag that to the user.
4. Any numeric error (wrong sign, wrong percentage direction, etc.) \
versus what the tool log actually shows.

Respond with ONLY a JSON object, no other text, no markdown fences:
{"grounded": true|false, "flags": ["short description", ...], "final_answer": "..."}

If the draft is correct, set grounded=true, flags=[], and final_answer \
equal to the draft (verbatim). If not, set grounded=false, list the \
flags, and write a corrected final_answer that fixes them — including \
adding an explicit "data not available" statement if the draft \
incorrectly implied an answer existed.
"""


def mcp_tools_to_anthropic_format(mcp_tools) -> list[dict]:
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
        }
        for t in mcp_tools
    ]


async def run_agent_loop(question: str, mcp_client: Client, anthropic_client: Anthropic, tools: list[dict]):
    """Runs the planning + tool-call loop. Returns (draft_answer, tool_call_log)."""
    messages = [{"role": "user", "content": question}]
    tool_call_log = []

    for _ in range(MAX_TOOL_ITERATIONS):
        response = anthropic_client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=PLANNER_SYSTEM_PROMPT,
            messages=messages,
            tools=tools,
        )

        if os.environ.get("AGENT_DEBUG"):
            web_search_count = 0
            if response.usage and getattr(response.usage, "server_tool_use", None):
                web_search_count = getattr(response.usage.server_tool_use, "web_search_requests", 0)
            print(f"[DEBUG] web_search_requests = {web_search_count}", file=sys.stderr)

        messages.append(response.to_param())

        if response.stop_reason != "tool_use":
            draft = "".join(block.text for block in response.content if block.type == "text")

            citations = []
            for block in response.content:
                if block.type == "text" and getattr(block, "citations", None):
                    for c in block.citations:
                        try:
                            citations.append(c.model_dump())
                        except Exception:
                            citations.append(str(c))
            if citations:
                tool_call_log.append({"tool": "web_search_citations", "args": {}, "result": citations})
            return draft, tool_call_log

        tool_results_content = []
        for block in response.content:
            if block.type == "tool_use":
                result = await mcp_client.call_tool(block.name, block.input)
                if result.structured_content is not None:
                    result_data = result.structured_content
                else:
                    text_parts = [b.text for b in result.content if getattr(b, "type", None) == "text"]
                    result_data = "\n".join(text_parts)
                tool_call_log.append({"tool": block.name, "args": block.input, "result": result_data})
                tool_results_content.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result_data, default=str),
                })
            elif "web_search" in getattr(block, "type", "") or "server_tool" in getattr(block, "type", ""):
                try:
                    logged = block.model_dump()
                except Exception:
                    logged = str(block)
                tool_call_log.append({"tool": f"web_search ({block.type})", "args": {}, "result": logged})
            elif block.type != "text":
                try:
                    logged = block.model_dump()
                except Exception:
                    logged = str(block)
                tool_call_log.append({"tool": f"unhandled block ({block.type})", "args": {}, "result": logged})
        if not tool_results_content:
            draft = "".join(block.text for block in response.content if block.type == "text")
            if draft:
                citations = []
                for block in response.content:
                    if block.type == "text" and getattr(block, "citations", None):
                        for c in block.citations:
                            try:
                                citations.append(c.model_dump())
                            except Exception:
                                citations.append(str(c))
                if citations:
                    tool_call_log.append({"tool": "web_search_citations", "args": {}, "result": citations})
                return draft, tool_call_log
        messages.append({"role": "user", "content": tool_results_content})

    return "[Stopped: exceeded max tool iterations without a final answer]", tool_call_log


def run_reflection(question: str, draft_answer: str, tool_call_log: list[dict], anthropic_client: Anthropic) -> dict:
    """Separate model call — checks the draft against the tool log. Returns the parsed verdict dict."""
    review_input = {
        "question": question,
        "draft_answer": draft_answer,
        "tool_call_log": tool_call_log,
    }
    response = anthropic_client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=REFLECTION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(review_input, default=str)}],
    )
    raw_text = "".join(block.text for block in response.content if block.type == "text")

    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return {
            "grounded": False,
            "flags": ["Reflection step did not return valid JSON — could not verify this answer."],
            "final_answer": draft_answer,
            "_raw_reflection_output": raw_text,
        }


def summarize_for_display(result_data, head: int = 3, tail: int = 10):
    """
    For our own audit printing only — NOT used for what's sent to the
    model, which always gets the full data. Naive head-truncation of a
    long chronologically-sorted record list hides the most recent
    entries, which is usually exactly what a "current value" or "last
    changed" claim needs to be checked against.
    """
    if isinstance(result_data, dict) and isinstance(result_data.get("records"), list):
        records = result_data["records"]
        if len(records) > head + tail:
            display = dict(result_data)
            display["records"] = {
                "_note": f"{len(records)} total records — showing first {head} and last {tail} for audit purposes",
                "first": records[:head],
                "last": records[-tail:],
            }
            return display
    return result_data


async def main(question: str):
    anthropic_client = Anthropic()

    async with Client(MCP_SERVER_URL) as mcp_client:
        tools_result = await mcp_client.list_tools()
        tools = mcp_tools_to_anthropic_format(tools_result.tools) + [WEB_SEARCH_TOOL]

        draft_answer, tool_call_log = await run_agent_loop(question, mcp_client, anthropic_client, tools)

    verdict = run_reflection(question, draft_answer, tool_call_log, anthropic_client)

    print("\n=== TOOL CALLS (with results) ===")
    for call in tool_call_log:
        print(f"- {call['tool']}({call['args']})")
        print(json.dumps(summarize_for_display(call["result"]), default=str, indent=2))

    print("\n=== DRAFT ANSWER ===")
    print(draft_answer)

    print(f"\n=== REFLECTION (grounded={verdict.get('grounded')}) ===")
    if verdict.get("flags"):
        print("Flags:", verdict["flags"])
    if "_raw_reflection_output" in verdict:
        print("Raw reflection output (JSON parse failed, showing so this is diagnosable):")
        print(verdict["_raw_reflection_output"])

    print("\n=== FINAL ANSWER ===")
    print(verdict.get("final_answer", draft_answer))


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "What is the current OPR?"
    asyncio.run(main(q))