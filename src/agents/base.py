from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class AgentRuntime:
    home: str = ""
    managed: bool = False
    label: str = "Runtime home"
    metadata: Mapping[str, Any] = field(default_factory=dict)


class BridgeIO(Protocol):
    def type(self, session: str, text: str) -> None: ...

    def keys(self, session: str, keys: Sequence[str]) -> None: ...


class AgentPlugin(ABC):
    name: str
    display_name: str
    runtime_label: str = "Runtime home"
    runtime_option_name: str = "--runtime-home"
    login_prompts: tuple[str, ...] = ()
    ready_streak_required: int = 2

    @abstractmethod
    def ensure_managed_runtime(
        self,
        session: str,
        *,
        cwd: Path,
        discord_channel_id: str | None,
    ) -> AgentRuntime:
        raise NotImplementedError

    @abstractmethod
    def build_launch_command(
        self,
        *,
        cwd: Path,
        runtime: AgentRuntime,
        session: str,
        discord_channel_id: str | None,
        approve_all: bool,
    ) -> str:
        raise NotImplementedError

    @abstractmethod
    def matches_process(self, pane_command: str, descendant_commands: Iterable[str]) -> bool:
        raise NotImplementedError

    @abstractmethod
    def capture_has_ready_surface(self, capture: str, cwd: Path) -> bool:
        raise NotImplementedError

    def extract_completion_summary(self, capture: str, prompt: str) -> str:
        _ = (capture, prompt)
        return ""

    def capture_has_completion_surface(self, capture: str, prompt: str) -> bool:
        return bool(self.extract_completion_summary(capture, prompt))

    def submit_prompt(self, session: str, prompt: str, *, bridge: BridgeIO) -> None:
        if prompt:
            bridge.type(session, prompt)
        bridge.keys(session, ["Enter"])

    def interrupt(self, session: str, *, bridge: BridgeIO) -> None:
        bridge.keys(session, ["C-c"])

    def cleanup_runtime(self, runtime: AgentRuntime) -> None:
        return None
