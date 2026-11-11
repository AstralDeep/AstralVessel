# The agent loop: ask the model, run the tools it asks for, feed back the results, repeat until it answers.
# One Turn owns limits, cancellation and accounting, and emits the event vocabulary as it goes.

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from . import events as ev
from . import files as outputs
from .manifest import BUILTIN_TOOLS, AgentPackage
from .model import ModelClient, ModelError, StepResult
from .primitives import PRESENT_DESCRIPTION, PRESENT_PARAMETERS, validate_components
from .sandbox import ToolRunner
from .tools import BUILTIN_SCHEMAS, WEB_TOOLS, clip, tool_spec
from .transcript import for_model, parse_arguments, replayable
from .web import webfetch, websearch

log = logging.getLogger("astralvessel")

Emit = Callable[[dict[str, Any]], Awaitable[None]]
OnMessage = Callable[[dict[str, Any]], Awaitable[None]]
EVENT_OUTPUT_CHARS = 4000
ENDING: set[asyncio.Future] = set()
STEP_LIMIT_NOTE = ("You have reached the step limit for this turn. Do not call any more tools. "
                   "Answer now with what you have found, and say what you could not check.")


def tool_specs(package: AgentPackage, allowed: set[str] | None = None) -> list[dict[str, Any]]:
    specs = []
    for name in package.manifest.tools.builtin:
        schema = BUILTIN_SCHEMAS[name]
        specs.append(tool_spec(name, schema["description"], schema["parameters"]))
    for tool in package.declared:
        specs.append(tool_spec(tool.name, tool.description, tool.parameters))
    if package.manifest.output.primitives:
        specs.append(tool_spec("present", PRESENT_DESCRIPTION, PRESENT_PARAMETERS))
    if allowed is not None:
        specs = [spec for spec in specs if spec["function"]["name"] in allowed]
    return specs


def system_prompt(package: AgentPackage, extra: tuple[str, ...] = ()) -> str:
    sandbox = package.manifest.sandbox
    mounts = "".join(f"\n- {m.target} ({'read-only' if m.read_only else 'writable'})" for m in sandbox.mounts)
    lines = [package.briefing(extra), "", "## Environment", f"Today is {date.today().isoformat()}.",
             f"Tool commands run in {sandbox.workdir}."]
    if mounts:
        lines.append("Mounted data:" + mounts)
    lines.append("Files a tool call writes under $TMPDIR are kept for later tool calls in this turn and "
                 "deleted when the turn ends.")
    lines.append("Text you write in the same message as a tool call is shown as brief narration, not as your "
                 "answer. Your final message, with no tool calls, is the answer: write it in full there.")
    if package.manifest.output.primitives:
        lines.append("If tables, metrics or charts would help, call `present` once before your final message. "
                     "Then write the complete answer in Markdown; never refer the reader to the visuals instead.")
    return "\n".join(lines)


