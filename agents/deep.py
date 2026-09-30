"""Deep agents: a plan, a scratchpad and ephemeral sub-agents, per run.

Mirrors the LangChain ``deepagents`` pattern on top of the specialists this
app already builds. An agent whose ``deep_json`` says ``enabled`` gets three
tool groups, each switchable on its own:

- **planning** -- ``write_todos`` / ``read_todos`` keep a short task list the
  model is told to write before starting and update as it goes.
- **scratchpad** -- ``ls`` / ``read_file`` / ``write_file`` / ``edit_file``
  over an in-memory file tree that lives only for the run, so long tool
  outputs and drafts stay out of the context window.
- **sub-agents** -- ``task`` runs an ephemeral pydantic-ai ``Agent`` with the
  parent's toolsets (its MCP servers and built-ins) and a fresh context, and
  returns its final text. Sub-agents are created on the fly, are never
  registered anywhere, and are not reachable from the orchestrator or as
  peers.

State is one :class:`DeepState` per *run*, keyed by ``RunContext.run_id`` and
kept in a module-level table with a TTL, so it works from chat, A2A, scheduled
runs and workflows alike without the runners knowing about it. A sub-agent
receives the parent's state as its ``deps``, so parent and children share one
plan and one scratchpad; a peer specialist reached through delegation does not
(its run has its own ``run_id`` and ``deps=None``), which keeps every agent's
scratchpad its own.

Sub-agent concurrency is bounded per state *and per depth*: children hold a
slot on the depth-1 semaphore while their own grandchildren queue on the
depth-2 one, so a full set of children spawning grandchildren cannot deadlock
against itself on a single semaphore.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.toolsets import FunctionToolset

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MAX_SUBAGENTS_LIMIT = 20
MAX_DEPTH_LIMIT = 3


class DeepConfig(BaseModel):
    """The parsed shape of ``AgentConfig.deep_json``.

    ``extra="forbid"`` so a typo in a hand-edited bundle (``max_subagent``)
    is a 422 at import, not a silently ignored knob.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    planning: bool = True
    scratchpad: bool = True
    subagents: bool = True
    max_subagents: int = Field(default=5, ge=1, le=MAX_SUBAGENTS_LIMIT)
    subagent_max_depth: int = Field(default=1, ge=1, le=MAX_DEPTH_LIMIT)
    subagent_instructions: str = Field(default="", max_length=20_000)


def parse_deep_config(raw: str | None, *, agent_name: str = "?") -> DeepConfig:
    """``DeepConfig`` from the stored column; defaults (disabled) when the
    column is null or malformed, so one bad row never fails a build."""
    if not raw:
        return DeepConfig()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("deep_json is not an object")
        return DeepConfig.model_validate(data)
    except Exception:  # noqa: BLE001 - malformed storage degrades, like peers_json
        logger.warning(
            "Malformed deep_json on agent %s; deep-agent tools stay off", agent_name
        )
        return DeepConfig()


def dump_deep_config(config: DeepConfig | None) -> str | None:
    """JSON for the ``deep_json`` column. ``None`` (clear the column) when the
    config is the defaults, so untouched agents keep a null column."""
    if config is None or config == DeepConfig():
        return None
    return json.dumps(config.model_dump(), sort_keys=True)


# ---------------------------------------------------------------------------
# Per-run state
# ---------------------------------------------------------------------------
MAX_FILES = 200
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_TODOS = 100
MAX_PATH_LENGTH = 200
# A read shows at most this many characters of one line, like `deepagents`.
MAX_LINE_CHARS = 2000
SUBAGENT_OUTPUT_LIMIT = 20_000
STATE_TTL_SECONDS = float(3600)
_SWEEP_INTERVAL_SECONDS = 60.0

TodoStatus = Literal["pending", "in_progress", "completed"]


class TodoItem(BaseModel):
    content: str = Field(min_length=1, max_length=1000)
    status: TodoStatus = "pending"


