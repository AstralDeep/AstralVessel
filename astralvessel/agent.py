# The agent loop: ask the model, run the tools it asks for, feed back the results, repeat until it answers.
# One Turn owns limits, cancellation and accounting, and emits the event vocabulary as it goes.

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from . import events as ev
from .manifest import BUILTIN_TOOLS, AgentPackage
from .model import ModelClient, ModelError, StepResult
from .primitives import PRESENT_DESCRIPTION, PRESENT_PARAMETERS, validate_components
from .sandbox import ToolRunner
from .tools import BUILTIN_SCHEMAS, WEB_TOOLS, clip, tool_spec
from .web import webfetch, websearch

Emit = Callable[[dict[str, Any]], Awaitable[None]]
EVENT_OUTPUT_CHARS = 4000
STEP_LIMIT_NOTE = ("You have reached the step limit for this turn. Do not call any more tools. "
                   "Answer now with what you have found, and say what you could not check.")


def tool_specs(package: AgentPackage) -> list[dict[str, Any]]:
    specs = []
    for name in package.manifest.tools.builtin:
        schema = BUILTIN_SCHEMAS[name]
        specs.append(tool_spec(name, schema["description"], schema["parameters"]))
    for tool in package.declared:
        specs.append(tool_spec(tool.name, tool.description, tool.parameters))
    if package.manifest.output.primitives:
        specs.append(tool_spec("present", PRESENT_DESCRIPTION, PRESENT_PARAMETERS))
    return specs


def system_prompt(package: AgentPackage) -> str:
    sandbox = package.manifest.sandbox
    mounts = "".join(f"\n- {m.target} ({'read-only' if m.read_only else 'writable'})" for m in sandbox.mounts)
    lines = [package.prompt.strip(), "", "## Environment", f"Today is {date.today().isoformat()}.",
             f"Tool commands run in {sandbox.workdir}."]
    if mounts:
        lines.append("Mounted data:" + mounts)
    lines.append("Text you write in the same message as a tool call is shown as brief narration, not as your "
                 "answer. Your final message, with no tool calls, is the answer: write it in full there.")
    if package.manifest.output.primitives:
        lines.append("If tables, metrics or charts would help, call `present` once before your final message. "
                     "Then write the complete answer in Markdown; never refer the reader to the visuals instead.")
    return "\n".join(lines)


