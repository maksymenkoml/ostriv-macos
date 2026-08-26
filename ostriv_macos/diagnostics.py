import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Optional, Sequence


class PatchError(Exception):
    def __init__(self, code: str, player_message: str, detail: str = ""):
        super().__init__(detail or player_message)
        self.code = code
        self.player_message = player_message
        self.detail = detail


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def decode_output(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


class CommandRunner:
    def run(
        self,
        argv: Sequence[str],
        timeout: Optional[float] = None,
    ) -> CommandResult:
        result = subprocess.run(
            list(argv),
            capture_output=True,
            check=False,
            timeout=timeout,
        )
        return CommandResult(
            result.returncode,
            decode_output(result.stdout),
            decode_output(result.stderr),
        )


def configure_logger(path: Path) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("ostriv_macos")
    for old_handler in logger.handlers[:]:
        logger.removeHandler(old_handler)
        old_handler.close()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = logging.FileHandler(
        str(path), encoding="utf-8", errors="backslashreplace"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


class PlayerOutput:
    def __init__(self, stream: IO[str] = sys.stdout, color: Optional[bool] = None):
        self.stream = stream
        self.color = stream.isatty() if color is None else color
        self._title_printed = False
        self._stages = set()

    def _line(self, text: str = "") -> None:
        print(text, file=self.stream)

    def title(self) -> None:
        if not self._title_printed:
            self._line("Ostriv for macOS")
            self._title_printed = True

    def stage(self, label: str, status: str, detail: str = "") -> None:
        if label in self._stages:
            return
        self._stages.add(label)
        suffix = " · " + detail if detail else ""
        self._line("{}: {}{}".format(label, status, suffix))