class ScratchpadError(ValueError):
    """A scratchpad operation the model can correct (bad path, cap hit,
    ambiguous edit). Returned to the model as text, never raised out of a
    tool: a ``ModelRetry`` counts against the retry budget, and exhausting it
    kills the whole run over a typo in a file name."""


@dataclass
class DeepState:
    """Everything one run's deep tools share: the plan, the scratchpad, and
    the sub-agent semaphores. Sub-agents get the parent's instance as deps."""

    run_id: str
    todos: list[TodoItem] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
    sizes: dict[str, int] = field(default_factory=dict)
    last_used: float = field(default_factory=time.monotonic)
    # depth -> semaphore bounding how many sub-agents run at once at that depth
    semaphores: dict[int, asyncio.Semaphore] = field(default_factory=dict)
    subagent_count: int = 0

    @property
    def total_bytes(self) -> int:
        return sum(self.sizes.values())

    def put(self, path: str, content: str) -> int:
        """Store ``content`` at ``path``, enforcing the caps. Returns the byte
        size written. Raises :class:`ScratchpadError` when a cap is hit."""
        size = len(content.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise ScratchpadError(
                f"{path!r} would be {size:,} bytes; a scratchpad file may hold at "
                f"most {MAX_FILE_BYTES:,} bytes. Split it across several files."
            )
        if path not in self.files and len(self.files) >= MAX_FILES:
            raise ScratchpadError(
                f"the scratchpad already holds {MAX_FILES} files; overwrite or "
                "reuse one instead of adding another."
            )
        new_total = self.total_bytes - self.sizes.get(path, 0) + size
        if new_total > MAX_TOTAL_BYTES:
            raise ScratchpadError(
                f"writing {path!r} would bring the scratchpad to {new_total:,} "
                f"bytes; the limit is {MAX_TOTAL_BYTES:,}. Overwrite a file you "
                "no longer need."
            )
        self.files[path] = content
        self.sizes[path] = size
        return size


_states: dict[str, DeepState] = {}
_last_sweep = 0.0


def _sweep(now: float) -> None:
    """Drop states nobody touched for ``STATE_TTL_SECONDS``. Cheap: runs at
    most once a minute, from whichever tool call happens to come along."""
    global _last_sweep
    if now - _last_sweep < _SWEEP_INTERVAL_SECONDS:
        return
    _last_sweep = now
    stale = [k for k, s in _states.items() if now - s.last_used > STATE_TTL_SECONDS]
    for k in stale:
        _states.pop(k, None)
    if stale:
        logger.debug("[deep] swept %d stale run state(s)", len(stale))


def state_for(ctx: RunContext[Any]) -> DeepState:
    """The :class:`DeepState` for this tool call.

    A sub-agent carries its parent's state as ``deps`` and so shares the
    parent's plan and scratchpad; anything else (a specialist run from chat,
    A2A, a scheduled run or a workflow step) is keyed by its own ``run_id``.
    """
    now = time.monotonic()
    _sweep(now)
    deps = getattr(ctx, "deps", None)
    if isinstance(deps, DeepState):
        deps.last_used = now
        return deps
    run_id = str(getattr(ctx, "run_id", None) or "no-run-id")
    state = _states.get(run_id)
    if state is None:
        state = DeepState(run_id=run_id)
        _states[run_id] = state
    state.last_used = now
    return state


def forget_state(run_id: str) -> None:
    """Drop a run's state early (tests; runners may call it on completion)."""
    _states.pop(run_id, None)


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------
_STATUS_MARK = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}


def render_todos(todos: Sequence[TodoItem]) -> str:
    if not todos:
        return "No todos yet."
    lines = [
        f"{i}. {_STATUS_MARK[t.status]} {t.content}" for i, t in enumerate(todos, 1)
    ]
    done = sum(1 for t in todos if t.status == "completed")
    lines.append(f"({done}/{len(todos)} completed)")
    return "\n".join(lines)