class Turn:
    def __init__(self, package: AgentPackage, model: ModelClient, runner: ToolRunner, emit: Emit,
                 on_message: OnMessage | None = None, services: Any = None, owner: str = "",
                 prompt_append: tuple[str, ...] = (), tool_scopes: frozenset[str] | None = None):
        self.package = package
        self.prompt_append = prompt_append
        self.services = services
        self.owner = owner
        self.declared = {t.name: t for t in package.declared}
        self.on_message = on_message
        self._sent = 0
        self.model = model
        self.runner = runner
        self.workspace = secrets.token_hex(16)
        self.used_workspace = False
        self.emit = emit
        self.allowed = set(package.manifest.tools.builtin) | {t.name for t in package.declared}
        if package.manifest.output.primitives:
            self.allowed.add("present")
        if tool_scopes is not None:
            # The caller narrowed this turn to some permission scopes: the other tools do not exist for it.
            scopes = package.tool_scopes()
            self.allowed = {name for name in self.allowed if scopes.get(name) in tool_scopes}
        self.new_messages: list[dict[str, Any]] = []
        self.components: list[dict[str, Any]] = []
        self.files: list[dict[str, Any]] = []
        self.tokens = {"input": 0, "output": 0}
        self.steps = 0
        self.answer = ""

    async def run(self, history: list[dict[str, Any]], user_content: str | list[dict[str, Any]], *,
                  conversation_id: str, turn_id: str) -> dict[str, Any]:
        # user_content is the message text, or OpenAI content parts when the user attached images.
        started = time.monotonic()
        self.new_messages.append({"role": "user", "content": user_content})
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
        except Exception:  # noqa: BLE001  the turn still ends with turn.finished and is recorded
            log.warning("turn failed", exc_info=True)
            status, error = ev.STATUS_MODEL_ERROR, "the turn failed unexpectedly"
        if self.used_workspace:
            # Shielded: a second cancel must not skip turn.finished, and the sandbox still gets told.
            ending = asyncio.ensure_future(self._end_workspace())
            ENDING.add(ending)
            ending.add_done_callback(ENDING.discard)
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(ending)
        self._close_dangling_calls()
        if self.new_messages[-1]["role"] != "assistant":
            self.new_messages.append({"role": "assistant", "content": f"[this turn ended early: {status}]"})
        await self._flush()
        result = {
            "status": status,
            "text": self.answer,
            "components": self.components,
            "files": self.files,
            "tokens": dict(self.tokens),
            "steps": self.steps,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
        if error:
            result["error"] = error
        await self.emit(ev.event(ev.TURN_FINISHED, conversation_id=conversation_id, turn_id=turn_id, **result))
        return result

    async def _loop(self, history: list[dict[str, Any]]) -> str:
        specs = tool_specs(self.package, self.allowed)
        send_reasoning = self.package.manifest.model.send_reasoning
        system = {"role": "system", "content": system_prompt(self.package, self.prompt_append)}
        base = [system, *for_model(history, send_reasoning)]
        limit = self.package.manifest.model.max_steps
        while True:
            over_limit = self.steps >= limit
            messages = base + self.new_messages
            if over_limit:
                messages = messages + [{"role": "user", "content": STEP_LIMIT_NOTE}]
            step = await self._step(messages, [] if over_limit else specs)
            if not step.tool_calls or over_limit:
                self.answer = step.text.strip()
                final: dict[str, Any] = {"role": "assistant", "content": step.text}
                if step.reasoning:
                    final["reasoning_content"] = step.reasoning
                self.new_messages.append(final)
                return ev.STATUS_STEP_LIMIT if over_limit else ev.STATUS_OK
            assistant: dict[str, Any] = {
                "role": "assistant",
                "content": step.text or None,
                "tool_calls": [{"id": c.id, "type": "function",
                                "function": {"name": c.name, "arguments": replayable(c.arguments)}}
                               for c in step.tool_calls],
            }
            if step.reasoning:
                assistant["reasoning_content"] = step.reasoning
            self.new_messages.append(assistant)
            for call in step.tool_calls:
                output = await self._call_tool(call.id, call.name, call.arguments)
                self.new_messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
            await self._flush()

    async def _end_workspace(self) -> None:
        try:
            async with asyncio.timeout(15):
                await self.runner.end_turn(self.workspace)
        except Exception:  # noqa: BLE001  the sandbox sweeps a workspace it was never told to end
            log.warning("could not end the turn's workspace", exc_info=True)

    async def _flush(self) -> None:
        # Hand each completed message to the caller once, in order, so a dropped stream still leaves a usable prefix.
        if self.on_message is None:
            return
        while self._sent < len(self.new_messages):
            message = self.new_messages[self._sent]
            self._sent += 1
            try:
                await self.on_message(message)
            except Exception:  # noqa: BLE001  a failed delivery must not end the turn
                log.warning("transcript delivery failed", exc_info=True)

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
        arguments = parse_arguments(raw_arguments)
        await self.emit(ev.event(ev.TOOL_STARTED, call_id=call_id, name=name,
                                 arguments=arguments if arguments is not None else raw_arguments[:2000]))
        if name not in self.allowed:
            output, failed = f"tool {name} does not exist", True
        elif arguments is None:
            output, failed = "the arguments were not a valid JSON object; call the tool again with valid JSON", True
        else:
            try:
                output, failed = await self._dispatch(name, arguments)
                output = await self._attach_files(call_id, output)
            except BaseException:
                # A cancel or the deadline stopped the tool; still close the event pair before unwinding.
                with contextlib.suppress(Exception):
                    await self.emit(ev.event(ev.TOOL_FINISHED, call_id=call_id, name=name, output="[stopped]",
                                             error=True, truncated=False,
                                             elapsed_ms=int((time.monotonic() - started) * 1000)))
                raise
        limit = self.package.manifest.sandbox.tool_output_chars
        output, truncated = clip(output, limit)
        shown, _ = clip(output, EVENT_OUTPUT_CHARS)
        await self.emit(ev.event(ev.TOOL_FINISHED, call_id=call_id, name=name, output=shown, error=failed,
                                 truncated=truncated, elapsed_ms=int((time.monotonic() - started) * 1000)))
        return output

    async def _dispatch(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        try:
            return await self._run_tool(name, arguments)
        except Exception:  # noqa: BLE001  report a generic failure; raw exception text never reaches the model
            log.warning("tool %s failed", name, exc_info=True)
            return f"{name} failed", True

    async def _run_tool(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        if name == "present":
            try:
                self.components = validate_components(arguments.get("components"))
            except ValueError as exc:
                return f"not attached: {exc}. Fix the components and call present again.", True
            return f"attached {len(self.components)} components to the answer", False
        if name in WEB_TOOLS:
            return await (webfetch if name == "webfetch" else websearch)(arguments)
        tool = self.declared.get(name)
        if tool is not None and tool.service:
            if self.services is None:
                return f"{name}: its service is not configured", True
            timeout = tool.timeout_seconds or self.package.manifest.sandbox.tool_timeout_seconds
            return await self.services.call(tool, arguments, self.owner, timeout)
        if name in BUILTIN_TOOLS or name in {t.name for t in self.package.declared}:
            self.used_workspace = True
            return await self.runner.run(name, arguments, self.workspace)
        return f"tool {name} does not exist", True

    async def _attach_files(self, call_id: str, output: str) -> str:
        notes = []
        for item in self.runner.take_files(self.workspace):
            used = sum(f["bytes"] for f in self.files)
            if len(self.files) >= outputs.MAX_FILES_PER_TURN or used + len(item.data) > outputs.MAX_TURN_BYTES:
                notes.append(f"[not attached {item.name}: this turn has returned all the files it may]")
                continue
            self.files.append({"name": item.name, "media_type": item.media_type, "bytes": len(item.data)})
            await self.emit(ev.event(ev.FILE_ADDED, call_id=call_id, name=item.name, media_type=item.media_type,
                                     data=item.data))
            notes.append(f"[attached {item.name} ({item.media_type}, {outputs.size_text(len(item.data))}) "
                         "to the answer]")
        return "\n".join([output.rstrip(), *notes]).lstrip() if notes else output

    def _close_dangling_calls(self) -> None:
        answered = {m["tool_call_id"] for m in self.new_messages if m["role"] == "tool"}
        for message in list(self.new_messages):
            for call in message.get("tool_calls") or []:
                if call["id"] not in answered:
                    self.new_messages.append({"role": "tool", "tool_call_id": call["id"],
                                              "content": "[not run: the turn ended first]"})
                    answered.add(call["id"])
