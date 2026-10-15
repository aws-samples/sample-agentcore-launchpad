"""Abstract interface for pluggable coding-agent backends.

Ported verbatim from strands_studio_ui ``backend/codegen/backends/base.py``
(origin/main).
"""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class GenerationTask:
    """Input for a single generation (or repair) round."""

    flow_data: dict  # nodes + edges (raw input)
    graph_mode: bool = False
    template_code: str | None = None  # frontend template output (fallback + reference)
    previous_code: str | None = None  # current code during repair rounds
    validation_errors: list[str] = field(default_factory=list)  # errors during repair rounds
    mode: str = "generate"  # "generate" | "fix" (selects backend prompts)
    # Launchpad: the round's token usage (the agent's ``ResultMessage.usage``) is
    # reported here for the spend budget (services/usage_meter.py); None = unmetered
    on_usage: Callable[[dict[str, Any]], Any] | None = None


class GenerationError(Exception):
    """Raised when a backend fails to produce generated_agent.py."""


class CodingAgentBackend(ABC):
    """A headless coding agent that writes generated_agent.py inside a workspace.

    Instances are created per generation request so a backend may keep a
    conversation session alive across the initial round and repair rounds.
    """

    name: str = ""

    @abstractmethod
    async def check_available(self) -> tuple[bool, str]:
        """Return (available, reason). reason explains unavailability."""
        ...

    @abstractmethod
    async def generate(
        self,
        workspace: Path,
        task: GenerationTask,
        on_progress: Callable[[str], Awaitable[None]],
    ) -> None:
        """Produce generated_agent.py inside workspace.

        Called once for the initial round and again for each repair round
        (with task.previous_code / task.validation_errors populated).
        Progress messages are reported through on_progress.
        Raises GenerationError if generated_agent.py is missing afterwards.
        """
        ...

    async def close(self) -> None:
        """Release any session resources. Default: no-op."""
        return None