class Turn:
    def __init__(self, package: AgentPackage, model: ModelClient, runner: ToolRunner, emit: Emit):
        self.package = package
        self.model = model
        self.runner = runner
        self.emit = emit
        self.allowed = set(package.manifest.tools.builtin) | {t.name for t in package.declared}
        if package.manifest.output.primitives:
            self.allowed.add("present")
        self.new_messages: list[dict[str, Any]] = []
        self.components: list[dict[str, Any]] = []
        self.tokens = {"input": 0, "output": 0}
        self.steps = 0
        self.answer = ""

    async def run(self, history: list[dict[str, Any]], user_text: str, *, conversation_id: str,
                  turn_id: str) -> dict[str, Any]:
        started = time.monotonic()
        self.new_messages.append({"role": "user", "content": user_text})
        await self.emit(ev.event(ev.TURN_STARTED, conversation_id=conversation_id, turn_id=turn_id))
        status, error = ev.STATUS_OK, None
        try:
            async with asyncio.timeout(self.package.manifest.sandbox.deadline_seconds):
                status = await self._loop(history)
        except TimeoutError:
            status, error = ev.STATUS_DEADLINE, "the turn ran past its time limit"
        except asyncio.CancelledError:
            status = ev.STATUS_CANCELLED
        except ModelError as exc:
            status, error = ev.STATUS_MODEL_ERROR, str(exc)
        self._close_dangling_calls()
        if self.new_messages[-1]["role"] != "assistant":
            self.new_messages.append({"role": "assistant", "content": f"[this turn ended early: {status}]"})
        result = {
            "status": status,
            "text": self.answer,
            "components": self.components,
            "tokens": dict(self.tokens),
            "steps": self.steps,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
        if error:
            result["error"] = error
        await self.emit(ev.event(ev.TURN_FINISHED, conversation_id=conversation_id, turn_id=turn_id, **result))
        return result

    async def _loop(self, history: list[dict[str, Any]]) -> str:
        specs = tool_specs(self.package)
        base = [{"role": "system", "content": system_prompt(self.package)}, *history]
        limit = self.package.manifest.model.max_steps
        while True:
            over_limit = self.steps >= limit
            messages = base + self.new_messages
            if over_limit:
                messages = messages + [{"role": "user", "content": STEP_LIMIT_NOTE}]
            step = await self._step(messages, [] if over_limit else specs)
            if not step.tool_calls or over_limit:
                self.answer = step.text.strip()
                self.new_messages.append({"role": "assistant", "content": step.text})
                return ev.STATUS_STEP_LIMIT if over_limit else ev.STATUS_OK
            assistant: dict[str, Any] = {
                "role": "assistant",
                "content": step.text or None,
                "tool_calls": [{"id": c.id, "type": "function",
                                "function": {"name": c.name, "arguments": c.arguments}} for c in step.tool_calls],
            }
            if step.reasoning:
                assistant["reasoning_content"] = step.reasoning
            self.new_messages.append(assistant)
            for call in step.tool_calls:
                output = await self._call_tool(call.id, call.name, call.arguments)
                self.new_messages.append({"role": "tool", "tool_call_id": call.id, "content": output})

    async def _step(self, messages: list[dict[str, Any]], specs: list[dict[str, Any]]) -> StepResult:
        self.steps += 1
        result = StepResult()
        async for kind, value in self.model.stream(messages, specs):
            if kind == "text":
                await self.emit(ev.event(ev.TEXT_DELTA, step=self.steps, content=value))
            elif kind == "reasoning":
                await self.emit(ev.event(ev.REASONING_DELTA, step=self.steps, content=value))
            elif kind == "done":
                result = value
        self.tokens["input"] += result.usage.get("input", 0)
        self.tokens["output"] += result.usage.get("output", 0)
        await self.emit(ev.event(ev.STEP_FINISHED, step=self.steps, tool_calls=len(result.tool_calls),
                                 tokens=result.usage, finish_reason=result.finish_reason))
        return result

    async def _call_tool(self, call_id: str, name: str, raw_arguments: str) -> str:
        started = time.monotonic()
        try:
            arguments = json.loads(raw_arguments or "{}")
            if not isinstance(arguments, dict):
                raise ValueError
        except ValueError:
            arguments = None
        await self.emit(ev.event(ev.TOOL_STARTED, call_id=call_id, name=name,
                                 arguments=arguments if arguments is not None else raw_arguments[:2000]))
        if name not in self.allowed:
            output, failed = f"tool {name} does not exist", True
        elif arguments is None:
            output, failed = "the arguments were not a valid JSON object", True
        else:
            output, failed = await self._dispatch(name, arguments)
        limit = self.package.manifest.sandbox.tool_output_chars
        output, truncated = clip(output, limit)
        shown, _ = clip(output, EVENT_OUTPUT_CHARS)
        await self.emit(ev.event(ev.TOOL_FINISHED, call_id=call_id, name=name, output=shown, error=failed,
                                 truncated=truncated, elapsed_ms=int((time.monotonic() - started) * 1000)))
        return output

    async def _dispatch(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        if name == "present":
            try:
                self.components = validate_components(arguments.get("components"))
            except ValueError as exc:
                return f"not attached: {exc}. Fix the components and call present again.", True
            return f"attached {len(self.components)} components to the answer", False
        if name in WEB_TOOLS:
            return await (webfetch if name == "webfetch" else websearch)(arguments)
        if name in BUILTIN_TOOLS or name in {t.name for t in self.package.declared}:
            return await self.runner.run(name, arguments)
        return f"tool {name} does not exist", True

    def _close_dangling_calls(self) -> None:
        answered = {m["tool_call_id"] for m in self.new_messages if m["role"] == "tool"}
        for message in list(self.new_messages):
            for call in message.get("tool_calls") or []:
                if call["id"] not in answered:
                    self.new_messages.append({"role": "tool", "tool_call_id": call["id"],
                                              "content": "[not run: the turn ended first]"})
                    answered.add(call["id"])
