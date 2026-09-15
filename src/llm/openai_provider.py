"""
OpenAI adapter -- same contract as AnthropicProvider, via function calling
instead of tool_use. Kept deliberately symmetrical so swapping
LLM_PROVIDER=openai in config is the only change needed anywhere in the
system; see src/llm/base.py.
"""

from __future__ import annotations

import json
import os

from src.llm.base import ActionKind, LLMProvider, Observation, ProposedAction

_DECIDE_FUNCTION = {
    "name": "propose_action",
    "description": "Propose the single next action to take toward the goal.",
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": [k.value for k in ActionKind]},
            "target_ref": {"type": "string"},
            "text_value": {"type": "string"},
            "reasoning": {"type": "string"},
            "stuck_reason": {"type": "string"},
        },
        "required": ["kind", "reasoning"],
    },
}


class OpenAIProvider(LLMProvider):
    def __init__(self, model: str = "gpt-4.1") -> None:
        import openai  # local import so the package is only required if this provider is selected

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        self._client = openai.OpenAI(api_key=api_key)
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
        history_desc = "\n".join(
            f"{i + 1}. {a.kind.value} ref={a.target_ref} value={a.text_value} -- {a.reasoning}"
            for i, a in enumerate(history)
        ) or "(none yet)"

        prompt = (
            f"Goal: {goal}\n\n"
            f"Current page: {observation.title} ({observation.url})\n\n"
            f"Visible elements:\n{elements_desc}\n\n"
            f"Actions taken so far:\n{history_desc}\n\n"
            "Propose exactly one next action via propose_action. Use kind=done if the "
            "goal is already achieved. Use kind=stuck (with stuck_reason) if you cannot "
            "safely proceed -- never guess at an element that isn't listed."
        )

        response = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            tools=[{"type": "function", "function": _DECIDE_FUNCTION}],
            tool_choice={"type": "function", "function": {"name": "propose_action"}},
        )

        call = response.choices[0].message.tool_calls[0]
        data = json.loads(call.function.arguments)
        return ProposedAction(
            kind=ActionKind(data["kind"]),
            target_ref=data.get("target_ref"),
            text_value=data.get("text_value"),
            reasoning=data.get("reasoning", ""),
            stuck_reason=data.get("stuck_reason"),
        )
