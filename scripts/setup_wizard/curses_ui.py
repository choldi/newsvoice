"""Minimal curses based TUI toolkit used by the setup wizard.

Standard library only: the wizard runs before any project environment exists.
The toolkit is a tiny list-of-rows view with modal dialogs on top — enough for
a guided installer without pulling in a framework.
"""

from __future__ import annotations

import curses
import os
import textwrap
from collections.abc import Callable
from dataclasses import dataclass, field

__all__ = ["Action", "App", "Dialog", "Picker", "Row", "Screen"]

#: Entry kinds and whether they take focus.
FOCUSABLE = frozenset({"text", "secret", "choice", "bool", "button", "password"})

#: (key, symbol) pairs that confirm an edit or a dialog.
CONFIRM_KEYS = frozenset({10, 13, curses.KEY_ENTER})
#: (key, symbol) pairs that cancel an edit or close a modal.
CANCEL_KEYS = frozenset(
    {
        27,
    }
)

# Colour pairs, resolved at App start.
C_HEADER = 1
C_FOCUS = 2
C_HELP = 3
C_OK = 4
C_WARN = 5
C_BAD = 6
C_DIM = 7
C_VALUE = 8

#: Themes offered by the TUI, in cycle order.
THEME_CYCLE = ("auto", "dark", "light", "mono")


@dataclass(frozen=True)
class Theme:
    """Resolved colour palette for one TUI theme.

    Attributes:
        key: Stable theme id (``dark``, ``light`` or ``mono``).
        label: Name shown in the status line.
        roles: ``color role -> (foreground, background, base attr)``.
            ``-1`` means the terminal default, which keeps text readable on
            both light and dark backgrounds.
    """

    key: str
    label: str
    roles: dict[int, tuple[int, int, int]]

    @property
    def base_attrs(self) -> dict[int, int]:
        """Return the base curses attributes for each colour role."""
        return {role: base for role, (_fg, _bg, base) in self.roles.items()}


def _terminal_is_dark() -> bool:
    """Best-effort guess of the terminal background from ``COLORFGBG``."""
    spec = os.environ.get("COLORFGBG", "").strip()
    if not spec:
        return True
    try:
        background = int(spec.split(";")[-1])
    except ValueError:
        return True
    if background == 8:
        return True
    return background < 7


def _dark_theme(colors: int) -> Theme:
    if colors >= 256:
        roles = {
            C_HEADER: (16, 51, 0),
            C_FOCUS: (16, 255, 0),
            C_HELP: (117, -1, 0),
            C_OK: (120, -1, 0),
            C_WARN: (222, -1, 0),
            C_BAD: (210, -1, 0),
            C_DIM: (245, -1, 0),
            C_VALUE: (-1, -1, 0),
        }
    else:
        roles = {
            C_HEADER: (curses.COLOR_BLACK, curses.COLOR_CYAN, 0),
            C_FOCUS: (curses.COLOR_BLACK, curses.COLOR_WHITE, 0),
            C_HELP: (curses.COLOR_CYAN, -1, 0),
            C_OK: (curses.COLOR_GREEN, -1, 0),
            C_WARN: (curses.COLOR_YELLOW, -1, 0),
            C_BAD: (curses.COLOR_RED, -1, 0),
            C_DIM: (-1, -1, 0),
            C_VALUE: (-1, -1, 0),
        }
    return Theme("dark", "oscuro", roles)


def _light_theme(colors: int) -> Theme:
    if colors >= 256:
        roles = {
            C_HEADER: (255, 25, 0),
            C_FOCUS: (255, 25, 0),
            C_HELP: (25, -1, 0),
            C_OK: (28, -1, 0),
            C_WARN: (130, -1, 0),
            C_BAD: (124, -1, 0),
            C_DIM: (240, -1, 0),
            C_VALUE: (-1, -1, 0),
        }
    else:
        roles = {
            C_HEADER: (curses.COLOR_WHITE, curses.COLOR_BLUE, 0),
            C_FOCUS: (curses.COLOR_WHITE, curses.COLOR_BLUE, 0),
            C_HELP: (curses.COLOR_BLUE, -1, 0),
            C_OK: (curses.COLOR_GREEN, -1, curses.A_BOLD),
            C_WARN: (curses.COLOR_MAGENTA, -1, curses.A_BOLD),
            C_BAD: (curses.COLOR_RED, -1, curses.A_BOLD),
            C_DIM: (-1, -1, curses.A_DIM),
            C_VALUE: (-1, -1, 0),
        }
    return Theme("light", "claro", roles)


