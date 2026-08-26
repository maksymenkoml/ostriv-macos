"""Durable, recoverable installation transactions."""

import copy
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Mapping

from .diagnostics import PatchError


JOURNAL_SCHEMA = 1
JOURNAL_CORRUPT_MESSAGE = "The installation journal is unreadable. Restore before trying again."
RECOVERY_REQUIRED_MESSAGE = "A previous installation needs recovery."
ROLLBACK_FAILED_MESSAGE = "Installation recovery failed. Restore before trying again."

logger = logging.getLogger("ostriv_macos")
if not logger.handlers:
    logger.addHandler(logging.NullHandler())
    logger.propagate = False


@dataclass(frozen=True)
class UndoRecord:
    kind: str
    data: Dict[str, object]


def atomic_write_json(path: Path, data: Dict[str, object]) -> None:
    """Replace *path* only after its complete JSON contents reach disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(path.parent),
            delete=False,
        ) as stream:
            temp_name = stream.name
            json.dump(data, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, str(path))
        if os.name != "nt":
            directory_descriptor = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


class InstallJournal:
    def __init__(self, path: Path):
        self.path = path
        self.data = self._load()

    def _load(self) -> Dict[str, object]:
        if not self.path.exists():
            return {"schema": JOURNAL_SCHEMA, "complete": True, "records": []}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            self._validate(loaded)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise PatchError(
                "install.journal_corrupt",
                JOURNAL_CORRUPT_MESSAGE,
                "Unable to load journal {}: {}: {}".format(
                    self.path, type(error).__name__, error
                ),
            ) from error
        return loaded

    @staticmethod
    def _validate(data: object) -> None:
        if not isinstance(data, dict):
            raise ValueError("journal root is not an object")
        if data.get("schema") != JOURNAL_SCHEMA:
            raise ValueError("unsupported journal schema {!r}".format(data.get("schema")))
        if not isinstance(data.get("complete"), bool):
            raise ValueError("journal complete flag is invalid")
        records = data.get("records")
        if not isinstance(records, list):
            raise ValueError("journal records are invalid")
        if "operation" in data and not isinstance(data["operation"], str):
            raise ValueError("journal operation is invalid")
        for index, item in enumerate(records):
            if not isinstance(item, dict):
                raise ValueError("journal record {} is not an object".format(index))
            if not isinstance(item.get("name"), str):
                raise ValueError("journal record {} name is invalid".format(index))
            if item.get("status") not in ("pending", "applied", "rolled_back"):
                raise ValueError("journal record {} status is invalid".format(index))
            undo = item.get("undo")
            if not isinstance(undo, dict):
                raise ValueError("journal record {} undo is invalid".format(index))
            if not isinstance(undo.get("kind"), str) or not isinstance(
                undo.get("data"), dict
            ):
                raise ValueError("journal record {} undo data is invalid".format(index))

    def _save(self, candidate: Dict[str, object]) -> None:
        atomic_write_json(self.path, candidate)
        self.data = candidate

    def start(self, operation: str) -> None:
        if self.data["records"] and not self.data.get("complete"):
            raise PatchError(
                "install.recovery_required",
                RECOVERY_REQUIRED_MESSAGE,
                "Cannot replace incomplete journal at {}".format(self.path),
            )
        candidate = {
            "schema": JOURNAL_SCHEMA,
            "operation": operation,
            "complete": False,
            "records": [],
        }
        self._save(candidate)

    def begin(self, name: str, undo: UndoRecord) -> int:
        candidate = copy.deepcopy(self.data)
        records = candidate["records"]
        records.append(
            {
                "name": name,
                "status": "pending",
                "undo": {"kind": undo.kind, "data": undo.data},
            }
        )
        self._save(candidate)
        return len(records) - 1

    def mark_applied(self, index: int) -> None:
        candidate = copy.deepcopy(self.data)
        candidate["records"][index]["status"] = "applied"
        self._save(candidate)

    def mark_rolled_back(self, index: int) -> None:
        candidate = copy.deepcopy(self.data)
        candidate["records"][index]["status"] = "rolled_back"
        self._save(candidate)

    def commit(self) -> None:
        candidate = copy.deepcopy(self.data)
        candidate["complete"] = True
        self._save(candidate)


class Transaction:
    def __init__(
        self,
        journal: InstallJournal,
        handlers: Mapping[str, Callable[[UndoRecord], None]],
    ):
        self.journal = journal
        self.handlers = handlers

    def start(self, operation: str) -> None:
        self.journal.start(operation)

    def _undo(self, record_data: Dict[str, object]) -> None:
        record = UndoRecord(record_data["kind"], record_data["data"])
        self.handlers[record.kind](record)

    def _rollback_failed(
        self, index: int, item: Dict[str, object], error: BaseException
    ) -> PatchError:
        undo = item["undo"]
        detail = (
            "Undo failed for operation={!r}, record={!r}, index={}, kind={!r}: {}: {}"
        ).format(
            self.journal.data.get("operation"),
            item.get("name"),
            index,
            undo.get("kind"),
            type(error).__name__,
            error,
        )
        logger.exception("install.rollback_failed: %s", detail)
        return PatchError("install.rollback_failed", ROLLBACK_FAILED_MESSAGE, detail)

    def step(
        self,
        name: str,
        undo: UndoRecord,
        action: Callable[[], None],
    ) -> None:
        index = self.journal.begin(name, undo)
        try:
            action()
            self.journal.mark_applied(index)
        except BaseException:
            item = self.journal.data["records"][index]
            try:
                self._undo(item["undo"])
            except BaseException as error:
                raise self._rollback_failed(index, item, error) from error
            self.journal.mark_rolled_back(index)
            raise

    def rollback(self) -> None:
        for index in range(len(self.journal.data["records"]) - 1, -1, -1):
            item = self.journal.data["records"][index]
            if item["status"] in ("pending", "applied"):
                try:
                    self._undo(item["undo"])
                except BaseException as error:
                    raise self._rollback_failed(index, item, error) from error
                self.journal.mark_rolled_back(index)
        self.journal.commit()

    def recover_incomplete(self) -> None:
        if not self.journal.data.get("complete"):
            self.rollback()
