import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Optional, Sequence


ALLOWED_EXTERNAL_EXECUTABLES = frozenset(
    {"cxbottle", "cxmenu", "lsregister", "mdfind", "wine"}
)
_SENSITIVE_OPTIONS = frozenset(
    {"--api-key", "--password", "--secret", "--token", "/d"}
)
_DIAGNOSTIC_LIMIT = 2048


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
    diagnostic: str = ""


def decode_output(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _validate_external_argv(argv: Sequence[str], allowed=ALLOWED_EXTERNAL_EXECUTABLES):
    command = list(argv)
    if not command or not isinstance(command[0], str):
        raise ValueError("external command is empty or invalid")
    executable = Path(command[0]).name
    if executable not in allowed:
        raise ValueError("external executable is not allowed: {}".format(executable))
    return command


def _safe_argv(argv: Sequence[str]) -> str:
    redacted = []
    hide_next = False
    for value in argv:
        text = str(value)
        if hide_next:
            redacted.append("<redacted>")
            hide_next = False
            continue
        redacted.append(text)
        hide_next = text.lower() in _SENSITIVE_OPTIONS
    return json.dumps(redacted, ensure_ascii=False)


def _sensitive_values(argv: Sequence[str]):
    return tuple(
        str(argv[index + 1])
        for index, value in enumerate(argv[:-1])
        if str(value).lower() in _SENSITIVE_OPTIONS and str(argv[index + 1])
    )


def _bounded_output(
    text: str,
    sensitive_values=(),
    limit: int = _DIAGNOSTIC_LIMIT,
) -> str:
    for value in sensitive_values:
        text = text.replace(value, "<redacted>")
    if len(text) <= limit:
        return repr(text)
    omitted = len(text) - limit
    return repr(text[:limit]) + " <truncated {} chars>".format(omitted)


def _command_diagnostic(
    argv: Sequence[str],
    returncode,
    stdout: str,
    stderr: str,
) -> str:
    sensitive_values = _sensitive_values(argv)
    return "returncode={} argv={} stdout={} stderr={}".format(
        returncode,
        _safe_argv(argv),
        _bounded_output(stdout, sensitive_values),
        _bounded_output(stderr, sensitive_values),
    )


def command_failure_detail(result, context: str = "") -> str:
    detail = getattr(result, "diagnostic", "")
    if not detail:
        output = getattr(result, "stderr", "") or getattr(result, "stdout", "")
        detail = "returncode={} output={}".format(
            getattr(result, "returncode", "unknown"),
            _bounded_output(str(output)),
        )
    return "{}: {}".format(context, detail) if context else detail


class CommandRunner:
    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger("ostriv_macos")

    def run(
        self,
        argv: Sequence[str],
        timeout: Optional[float] = None,
    ) -> CommandResult:
        command = _validate_external_argv(argv)
        self.logger.info(
            "command start argv=%s timeout=%s", _safe_argv(command), timeout
        )
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
        stdout = decode_output(result.stdout)
        stderr = decode_output(result.stderr)
        diagnostic = _command_diagnostic(
            command,
            result.returncode,
            stdout,
            stderr,
        )
        decoded = CommandResult(
            result.returncode,
            stdout,
            stderr,
            diagnostic,
        )
        self.logger.info("command result %s", diagnostic)
        return decoded


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

    @staticmethod
    def _shorten(path: Path) -> str:
        try:
            relative = path.expanduser().relative_to(Path.home())
        except ValueError:
            return str(path)
        return "~" if not relative.parts else "~/" + str(relative)

    def success(self, log_path: Path) -> None:
        self._line()
        self._line(
            "Ready. Quit and reopen CrossOver once, then open Ostriv (patched)."
        )
        self._line("Log: {}".format(self._shorten(log_path)))

    def restored(self, log_path: Path) -> None:
        self._line()
        self._line("Restored. Open Ostriv normally in CrossOver.")
        self._line("Log: {}".format(self._shorten(log_path)))

    def failure(self, message: str, log_path: Optional[Path] = None) -> None:
        self._line()
        self._line(message)
        if log_path is not None:
            self._line("Log: {}".format(self._shorten(log_path)))