def _mono_theme(_colors: int) -> Theme:
    roles = {
        C_HEADER: (-1, -1, curses.A_BOLD | curses.A_REVERSE),
        C_FOCUS: (-1, -1, curses.A_BOLD | curses.A_REVERSE),
        C_HELP: (-1, -1, curses.A_DIM),
        C_OK: (-1, -1, curses.A_BOLD),
        C_WARN: (-1, -1, curses.A_BOLD | curses.A_UNDERLINE),
        C_BAD: (-1, -1, curses.A_BOLD | curses.A_REVERSE),
        C_DIM: (-1, -1, curses.A_DIM),
        C_VALUE: (-1, -1, 0),
    }
    return Theme("mono", "monocromo", roles)


def resolve_theme(key: str, colors: int = 8) -> Theme:
    """Return the concrete palette for ``key``.

    Args:
        key: Theme id, including the special ``auto`` value.
        colors: Number of colours the terminal supports.

    Returns:
        A fully resolved :class:`Theme`.
    """
    if key == "auto":
        key = "dark" if _terminal_is_dark() else "light"
    if key == "light":
        return _light_theme(colors)
    if key == "mono":
        return _mono_theme(colors)
    return _dark_theme(colors)


def _wrap_text(text: str, width: int) -> list[str]:
    """Wrap ``text`` to ``width`` columns, honouring explicit newlines.

    Args:
        text: Text to wrap.
        width: Maximum number of columns per line.

    Returns:
        The wrapped lines, never empty (blank input yields one empty line).
    """
    if width < 1:
        return [text]
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        wrapped = textwrap.wrap(
            paragraph,
            width=width,
            break_long_words=True,
            break_on_hyphens=False,
            replace_whitespace=False,
            drop_whitespace=False,
        )
        if wrapped:
            lines.extend(line.rstrip() for line in wrapped)
        else:
            lines.append("")
    return lines or [""]


@dataclass
class Row:
    """One line of a screen.

    Attributes:
        kind: ``heading``, ``note``, ``text``, ``secret``, ``choice``,
            ``bool``, ``button`` or ``password``.
        label: Field name shown on the left.
        value: Current value (also the button label for ``button``).
        help: One line explanation rendered dim under the value.
        options: Allowed values for a ``choice`` row.
        action: Stable id handed back to the activate callback for buttons.
        default: Value used by the reset shortcut.
        generate: ``hex32`` when Ctrl+G can mint a secret for this row.
        advanced: Hidden until the advanced toggle is on.
        secret: Mask the value with dots until revealed.
        result: Optional pre-computed status for review screens
            (``ok``, ``warn``, ``bad`` or empty).
    """

    kind: str
    label: str = ""
    value: str = ""
    help: str = ""
    options: list[str] = field(default_factory=list)
    action: str = ""
    default: str = ""
    generate: str = ""
    advanced: bool = False
    secret: bool = False
    result: str = ""

    @property
    def focusable(self) -> bool:
        """Report whether the row can receive keyboard focus."""
        return self.kind in FOCUSABLE

    @property
    def display_value(self) -> str:
        """Return the text to render for the value column."""
        if self.kind == "bool":
            return "sí" if self.value.strip().lower() in ("true", "1", "yes") else "no"
        if self.secret and self.value:
            return "•" * min(len(self.value), 40)
        if not self.value:
            return "" if self.kind != "button" else self.label
        return self.value


@dataclass
class Screen:
    """A wizard step: a title, rows and optional callbacks.

    Attributes:
        title: Big heading rendered in the header.
        subtitle: Progress/context line under the title.
        rows: Content rows, in order.
        footer: Key hints shown in the last line.
        on_enter: Called with ``(app)`` the first time the screen is shown.
        on_activate: Called with ``(app, row)`` when Enter is pressed on a
            non editing row (buttons, choices, passwords).
        on_change: Called with ``(app, row)`` after a value is committed.
    """

    title: str
    subtitle: str = ""
    rows: list[Row] = field(default_factory=list)
    footer: str = ""
    on_enter: Callable[[App], None] | None = None
    on_activate: Callable[[App, Row], None] | None = None
    on_change: Callable[[App, Row], None] | None = None


@dataclass
class Action:
    """Result of a key press.

    Attributes:
        kind: ``none``, ``redraw`` or ``quit``.
    """

    kind: str = "none"


