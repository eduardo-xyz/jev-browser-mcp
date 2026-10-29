"""Target protocol: the surface (Mac Chrome, Android) a Runner drives."""
from pathlib import Path
from typing import Protocol


class StalePage(Exception):
    """The page changed between observe and act."""


class TargetBlocked(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class DeviceDisconnected(Exception):
    """The device went away; the session is kept so it can reconnect."""


class Target(Protocol):
    kind: str  # "chrome" | "android"

    def observe(self) -> dict: ...

    def fresh(self, page: dict) -> bool: ...

    def act(self, action: dict, page: dict, text: str | None = None, verify: bool = False) -> None:
        """Perform `action` on the screen.

        StalePage may only be raised before any input is sent to the screen (verify check).
        Any exception after input may have been sent is treated by the runner as an
        uncertain action.
        """
        ...

    def screenshot(self, path: Path) -> Path: ...

    def close(self) -> None: ...
