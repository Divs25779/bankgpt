"""
Provider-agnostic interface for the discovery agent's LLM calls.

Why an interface at all: the discovery loop's *decision* step (observe ->
decide) is the only place a specific vendor's API shape leaks into this
system. Everything downstream -- the artifact schema, the replay engine,
the safety layer -- must not care which model produced the discovery run.
That's not just tidiness: it directly answers "LLM provider is explicitly
your call" from a position of not having locked yourself in, and it means
a provider outage or pricing change never touches the production replay
path anyway (replay never calls an LLM at all).

The interface intentionally does NOT expose vendor-specific concepts
(e.g. Anthropic's tool_use blocks vs OpenAI's function_call format). It
exposes exactly what the agent loop needs: given an observation and a
goal, return one structured Action.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ActionKind(str, Enum):
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    NAVIGATE = "navigate"
    READ_TEXT = "read_text"
    WAIT = "wait"
    DONE = "done"          # model believes the goal is achieved
    STUCK = "stuck"         # model cannot proceed safely -- triggers escalation


class ProposedAction(BaseModel):
    """
    One decision from the model, for one turn of observe -> decide -> act.

    `target_ref` is an opaque id into the current observation's element
    list (see Observation below) -- the model never invents a selector
    from scratch; it points at something it was actually shown. This is
    what makes the discovery transcript compilable into stable Locators
    later (src/artifact/compiler.py), instead of hoping the model's
    freeform description of "the button" can be turned into one.
    """

    kind: ActionKind
    target_ref: Optional[str] = None
    text_value: Optional[str] = None
    reasoning: str = Field(description="Why the model chose this action -- persisted "
                            "to evidence, never to the artifact itself.")
    stuck_reason: Optional[str] = None  # populated when kind == STUCK


class ObservedElement(BaseModel):
    ref: str                 # stable per-observation id, e.g. "e3"
    role: str
    name: str
    value: Optional[str] = None


class Observation(BaseModel):
    """
    What the agent shows the model each turn. Built from Playwright's
    accessibility snapshot (see src/agent/perception.py) -- text, not
    pixels, by default. This is the concrete implementation of "bias
    toward an approach that still works with no clean DOM": the
    accessibility tree is populated by the browser/OS even for
    server-rendered legacy markup with no test IDs, because it's derived
    from semantics (role, label) rather than markup structure.
    """

    url: str
    title: str
    elements: list[ObservedElement]
    labels: list[str] = Field(default_factory=list)
    screenshot_path: Optional[str] = None  # only attached on request / on failure


class DialogEvent(BaseModel):
    """
    Records a native browser dialog (confirm/alert/prompt/beforeunload)
    that fired as a synchronous side effect of executing an action
    during discovery, and how the loop handled it.

    This is NOT optional bookkeeping -- if the loop auto-handles dialogs
    with a blanket handler and never records that one fired, the
    compiler has no way of knowing the resulting artifact needs a
    Step.expects_dialog set, and replay (which does NOT use a blanket
    handler -- see src/artifact/schema.py's DialogPolicy docstring) will
    hang or fail the moment it hits the same dialog for real. Every
    DiscoveryTurn (src/artifact/compiler.py) whose action triggered a
    dialog must carry one of these.
    """

    message: str
    dialog_type: str   # "alert" | "confirm" | "beforeunload" | "prompt"
    policy: str          # "accept" | "dismiss" -- how discovery handled it


class LLMProvider(ABC):
    """Implement this once per vendor. The agent loop only ever talks to this interface."""

    @abstractmethod
    def decide_next_action(
        self,
        goal: str,
        observation: Observation,
        history: list[ProposedAction],
    ) -> ProposedAction:
        """Given the goal, current observation, and prior actions this run, return the next action."""
        raise NotImplementedError


def get_provider(name: str) -> LLMProvider:
    """Factory so the agent entrypoint can select a provider via config/env, e.g. LLM_PROVIDER=anthropic."""
    if name == "anthropic":
        from src.llm.anthropic_provider import AnthropicProvider
        return AnthropicProvider()
    if name == "openai":
        from src.llm.openai_provider import OpenAIProvider
        return OpenAIProvider()
    raise ValueError(f"Unknown LLM provider: {name}")
