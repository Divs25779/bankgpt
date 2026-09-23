"""
Anthropic adapter. Uses tool-calling (not the vision-based "computer use"
beta) because my observation is already structured text (the
accessibility tree) -- there is nothing for a vision model to add here,
and forcing one structured tool call per turn makes ProposedAction
parsing trivial and cheap. See src/llm/base.py for why the interface
looks the way it does.
"""

from __future__ import annotations

import os

from src.llm.base import ActionKind, LLMProvider, Observation, ProposedAction

_DECIDE_TOOL = {
    "name": "propose_action",
    "description": "Propose the single next action to take toward the goal, "
    "given the current observed elements.",
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": [k.value for k in ActionKind],
            },
            "target_ref": {
                "type": "string",
                "description": "The 'ref' of the element to act on, copied exactly "
                "from the observation's visible elements. Omit for navigate/wait/done/stuck, "
                "and omit for read_text when reading a labeled data field (use text_value instead).",
            },
            "text_value": {
                "type": "string",
                "description": "Text to type, URL to navigate to, option to select, OR -- for "
                "kind=read_text on a labeled data field with no target_ref -- the exact label "
                "string copied from the observation's readable data fields (e.g. 'Savings Balance:'). "
                "Required for read_text when no target_ref applies; omit for click/done/stuck.",
            },
            "reasoning": {"type": "string"},
            "stuck_reason": {
                "type": "string",
                "description": "Required and only used when kind == stuck.",
            },
        },
        "required": ["kind", "reasoning"],
    },
}


class AnthropicProvider(LLMProvider):
    def __init__(self, model: str = "claude-sonnet-4-6") -> None:
        import anthropic  # local import so the package is only required if this provider is selected

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def decide_next_action(
        self,
        goal: str,
        observation: Observation,
        history: list[ProposedAction],
    ) -> ProposedAction:
        elements_desc = "\n".join(
            f'- ref={e.ref} role={e.role} name="{e.name}"' + (f' value="{e.value}"' if e.value else "")
            for e in observation.elements
        )
        labels_desc = (
            ", ".join(observation.labels) if observation.labels else "(none)"
        )
        history_desc = "\n".join(
            f"{i + 1}. {a.kind.value} ref={a.target_ref} value={a.text_value} -- {a.reasoning}"
            for i, a in enumerate(history)
        ) or "(none yet)"

        prompt = (
            f"Goal: {goal}\n\n"
            f"Current page: {observation.title} ({observation.url})\n\n"
            f"Visible interactive elements (click/type/select via target_ref):\n{elements_desc}\n\n"
            f"Readable data fields on this page (labels only, not their current values -- "
            f"to read one, use kind=read_text with text_value set to the exact label string "
            f"below, and no target_ref):\n{labels_desc}\n\n"
            f"Actions taken so far:\n{history_desc}\n\n"
            "Propose exactly one next action using the propose_action tool. "
            "If the goal is already achieved by the current page state, use kind=done. "
            "If you cannot safely proceed (e.g. an unexpected state, missing element, "
            "or an action that looks irreversible and wasn't part of the goal), use "
            "kind=stuck and explain why in stuck_reason -- do not guess."
        )

        response = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            tools=[_DECIDE_TOOL],
            tool_choice={"type": "tool", "name": "propose_action"},
            messages=[{"role": "user", "content": prompt}],
        )

        tool_use = next(b for b in response.content if b.type == "tool_use")
        data = tool_use.input
        return ProposedAction(
            kind=ActionKind(data["kind"]),
            target_ref=data.get("target_ref"),
            text_value=data.get("text_value"),
            reasoning=data.get("reasoning", ""),
            stuck_reason=data.get("stuck_reason"),
        )
