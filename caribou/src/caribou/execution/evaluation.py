"""
Shared logic behind the "/evaluate" REPL command and its web-UI equivalent
(`POST /api/sessions/{id}/evaluate`): resolving which evaluator agent to use
for a run, and running the bounded LLM call against its transcript.

Kept separate from execution/user_commands.py (CLI-specific: console output,
TODOs, report-file conventions) and server/session_manager.py (server-specific:
session state, HTTP-facing persistence) so both share one implementation of
the actual evaluation logic instead of drifting apart.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from caribou.agents.AgentSystem import Agent, AgentSystem
from caribou.config import DEFAULT_AGENT_DIR
from caribou.execution.token_utils import estimate_messages_tokens, estimate_tokens

PACKAGE_AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents"
EVALUATOR_BLUEPRINT_NAME = "evaluator_agent"

# Prototype-scoped safety guard: the evaluation payload is the full run
# history, not a summarized slice, so it can exceed a real model's context
# window on a long run. This bound is a heuristic (see token_utils' own
# docstring) applied before sending, not tied to any specific model's real
# limit. Exceeding it aborts the call rather than truncating silently.
EVALUATE_MAX_CONTEXT_TOKENS = 100_000


class EvaluationContextTooLarge(ValueError):
    def __init__(self, estimated_tokens: int, limit: int):
        self.estimated_tokens = estimated_tokens
        self.limit = limit
        super().__init__(
            f"Run context is too large to evaluate: ~{estimated_tokens:,} "
            f"estimated tokens exceeds the {limit:,}-token prototype limit."
        )


def resolve_evaluator_agent(agent_system: AgentSystem) -> Tuple[Agent, str]:
    """Pick the evaluator agent to use, in order:
    1. the agent named by this system's own 'evaluator_agent' blueprint field
    2. (legacy) an agent literally named 'evaluator' in the running blueprint
    3. a user-defined evaluator_agent.json override (CARIBOU_HOME/agent_systems)
    4. the package-shipped default evaluator_agent.json

    Returns (agent, human-readable description of where it came from).
    """
    declared_evaluator = agent_system.get_evaluator_agent()
    if declared_evaluator is not None:
        return (
            declared_evaluator,
            f"the '{declared_evaluator.name}' agent declared as this system's "
            "evaluator_agent",
        )

    live_evaluator = agent_system.get_agent("evaluator")
    if live_evaluator is not None:
        return (
            live_evaluator,
            "the 'evaluator' agent defined in this run's agent system",
        )

    for search_dir, label in (
        (DEFAULT_AGENT_DIR, "user"),
        (PACKAGE_AGENTS_DIR, "package default"),
    ):
        blueprint_path = Path(search_dir) / f"{EVALUATOR_BLUEPRINT_NAME}.json"
        if blueprint_path.exists():
            evaluator_system = AgentSystem.load_from_json(str(blueprint_path))
            evaluator_agent = evaluator_system.get_agent("evaluator")
            if evaluator_agent is None:
                raise ValueError(
                    f"{blueprint_path} does not define an agent named 'evaluator'"
                )
            return evaluator_agent, f"{label} evaluator blueprint: {blueprint_path}"

    raise FileNotFoundError(
        "No evaluator agent found: set 'evaluator_agent' (or define an agent named "
        "'evaluator') in the running blueprint, or add "
        f"{EVALUATOR_BLUEPRINT_NAME}.json to {DEFAULT_AGENT_DIR} (the package "
        f"default at {PACKAGE_AGENTS_DIR} is missing too, which shouldn't happen "
        "in a normal install)."
    )


def build_evaluation_payload(
    *,
    run_id: str,
    turn: int,
    active_agent: str,
    history: List[Dict[str, str]],
    todos: Optional[List[Dict[str, object]]] = None,
) -> Dict[str, object]:
    return {
        "run_id": run_id,
        "turns_completed": turn,
        "active_agent": active_agent,
        "todos": todos or [],
        "history": history,
    }


def estimate_payload_tokens(system_prompt: str, payload: Dict[str, object]) -> int:
    # Estimate on raw message/todo content, not a JSON-serialized blob —
    # escaping quotes/newlines would inflate the count past what the model
    # actually has to read.
    todos_tokens = sum(
        estimate_tokens(str(t.get("text", ""))) for t in payload.get("todos", [])
    )
    return (
        estimate_tokens(system_prompt)
        + estimate_messages_tokens(payload["history"])
        + todos_tokens
    )


def run_evaluation(
    *,
    evaluator_agent: Agent,
    llm_client: object,
    model_name: str,
    payload: Dict[str, object],
    max_context_tokens: int = EVALUATE_MAX_CONTEXT_TOKENS,
) -> str:
    """Call the evaluator LLM with the given payload. Raises
    EvaluationContextTooLarge if the estimated size exceeds the bound —
    callers must not truncate or retry with a smaller payload silently, and
    must let any provider error from the LLM call itself propagate."""
    system_prompt = evaluator_agent.get_full_prompt(None)
    estimated_tokens = estimate_payload_tokens(system_prompt, payload)
    if estimated_tokens > max_context_tokens:
        raise EvaluationContextTooLarge(estimated_tokens, max_context_tokens)

    response = llm_client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload)},
        ],
        temperature=0.2,
    )
    return response.choices[0].message.content
