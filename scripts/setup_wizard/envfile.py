"""Comment preserving reader/writer for the three ``.env`` files.

The templates in the repository carry a lot of documentation as comments, so
the wizard never rewrites a file from scratch: it edits lines in place and only
falls back to appending a key when the template does not mention it at all.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["EnvFile", "Entry"]

_KEY_LINE = re.compile(r"^\s*(#)?\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


@dataclass
class Entry:
    """One key of an env file.

    Attributes:
        key: Variable name, or ``None`` for a pure comment line.
        value: Current value (``""`` when the key is commented out or empty).
        commented: True when the template only had ``# KEY=...``.
    """

    key: str | None
    value: str
    commented: bool = False


@dataclass
class EnvFile:
    """In-memory representation of one ``.env`` file.

    Attributes:
        path: File that will be written.
        example: Template used when ``path`` does not exist yet.
        lines: Source lines in file order (mutable render buffer).
        edits: Keys touched by the wizard, with their new value.
        pristine: Uncommented values as they were read, so the diff can be
            computed after the buffer has been rewritten.
    """

    path: Path
    example: Path
    lines: list[str] = field(default_factory=list)
    edits: dict[str, str] = field(default_factory=dict)
    pristine: dict[str, str] = field(default_factory=dict)

    # -- construction -------------------------------------------------------
    @classmethod
    def load(cls, path: Path, example: Path) -> EnvFile:
        """Read the file, falling back to its template when it is missing.

        Args:
            path: Target env file.
            example: Template to start from when ``path`` does not exist.

        Returns:
            A populated :class:`EnvFile`.
        """
        source = path if path.exists() else example
        try:
            text = source.read_text(encoding="utf-8")
        except OSError:
            text = ""
        instance = cls(path=path, example=example, lines=text.splitlines())
        instance.pristine = {
            entry.key: entry.value
            for entry in instance.entries()
            if entry.key and not entry.commented
        }
        return instance

    @property
    def exists(self) -> bool:
        """Report whether the target file is already on disk."""
        return self.path.exists()

    @property
    def from_example(self) -> bool:
        """Report whether the current content came from the template."""
        return not self.exists

    @property
    def key_path(self) -> str:
        """Return the repository-relative path of the target file."""
        from .util import REPO_ROOT

        try:
            return str(self.path.relative_to(REPO_ROOT))
        except ValueError:
            return str(self.path)

    # -- reading ------------------------------------------------------------
    def entries(self) -> list[Entry]:
        """Parse every line into an :class:`Entry`.

        Returns:
            One entry per source line; comment lines have ``key=None``.
        """
        out: list[Entry] = []
        for line in self.lines:
            match = _KEY_LINE.match(line)
            if not match:
                out.append(Entry(key=None, value=""))
                continue
            commented, key, value = match.group(1), match.group(2), match.group(3)
            out.append(Entry(key=key, value=value.strip(), commented=bool(commented)))
        return out

    def get(self, key: str, default: str = "") -> str:
        """Return the effective value of ``key``.

        Uncommented keys win over commented ones, so a template default stays
        visible without shadowing a real setting.

        Args:
            key: Variable name.
            default: Value returned when the key is absent.

        Returns:
            The value found, or ``default``.
        """
        found: str | None = None
        for entry in self.entries():
            if entry.key != key:
                continue
            if not entry.commented:
                return entry.value
            found = entry.value
        return found if found is not None else default

    def has(self, key: str) -> bool:
        """Report whether ``key`` is mentioned (commented or not)."""
        return any(entry.key == key for entry in self.entries())

    def original(self, key: str) -> str:
        """Return the value currently on disk/template for ``key``.

        Args:
            key: Variable name.

        Returns:
            The on-disk value, or ``""`` when the key is unknown.
        """
        return self.pristine.get(key, "")

    # -- writing ------------------------------------------------------------
    def set(self, key: str, value: str) -> None:
        """Stage a new value for ``key``.

        The line is replaced in place and the comment marker removed, so a
        ``# DOC_TTS_LINK_SECRET=`` in the template becomes an active setting.

        Args:
            key: Variable name.
            value: New value; ``""`` leaves the key present but empty.
        """
        self.edits[key] = value
        for index, line in enumerate(self.lines):
            match = _KEY_LINE.match(line)
            if match and match.group(2) == key:
                indent = re.match(r"^\s*", line).group(0) if line[:1].isspace() else ""
                self.lines[index] = f"{indent}{key}={value}"
                return
        self.lines.append(f"{key}={value}")

    def staged(self, key: str) -> str | None:
        """Return the staged value for ``key`` when it differs from disk.

        Args:
            key: Variable name.

        Returns:
            The pending value, or ``None`` when the key was not edited.
        """
        return self.edits.get(key)

    def changed_keys(self) -> list[str]:
        """Return the edited keys whose value differs from the current file.

        Returns:
            Sorted variable names that will actually change on disk.
        """
        return sorted(k for k, v in self.edits.items() if self.original(k) != v)

    def render(self) -> str:
        """Return the full text to write to disk.

        Returns:
            File content with the staged edits applied.
        """
        return "\n".join(self.lines) + "\n"

    def write(self, keep_backup: bool = True) -> Path | None:
        """Write the file, keeping a timestamped backup when it already existed.

        Args:
            keep_backup: Copy the previous file to ``<name>.bak-<ts>`` first.

        Returns:
            The backup path, or ``None`` when there was nothing to back up.
        """
        backup: Path | None = None
        if keep_backup and self.exists:
            stamp = _timestamp()
            backup = self.path.with_name(f"{self.path.name}.bak-{stamp}")
            shutil.copy2(self.path, backup)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.render(), encoding="utf-8")
        return backup

    def diff_against_disk(self) -> list[tuple[str, str, str]]:
        """List the changes the staged edits would introduce.

        Returns:
            Tuples of ``(key, current, new)`` sorted by key.
        """
        return sorted(
            (k, self.original(k), v) for k, v in self.edits.items() if self.original(k) != v
        )


def _timestamp() -> str:
    """Return a filesystem safe local timestamp.

    Returns:
        ``YYYYmmdd-HHMMSS``.
    """
    from datetime import datetime

    return datetime.now().strftime("%Y%m%d-%H%M%S")