class Dialog:
    """Modal message with a list of labelled buttons."""

    def __init__(
        self,
        title: str,
        lines: list[str],
        buttons: list[tuple[str, Callable[[App], None]]] | None = None,
    ) -> None:
        self.title = title
        self.lines = lines
        self.buttons = buttons or [("Aceptar", lambda app: app.close_modal())]
        self.index = 0
        self.offset = 0

    def handle(self, key: int, app: App) -> None:
        """Process one key press while the dialog owns the keyboard."""
        total = len(self.buttons)
        if key in CONFIRM_KEYS:
            self.buttons[self.index][1](app)
        elif key in CANCEL_KEYS:
            app.close_modal()
        elif key in (curses.KEY_RIGHT, 9):  # right / tab
            self.index = (self.index + 1) % total
        elif key in (curses.KEY_LEFT, curses.KEY_BTAB):
            self.index = (self.index - 1) % total
        elif key == curses.KEY_PPAGE:
            self.offset = max(self.offset - 5, 0)
        elif key == curses.KEY_NPAGE:
            self.offset += 5
        elif key == curses.KEY_HOME:
            self.offset = 0
        elif key == curses.KEY_END:
            self.offset = 10**9
        elif key in (curses.KEY_UP, curses.KEY_DOWN):
            if total > 1:
                self.index = (self.index + (1 if key == curses.KEY_DOWN else -1)) % total
            else:
                self.offset = max(self.offset + (1 if key == curses.KEY_DOWN else -1), 0)

    def draw(self, app: App) -> None:
        """Render the dialog on top of the current screen."""
        app.draw_modal(self.title, self.lines, [b[0] for b in self.buttons], self.index)


class Picker:
    """Modal single choice list."""

    def __init__(
        self,
        title: str,
        options: list[str],
        current: str,
        on_pick: Callable[[App, str], None],
        help_text: str = "",
    ) -> None:
        self.title = title
        self.options = options
        self.help = help_text
        self.on_pick = on_pick
        try:
            self.index = options.index(current)
        except ValueError:
            self.index = 0

    def handle(self, key: int, app: App) -> None:
        """Process one key press while the picker owns the keyboard."""
        if key in CONFIRM_KEYS:
            self.on_pick(app, self.options[self.index])
        elif key in CANCEL_KEYS:
            app.close_modal()
        elif key in (curses.KEY_UP, curses.KEY_PPAGE):
            self.index = (self.index - 1) % len(self.options)
        elif key in (curses.KEY_DOWN, curses.KEY_NPAGE):
            self.index = (self.index + 1) % len(self.options)
        elif key == curses.KEY_HOME:
            self.index = 0
        elif key == curses.KEY_END:
            self.index = len(self.options) - 1

    def draw(self, app: App) -> None:
        """Render the picker on top of the current screen."""
        lines = list(self.options)
        if self.help:
            lines.append("")
            lines.append(self.help)
        app.draw_modal(self.title, lines, [], self.index, highlight=True)