def _clean_path(path: str) -> str:
    p = (path or "").strip()
    if not p:
        raise ScratchpadError("a file path is required.")
    if len(p) > MAX_PATH_LENGTH:
        raise ScratchpadError(f"file paths may be at most {MAX_PATH_LENGTH} characters.")
    if any(ch in p for ch in ("\x00", "\n", "\r")):
        raise ScratchpadError("a file path may not contain newlines or NUL bytes.")
    return p


def _number_lines(content: str, offset: int, limit: int) -> str:
    """``cat -n`` style, 1-based numbers, ``offset`` lines skipped."""
    if not content:
        return "(empty file)"
    lines = content.splitlines()
    offset = max(0, offset)
    limit = max(1, limit)
    if offset >= len(lines):
        raise ScratchpadError(
            f"offset {offset} is past the end of the file ({len(lines)} lines)."
        )
    chunk = lines[offset : offset + limit]
    out = []
    for i, line in enumerate(chunk, offset + 1):
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS] + "…"
        out.append(f"{i:6d}\t{line}")
    remaining = len(lines) - (offset + len(chunk))
    if remaining > 0:
        out.append(f"… {remaining} more line(s); read again with offset={offset + len(chunk)}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Instructions
# ---------------------------------------------------------------------------
DEFAULT_SUBAGENT_INSTRUCTIONS = (
    "You are a sub-agent working on one delegated task for a specialist agent. "
    "You have the same tools as your parent. Complete the task described in the "
    "user message completely and independently, then return a concise result "
    "that the parent can use directly: the facts found, the decisions taken and "
    "anything still missing. Do not ask questions back; make reasonable "
    "assumptions and state them."
)


def _has_task_tool(config: DeepConfig, depth: int) -> bool:
    return config.subagents and depth < config.subagent_max_depth


def deep_instructions(config: DeepConfig, *, depth: int = 0) -> str:
    """System-prompt section describing the deep tools this agent has.

    Returns ``""`` when nothing is enabled at this depth, so callers can
    append it unconditionally.
    """
    parts: list[str] = []
    if config.planning:
        parts.append(
            "- **Plan first.** Before any non-trivial work, call `write_todos` "
            "with the steps you intend to take. Mark a step `in_progress` when "
            "you start it and `completed` when it is done by calling "
            "`write_todos` again with the full, updated list (it replaces the "
            "previous one). `read_todos` shows the current plan. Keep the plan "
            "short and concrete; revise it when you learn something new."
        )
    if config.scratchpad:
        parts.append(
            "- **Use the scratchpad for long intermediate results.** `ls`, "
            "`read_file`, `write_file` and `edit_file` work on a private, "
            "in-memory file tree that exists only for this run. Put large tool "
            "outputs, drafts and notes there and read back only the parts you "
            "need, so your context stays small. Nothing in it is shown to the "
            "user or kept after the run: anything the user must see belongs in "
            "your final answer."
        )
    if _has_task_tool(config, depth):
        parts.append(
            "- **Delegate isolated sub-tasks with `task`.** It runs an ephemeral "
            "sub-agent that has your tools but none of your conversation, and "
            "returns its final text. Use it for self-contained pieces of work, "
            "especially ones that produce a lot of output or that can run side "
            "by side (call `task` several times in one turn to parallelise). "
            "Put everything the sub-agent needs into `description`. It shares "
            f"your scratchpad, so for big results ask it to write the full "
            "output to a file and return a short summary with the path. At most "
            f"{config.max_subagents} sub-agents run at once."
        )
    if not parts:
        return ""
    return (
        "\n\n## Working method (deep agent)\n"
        "You work like a deep agent: plan, keep bulky intermediate results out "
        "of your context, and split off isolated work.\n" + "\n".join(parts)
    )


# ---------------------------------------------------------------------------
# Toolset
# ---------------------------------------------------------------------------
ProgressHandlerFactory = Callable[[str], Any]


def deep_toolset(
    config: DeepConfig,
    *,
    parent_toolsets: Sequence[Any],
    model: Any,
    agent_name: str,
    depth: int = 0,
    retries: int = 1,
    progress_handler_factory: ProgressHandlerFactory | None = None,
) -> FunctionToolset:
    """The deep tools for one agent at one nesting ``depth``.

    ``parent_toolsets`` are the agent's own toolsets (MCP servers, built-ins);
    every sub-agent gets them too, plus a deep toolset one level deeper. The
    ``task`` tool is omitted once ``depth`` reaches ``subagent_max_depth``, so
    the recursion is bounded by construction rather than by a runtime check.
    ``progress_handler_factory(label)`` may return a pydantic-ai
    ``event_stream_handler`` so a sub-agent's tool calls show up in the chat
    like a specialist's do.
    """
    toolset: FunctionToolset = FunctionToolset()

    # -- planning -----------------------------------------------------------
    if config.planning:

        @toolset.tool
        async def write_todos(ctx: RunContext[Any], todos: list[TodoItem]) -> str:
            """Replace your plan with this list of todos and return it.

            Send the complete list every time (it replaces the previous one),
            with each item's current status: `pending`, `in_progress` or
            `completed`.

            Args:
                todos: The full, ordered plan.
            """
            if len(todos) > MAX_TODOS:
                return f"Error: at most {MAX_TODOS} todos; merge steps."
            state = state_for(ctx)
            state.todos = list(todos)
            logger.info(
                "[deep] %s plan: %d todo(s), %d in progress, %d completed",
                agent_name, len(state.todos),
                sum(1 for t in state.todos if t.status == "in_progress"),
                sum(1 for t in state.todos if t.status == "completed"),
            )
            return render_todos(state.todos)

        @toolset.tool
        async def read_todos(ctx: RunContext[Any]) -> str:
            """Show the current plan (the todos and their statuses)."""
            return render_todos(state_for(ctx).todos)

    # -- scratchpad ---------------------------------------------------------
    if config.scratchpad:

        @toolset.tool
        async def ls(ctx: RunContext[Any]) -> list[str]:
            """List the files in the scratchpad (paths, sorted)."""
            return sorted(state_for(ctx).files)

        @toolset.tool
        async def read_file(
            ctx: RunContext[Any], path: str, offset: int = 0, limit: int = 2000
        ) -> str:
            """Read a scratchpad file, line-numbered like `cat -n`.

            Args:
                path: The file to read (as listed by `ls`).
                offset: Lines to skip from the top (0-based).
                limit: Maximum number of lines to return.
            """
            state = state_for(ctx)
            try:
                p = _clean_path(path)
                content = state.files.get(p)
                if content is None:
                    raise ScratchpadError(
                        f"no file at {p!r}. Call `ls` to see what exists."
                    )
                return _number_lines(content, offset, limit)
            except ScratchpadError as e:
                return f"Error: {e}"

        @toolset.tool
        async def write_file(ctx: RunContext[Any], path: str, content: str) -> str:
            """Create or overwrite a scratchpad file.

            Args:
                path: Where to store it, e.g. `notes/findings.md`.
                content: The full text of the file.
            """
            state = state_for(ctx)
            try:
                p = _clean_path(path)
                size = state.put(p, content)
            except ScratchpadError as e:
                return f"Error: {e}"
            return (
                f"Wrote {size:,} bytes to {p} "
                f"({len(state.files)} file(s), {state.total_bytes:,} bytes in use)."
            )

        @toolset.tool
        async def edit_file(
            ctx: RunContext[Any],
            path: str,
            old_string: str,
            new_string: str,
            replace_all: bool = False,
        ) -> str:
            """Replace text inside a scratchpad file.

            `old_string` must occur exactly once unless `replace_all` is set;
            include enough surrounding text to make it unique.

            Args:
                path: The file to edit.
                old_string: The exact text to find.
                new_string: What to put in its place.
                replace_all: Replace every occurrence instead of exactly one.
            """
            state = state_for(ctx)
            try:
                p = _clean_path(path)
                content = state.files.get(p)
                if content is None:
                    raise ScratchpadError(
                        f"no file at {p!r}. Call `ls` to see what exists."
                    )
                if not old_string:
                    raise ScratchpadError("old_string must not be empty.")
                count = content.count(old_string)
                if count == 0:
                    raise ScratchpadError(
                        f"old_string was not found in {p!r}. Read the file and "
                        "copy the text exactly."
                    )
                if count > 1 and not replace_all:
                    raise ScratchpadError(
                        f"old_string occurs {count} times in {p!r}. Include more "
                        "surrounding text to make it unique, or set "
                        "replace_all=true."
                    )
                updated = (
                    content.replace(old_string, new_string)
                    if replace_all
                    else content.replace(old_string, new_string, 1)
                )
                state.put(p, updated)
            except ScratchpadError as e:
                return f"Error: {e}"
            replaced = count if replace_all else 1
            return f"Replaced {replaced} occurrence(s) in {p}."

    # -- sub-agents ---------------------------------------------------------
    if _has_task_tool(config, depth):
        child_depth = depth + 1
        base_instructions = (
            config.subagent_instructions.strip() or DEFAULT_SUBAGENT_INSTRUCTIONS
        )

        @toolset.tool
        async def task(
            ctx: RunContext[Any], description: str, instructions: str = ""
        ) -> str:
            """Run an isolated sub-task in an ephemeral sub-agent and return its
            final text.

            The sub-agent has your tools and shares your scratchpad, but sees
            none of your conversation: put everything it needs into
            `description`. Call `task` several times in one turn to run
            sub-tasks in parallel.

            Args:
                description: The complete, self-contained task to carry out,
                    including what to return.
                instructions: Optional extra guidance for this sub-agent (tone,
                    constraints, which tools to prefer).
            """
            state = state_for(ctx)
            state.subagent_count += 1
            n = state.subagent_count
            label = f"{agent_name} › sub-agent #{n}"

            system_text = base_instructions
            extra = (instructions or "").strip()
            if extra:
                system_text += "\n\n" + extra
            system_text += deep_instructions(config, depth=child_depth)

            child = Agent(
                instructions=system_text,
                deps_type=DeepState,
                retries=retries,
                name=label,
            )
            child_toolset = deep_toolset(
                config,
                parent_toolsets=parent_toolsets,
                model=model,
                agent_name=agent_name,
                depth=child_depth,
                retries=retries,
                progress_handler_factory=progress_handler_factory,
            )
            handler = (
                progress_handler_factory(label) if progress_handler_factory else None
            )
            semaphore = state.semaphores.setdefault(
                child_depth, asyncio.Semaphore(config.max_subagents)
            )
            logger.info(
                "[deep] %s START (depth %d) | %.160s",
                label, child_depth, description.replace("\n", " "),
            )
            async with semaphore:
                try:
                    result = await child.run(
                        description,
                        model=model,
                        deps=state,
                        usage=ctx.usage,
                        toolsets=[*parent_toolsets, child_toolset],
                        event_stream_handler=handler,
                    )
                except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                    raise
                except BaseException as e:  # noqa: BLE001
                    # A sign-in demand from an MCP server must reach the
                    # delegation tool, which owns the sign-in flow.
                    from agents.oauth2 import find_oauth_required

                    if find_oauth_required(e) is not None:
                        raise
                    logger.exception("[deep] %s failed", label)
                    return f"Sub-agent #{n} failed: {type(e).__name__}: {e}"

            out = "" if result.output is None else str(result.output)
            logger.info("[deep] %s DONE | %d chars", label, len(out))
            if len(out) > SUBAGENT_OUTPUT_LIMIT:
                out = out[:SUBAGENT_OUTPUT_LIMIT] + (
                    f"\n\n[Sub-agent output truncated at {SUBAGENT_OUTPUT_LIMIT:,} "
                    "characters. Ask for a narrower task, or have the sub-agent "
                    "write the full result to the scratchpad and return a summary.]"
                )
            if not out.strip():
                return f"Sub-agent #{n} completed but returned no text."
            return out

    return toolset