class App:
    """Screen stack renderer and keyboard dispatcher."""

    def __init__(self, title: str) -> None:
        self.title = title
        self.screens: list[Screen] = []
        self.focus = 0
        self.offset = 0
        self.editing: Row | None = None
        self.editing_original = ""
        self.cursor = 0
        self.revealed: set[int] = set()
        self.show_advanced = False
        self.modal: Dialog | Picker | None = None
        self.message = ""
        self.message_level = ""
        self.visited: set[int] = set()
        self.should_quit = False
        self.window: curses.window | None = None
        requested = os.environ.get("NEWSVOICE_TUI_THEME", "auto").strip().lower()
        self.theme_key = requested if requested in THEME_CYCLE else "auto"
        self._theme = resolve_theme(self.theme_key)
        self._colors = 8
        self._colored = False
        self._base_attrs: dict[int, int] = {}
        self._visual_starts: dict[int, int] = {}

    # -- stack navigation ---------------------------------------------------
    def push(self, screen: Screen) -> None:
        """Append a screen to the stack and focus its first row."""
        self.screens.append(screen)
        self.offset = 0
        self.editing = None
        self.visited.add(id(screen))
        if screen.on_enter:
            screen.on_enter(self)
        self.focus = self._first_focusable()

    # -- focus management ---------------------------------------------------
    def _is_focusable(self, row: Row) -> bool:
        return row.focusable and (self.show_advanced or not row.advanced)

    def _first_focusable(self) -> int:
        for index, row in enumerate(self.current.rows):
            if self._is_focusable(row):
                return index
        return 0

    def _last_focusable(self) -> int:
        for index in range(len(self.current.rows) - 1, -1, -1):
            if self._is_focusable(self.current.rows[index]):
                return index
        return 0

    def _move(self, step: int) -> bool:
        """Move the focus by ``step``, wrapping around both ends.

        Args:
            step: Direction, ``-1`` up and ``1`` down.

        Returns:
            True when the focus actually moved.
        """
        total = len(self.current.rows)
        if not total:
            return False
        index = self.focus
        for _ in range(total):
            index = (index + step) % total
            row = self.current.rows[index]
            if self._is_focusable(row):
                self.focus = index
                return True
        return False

    def refocus(self) -> None:
        """Keep the focus valid after the current screen's rows changed."""
        self._clamp_focus()

    def _clamp_focus(self) -> None:
        """Keep the focus on a focusable row after a view change."""
        rows = self.current.rows
        if 0 <= self.focus < len(rows) and self._is_focusable(rows[self.focus]):
            return
        self.focus = self._first_focusable()

    def pop(self) -> None:
        """Leave the current screen (no-op on the root one)."""
        if len(self.screens) <= 1:
            self.ask_quit()
            return
        self.screens.pop()
        self.offset = 0
        self.editing = None
        self.focus = self._first_focusable()

    def replace(self, screen: Screen) -> None:
        """Swap the top of the stack for ``screen``."""
        if self.screens:
            self.screens.pop()
        self.push(screen)

    @property
    def current(self) -> Screen:
        """Return the screen currently on top of the stack."""
        return self.screens[-1]

    # -- modals -------------------------------------------------------------
    def close_modal(self) -> None:
        """Dismiss whatever modal is open."""
        self.modal = None

    def info(self, title: str, *lines: str) -> None:
        """Show a read-only message dialog."""
        self.modal = Dialog(title, list(lines))

    # -- feedback -----------------------------------------------------------
    def say(self, text: str, level: str = "") -> None:
        """Set the transient status line (``ok``, ``warn`` or ``bad``)."""
        self.message = text
        self.message_level = level

    def ask_quit(self) -> None:
        """Open the leave confirmation dialog."""
        self.modal = Dialog(
            "Salir",
            ["Se perderán los cambios que no se hayan guardado."],
            [
                ("Seguir editando", lambda app: app.close_modal()),
                ("Salir", lambda app: (app.close_modal(), setattr(app, "should_quit", True))),
            ],
        )

    # -- helpers ------------------------------------------------------------
    def visible_rows(self) -> list[Row]:
        """Return the rows that are focusable in the current view."""
        return [
            row
            for row in self.current.rows
            if row.focusable and (self.show_advanced or not row.advanced)
        ]

    def _row_index(self, row: Row) -> int:
        try:
            return self.current.rows.index(row)
        except ValueError:
            return -1

    # -- main loop ----------------------------------------------------------
    def redraw(self) -> None:
        """Repaint the frame from inside a long running callback."""
        if self.window is None:
            return
        try:
            self.draw(self.window)
        except curses.error:
            pass

    def run(self, stdscr: curses.window) -> int:
        """Render and dispatch keys until the user leaves.

        Args:
            stdscr: The curses window created by the caller.

        Returns:
            Process exit status (0 on a clean exit).
        """
        self.window = stdscr
        curses.curs_set(0)
        stdscr.keypad(True)
        stdscr.timeout(-1)
        self._apply_theme()

        while not self.should_quit:
            self.draw(stdscr)
            key = stdscr.getch()
            self.handle(key)
        return 0

    def _apply_theme(self) -> None:
        """Resolve and install the active palette as curses colour pairs."""
        self._colors = 8
        self._colored = False
        try:
            has_colors = curses.has_colors()
        except curses.error:
            has_colors = False
        if has_colors:
            try:
                curses.start_color()
                curses.use_default_colors()
                self._colors = getattr(curses, "COLORS", 8) or 8
                self._colored = True
            except curses.error:
                self._colored = False
        self._theme = resolve_theme(self.theme_key, self._colors)
        self._base_attrs = self._theme.base_attrs
        if not self._colored:
            return
        for role, (foreground, background, _base) in self._theme.roles.items():
            try:
                curses.init_pair(role, foreground, background)
            except curses.error:
                pass

    def cycle_theme(self) -> None:
        """Switch to the next visually different theme."""
        try:
            start = THEME_CYCLE.index(self.theme_key)
        except ValueError:
            start = 0
        for step in range(1, len(THEME_CYCLE) + 1):
            candidate = THEME_CYCLE[(start + step) % len(THEME_CYCLE)]
            if resolve_theme(candidate, self._colors).key != self._theme.key:
                self.theme_key = candidate
                break
        self._apply_theme()
        self.say(f"Tema: {self.theme_status()}", "ok")

    def theme_status(self) -> str:
        """Return a human readable name for the active theme."""
        if self.theme_key == "auto":
            return f"auto · {self._theme.label}"
        return self._theme.label

    def _attr(self, color: int, extra: int = 0) -> int:
        base = self._base_attrs.get(color, 0)
        if not self._colored:
            return base | extra
        return curses.color_pair(color) | base | extra

    # -- drawing ------------------------------------------------------------
    def _put(
        self,
        stdscr: curses.window,
        y: int,
        x: int,
        text: str,
        attr: int = 0,
        limit: int | None = None,
    ) -> None:
        """Write text clipped to ``limit`` columns, even with multibyte chars."""
        if limit is None:
            _h, w = stdscr.getmaxyx()
            limit = max(w - x - 1, 1)
        clipped = text[: max(limit, 0)]
        if not clipped:
            return
        try:
            stdscr.addnstr(y, x, clipped, len(clipped.encode("utf-8")), attr)
        except curses.error:
            pass

    def draw(self, stdscr: curses.window) -> None:
        """Paint the whole frame."""
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        if h < 6 or w < 30:
            self._put(stdscr, 0, 0, "Terminal demasiado pequeño (mínimo 30x6)", limit=w - 1)
            stdscr.refresh()
            return

        screen = self.current
        # Header
        bar = f" {self.title} "
        self._put(
            stdscr,
            0,
            0,
            bar.ljust(w),
            self._attr(C_HEADER, curses.A_BOLD),
            limit=w - 1,
        )
        self._put(stdscr, 1, 0, screen.title, self._attr(C_FOCUS, curses.A_BOLD), limit=w - 1)
        if screen.subtitle:
            self._put(
                stdscr,
                1,
                min(len(screen.title) + 2, w - 2),
                screen.subtitle,
                limit=max(w - 3, 1),
            )

        # Status (wrapped) + footer
        level_color = {"ok": C_OK, "warn": C_WARN, "bad": C_BAD}.get(self.message_level, C_DIM)
        status_lines = _wrap_text(self.message, max(w - 1, 1)) if self.message else []
        max_status = max(h - 7, 1)
        if len(status_lines) > max_status:
            status_lines = status_lines[:max_status]
            status_lines[-1] = status_lines[-1][: max(w - 4, 1)] + "…"
        reserved = max(len(status_lines), 1)

        # Body
        body_top = 2
        body_h = max(h - 3 - reserved, 1)
        layout = self._build_layout(w)
        total_lines = layout[-1][1] + len(layout[-1][2]) if layout else 0
        focus_start = 0
        focus_end = 0
        for index, start, lines in layout:
            if index == self.focus:
                focus_start = start
                focus_end = start + len(lines)
                break
        if focus_end > self.offset + body_h:
            self.offset = focus_end - body_h
        if focus_start < self.offset:
            self.offset = focus_start
        self.offset = max(0, min(self.offset, max(total_lines - body_h, 0)))
        self._visual_starts = {index: start for index, start, _lines in layout}
        for index, start, lines in layout:
            for line_no, segments in enumerate(lines):
                visual = start + line_no
                if visual < self.offset or visual >= self.offset + body_h:
                    continue
                y = body_top + (visual - self.offset)
                for x, text, attr in segments:
                    self._put(stdscr, y, x, text, attr)

        for i, line in enumerate(status_lines):
            self._put(
                stdscr,
                h - 1 - reserved + i,
                0,
                line,
                self._attr(level_color),
                limit=w - 1,
            )
        self._put(
            stdscr,
            h - 1,
            0,
            screen.footer.ljust(w),
            self._attr(C_HELP),
            limit=w - 1,
        )

        if self.modal is not None:
            self.modal.draw(self)
        elif self.editing is not None:
            self._place_cursor(stdscr, h, w)
        stdscr.refresh()

    def _row_visual_lines(
        self, row: Row, w: int, focused: bool
    ) -> list[list[tuple[int, str, int]]]:
        """Return the wrapped visual lines and segments for one row."""
        if row.kind == "heading":
            attr = self._attr(C_FOCUS, curses.A_BOLD | curses.A_UNDERLINE)
            return [[(1, line, attr)] for line in _wrap_text(row.label, max(w - 2, 1))]
        if row.kind == "note":
            color = {"ok": C_OK, "warn": C_WARN, "bad": C_BAD}.get(row.result, C_DIM)
            extra = curses.A_BOLD if row.result else curses.A_DIM
            attr = self._attr(color, extra)
            return [[(2, line, attr)] for line in _wrap_text(row.label, max(w - 3, 1))]

        marker = "▸ " if focused else "  "
        if focused and self.editing is row:
            marker = "▸ "
        prefix = marker + row.label
        value_x = max(len(prefix) + 2, 24)
        label_attr = self._attr(C_FOCUS, curses.A_BOLD) if focused else self._attr(C_VALUE)
        first: list[tuple[int, str, int]] = [(1, prefix.ljust(value_x - 1), label_attr)]

        value = row.display_value
        if focused and self.editing is row:
            value = row.value
        max_val = max(w - value_x - 3, 4)
        start = 0
        if focused and self.editing is row:
            if self.cursor > max_val - 1:
                start = self.cursor - max_val + 1
            value = value[start : start + max_val]
        elif len(value) > max_val:
            value = value[: max_val - 1] + "…"

        suffix = ""
        if row.result:
            suffix = {"ok": " ✓", "warn": " ⚠", "bad": " ✗"}.get(row.result, "")
        value_attr = self._attr({"ok": C_OK, "warn": C_WARN, "bad": C_BAD}.get(row.result, C_VALUE))
        first.append((value_x, value + suffix, value_attr))

        if focused and row.generate and not self.editing:
            hint = "[g genera]"
            first.append((max(w - len(hint) - 1, 1), hint, self._attr(C_HELP)))

        lines = [first]
        if row.help and focused:
            help_attr = self._attr(C_HELP, curses.A_DIM)
            for line in _wrap_text(row.help, max(w - 5, 1)):
                lines.append([(4, line, help_attr)])
        return lines

    def _build_layout(self, w: int) -> list[tuple[int, int, list[list[tuple[int, str, int]]]]]:
        """Lay out the visible rows, accounting for wrapped lines."""
        layout: list[tuple[int, int, list[list[tuple[int, str, int]]]]] = []
        visual = 0
        for index, row in enumerate(self.current.rows):
            if row.advanced and not self.show_advanced:
                continue
            focused = index == self.focus and row.focusable and self.editing is None
            lines = self._row_visual_lines(row, w, focused)
            layout.append((index, visual, lines))
            visual += len(lines)
        return layout

    def _place_cursor(self, stdscr: curses.window, h: int, w: int) -> None:
        row = self.editing
        if row is None:
            return
        index = self._row_index(row)
        visual = self._visual_starts.get(index, 0)
        screen_top = 2
        y = screen_top + (visual - self.offset)
        if y < screen_top or y >= h - 3:
            return
        value_x = max(len("▸ " + row.label) + 2, 24)
        max_val = max(w - value_x - 3, 4)
        start = self.cursor - max_val + 1 if self.cursor > max_val - 1 else 0
        curses.curs_set(1)
        stdscr.move(y, min(value_x + (self.cursor - start), w - 2))
        curses.curs_set(0)

    def draw_modal(
        self,
        title: str,
        lines: list[str],
        buttons: list[str],
        index: int,
        *,
        highlight: bool = False,
    ) -> None:
        """Render a modal box over the current frame.

        Args:
            title: Modal heading.
            lines: Body lines (buttons are drawn for dialogs only).
            buttons: Button labels; empty for a selection list.
            index: Focused button or option.
            highlight: True when ``index`` selects an option line.
        """
        scr = _MODAL_SCR
        if scr is None:
            return
        h, w = scr.getmaxyx()
        widest = max((len(line) + 4 for line in lines), default=0)
        width = max(min(max(len(title) + 6, widest), w - 4), 24)
        inner = max(width - 4, 1)
        display_lines: list[str] = []
        if highlight:
            display_lines = list(lines)
        else:
            for line in lines:
                display_lines.extend(_wrap_text(line, inner))
        buttons_h = 1 if buttons else 0
        height = min(max(len(display_lines) + 4 + buttons_h, 6), h - 2)
        body_capacity = max(height - 2 - buttons_h, 1)
        max_scroll = max(len(display_lines) - body_capacity, 0)
        scroll = 0 if highlight else getattr(self.modal, "offset", 0)
        if highlight and lines:
            scroll = max(0, min(index - body_capacity + 1, max_scroll))
        scroll = max(0, min(scroll, max_scroll))
        if self.modal is not None and hasattr(self.modal, "offset"):
            self.modal.offset = scroll
        top = max((h - height) // 2, 0)
        left = max((w - width) // 2, 0)
        for row in range(height):
            self._put(scr, top + row, 0, " " * w, limit=w)
        box = self._attr(C_FOCUS)
        for row in range(height):
            self._put(scr, top + row, left, " " * width, box, limit=width)
        title_line = f" {title} "
        if max_scroll > 0 and not highlight:
            title_line += "· PgUp/PgDn "
        self._put(
            scr,
            top,
            left + 2,
            title_line,
            self._attr(C_HEADER, curses.A_BOLD),
            limit=width - 4,
        )
        for i in range(body_capacity):
            source = scroll + i
            if source >= len(display_lines):
                break
            self._put(
                scr,
                top + 1 + i,
                left + 2,
                display_lines[source],
                self._attr(C_VALUE),
                limit=inner,
            )
        if buttons:
            y = top + height - 1
            x = left + 2
            for i, label in enumerate(buttons):
                text = f" {label} " if i != index else f"[ {label} ]"
                attr = self._attr(C_FOCUS, curses.A_BOLD) if i == index else self._attr(C_VALUE)
                self._put(scr, y, x, text, attr, limit=width - 4)
                x += len(text) + 1
        elif highlight and lines:
            row = index - scroll
            if 0 <= row < body_capacity:
                y = top + 1 + row
                self._put(
                    scr,
                    y,
                    left,
                    lines[index].ljust(width),
                    self._attr(C_FOCUS, curses.A_BOLD),
                    limit=width,
                )
        scr.refresh()

    # -- keyboard -----------------------------------------------------------
    def handle(self, key: int) -> Action:
        """Dispatch one key press to the modal, the editor or the screen."""
        if self.modal is not None:
            self.modal.handle(key, self)
            return Action()
        if self.editing is not None:
            return self._handle_edit(key)
        return self._handle_nav(key)

    def _handle_nav(self, key: int) -> Action:
        if key in (ord("q"), ord("Q")):
            self.ask_quit()
            return Action()
        if key in (27, curses.KEY_BACKSPACE) and len(self.screens) > 1:
            self.pop()
            self.say("")
            return Action()
        if key in (curses.KEY_UP, curses.KEY_BTAB):
            self._move(-1)
            self.say("")
            return Action()
        if key in (curses.KEY_DOWN, ord("\t")):
            self._move(1)
            self.say("")
            return Action()
        if key == curses.KEY_HOME:
            self.focus = self._first_focusable()
            return Action()
        if key == curses.KEY_END:
            self.focus = self._last_focusable()
            return Action()
        if key == ord("a") or key == ord("A"):
            self.show_advanced = not self.show_advanced
            self._clamp_focus()
            self.say(
                "Opciones avanzadas visibles"
                if self.show_advanced
                else "Opciones avanzadas ocultas"
            )
            return Action()
        if key in (curses.KEY_F2, ord("t"), ord("T")):
            self.cycle_theme()
            return Action()
        if key == ord("?"):
            self.info(
                "Ayuda",
                "↑/↓  mover · Enter  editar/abrir · Esc  atrás",
                "Tab  siguiente campo · a  opciones avanzadas",
                "g  generar secreto · Ctrl+R  valor por defecto",
                "t / F2  cambiar tema · q  salir",
            )
            return Action()

        row = self._focused_row()
        if row is None:
            return Action()

        if key in CONFIRM_KEYS:
            self._activate(row)
            return Action()
        if key in (7, ord("g"), ord("G")) and row.generate:
            from .util import generate_secret

            row.value = generate_secret()
            self.say(f"Secreto generado para {row.label or row.action}", "ok")
            self._notify_change(row)
            return Action()
        if key == 18 and row.default:  # Ctrl+R
            row.value = row.default
            self.say(f"{row.label or row.action} restaurado al valor por defecto")
            self._notify_change(row)
            return Action()
        if key == ord(" ") and row.kind == "bool":
            row.value = "true" if row.value != "true" else "false"
            self._notify_change(row)
            return Action()
        if key in (curses.KEY_LEFT, curses.KEY_RIGHT) and row.kind == "choice" and row.options:
            step = 1 if key == curses.KEY_RIGHT else -1
            try:
                pos = row.options.index(row.value)
            except ValueError:
                pos = 0
            row.value = row.options[(pos + step) % len(row.options)]
            self._notify_change(row)
            return Action()
        return Action()

    def _handle_edit(self, key: int) -> Action:
        row = self.editing
        if row is None:
            return Action()
        if key in CANCEL_KEYS:
            row.value = self.editing_original
            self.editing = None
            self.say("Edición cancelada")
            return Action()
        if key in CONFIRM_KEYS or key == 9:
            self.editing = None
            self.editing_original = ""
            self.say(f"{row.label or row.action} guardado en memoria", "ok")
            self._notify_change(row)
            return Action()
        if key in (curses.KEY_BACKSPACE, 127, 8):
            if self.cursor > 0:
                row.value = row.value[: self.cursor - 1] + row.value[self.cursor :]
                self.cursor -= 1
            return Action()
        if key == curses.KEY_DC:
            if self.cursor < len(row.value):
                row.value = row.value[: self.cursor] + row.value[self.cursor + 1 :]
            return Action()
        if key == curses.KEY_LEFT:
            self.cursor = max(self.cursor - 1, 0)
            return Action()
        if key == curses.KEY_RIGHT:
            self.cursor = min(self.cursor + 1, len(row.value))
            return Action()
        if key == curses.KEY_HOME:
            self.cursor = 0
            return Action()
        if key == curses.KEY_END:
            self.cursor = len(row.value)
            return Action()
        if 32 <= key <= 126:
            row.value = row.value[: self.cursor] + chr(key) + row.value[self.cursor :]
            self.cursor += 1
        elif key == ord("\n"):
            pass
        return Action()

    def _focused_row(self) -> Row | None:
        rows = self.current.rows
        if not rows:
            return None
        if 0 <= self.focus < len(rows) and self._is_focusable(rows[self.focus]):
            return rows[self.focus]
        index = self._first_focusable()
        self.focus = index
        row = rows[index]
        return row if self._is_focusable(row) else None

    def _activate(self, row: Row) -> None:
        if row.kind == "choice" and row.options:
            self.modal = Picker(
                row.label or "Selecciona", row.options, row.value, self._pick, row.help
            )
            return
        if row.kind == "bool":
            row.value = "true" if row.value != "true" else "false"
            self._notify_change(row)
            return
        if row.kind == "password":
            self.modal = _PasswordDialog(row)
            return
        if row.kind == "button":
            if self.current.on_activate:
                self.current.on_activate(self, row)
            return
        self.editing = row
        self.editing_original = row.value
        self.cursor = len(row.value)
        self.say(f"Editando {row.label} · Enter confirma · Esc cancela")

    def _pick(self, app: App, value: str) -> None:
        row = self._focused_row()
        self.close_modal()
        if row is None:
            return
        row.value = value
        self._notify_change(row)
        self.say(f"{row.label} = {value}", "ok")

    def _notify_change(self, row: Row) -> None:
        if self.current.on_change:
            self.current.on_change(self, row)


class _PasswordDialog:
    """Modal used to collect a password before hashing it."""

    def __init__(self, row: Row) -> None:
        self.row = row
        self.first = ""
        self.second = ""
        self.stage = 0
        self.error = ""

    def handle(self, key: int, app: App) -> None:
        """Collect two password entries then hash them."""
        if key in CANCEL_KEYS:
            app.close_modal()
            return
        if key in CONFIRM_KEYS:
            self._advance(app)
            return
        if key in (curses.KEY_BACKSPACE, 127, 8):
            if self.stage == 0:
                self.first = self.first[:-1]
            else:
                self.second = self.second[:-1]
            return
        if 32 <= key <= 126:
            if self.stage == 0:
                self.first += chr(key)
            else:
                self.second += chr(key)

    def _advance(self, app: App) -> None:
        from .settings import hash_password

        if self.stage == 0:
            if not self.first:
                self.error = "La contraseña no puede estar vacía."
                return
            self.stage = 1
            self.error = ""
            return
        if self.first != self.second:
            self.stage = 0
            self.first = ""
            self.second = ""
            self.error = "Las contraseñas no coinciden; inténtalo otra vez."
            return
        digest = hash_password(self.first)
        if digest is None:
            self.error = (
                "No se pudo importar bcrypt (instala el entorno de apps/news). "
                "Usa ADMIN_API_TOKEN en su lugar."
            )
            return
        self.row.value = digest
        app.close_modal()
        app.say("Hash bcrypt generado para ADMIN_PASSWORD_HASH", "ok")
        if app.current.on_change:
            app.current.on_change(app, self.row)

    def draw(self, app: App) -> None:
        """Render the two-step password prompt."""
        if self.stage == 0:
            prompt = "Contraseña: " + "•" * len(self.first)
        else:
            prompt = "Repite:       " + "•" * len(self.second)
        lines = [prompt]
        if self.error:
            lines.append(self.error)
        lines.append("Enter para continuar · Esc cancela")
        app.draw_modal("ADMIN_PASSWORD_HASH", lines, [], 0)


#: Window the modal renderer paints on; set by the runner once curses started.
_MODAL_SCR: curses.window | None = None


def set_modal_screen(window: curses.window) -> None:
    """Register the curses window modals should be drawn on.

    Args:
        window: The window created by :func:`run_tui`.
    """
    global _MODAL_SCR
    _MODAL_SCR = window


def run_tui(app: App) -> int:
    """Start curses and run ``app`` inside it.

    Args:
        app: Configured application.

    Returns:
        Process exit status.
    """

    def _wrapped(stdscr: curses.window) -> int:
        set_modal_screen(stdscr)
        return app.run(stdscr)

    return curses.wrapper(_wrapped)
