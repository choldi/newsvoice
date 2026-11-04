"""Minimal curses based TUI toolkit used by the setup wizard.

Standard library only: the wizard runs before any project environment exists.
The toolkit is a tiny list-of-rows view with modal dialogs on top — enough for
a guided installer without pulling in a framework.
"""

from __future__ import annotations

import curses
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
        elif key in (curses.KEY_UP, curses.KEY_DOWN) and total > 1:
            self.index = (self.index + (1 if key == curses.KEY_DOWN else -1)) % total

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
        if self.offset >= len(rows):
            self.offset = 0
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

    def _ensure_visible(self, screen_h: int) -> None:
        body = max(screen_h - 4, 1)
        if self.focus >= self.offset + body:
            self.offset = self.focus - body + 1
        elif self.focus < self.offset:
            self.offset = self.focus

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
        if curses.has_colors():
            self._init_colors()

        while not self.should_quit:
            self.draw(stdscr)
            key = stdscr.getch()
            self.handle(key)
        return 0

    @staticmethod
    def _init_colors() -> None:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(C_HEADER, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(C_FOCUS, curses.COLOR_BLACK, curses.COLOR_WHITE)
        curses.init_pair(C_HELP, curses.COLOR_BLUE, -1)
        curses.init_pair(C_OK, curses.COLOR_GREEN, -1)
        curses.init_pair(C_WARN, curses.COLOR_YELLOW, -1)
        curses.init_pair(C_BAD, curses.COLOR_RED, -1)
        curses.init_pair(C_DIM, curses.COLOR_WHITE, -1)
        curses.init_pair(C_VALUE, curses.COLOR_BLACK, -1)

    @staticmethod
    def _attr(color: int, extra: int = 0) -> int:
        if not curses.has_colors():
            return extra
        return curses.color_pair(color) | extra

    # -- drawing ------------------------------------------------------------
    def draw(self, stdscr: curses.window) -> None:
        """Paint the whole frame."""
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        if h < 6 or w < 30:
            stdscr.addnstr(0, 0, "Terminal demasiado pequeño (mínimo 30x6)", w - 1)
            stdscr.refresh()
            return

        screen = self.current
        # Header
        bar = f" {self.title} "
        stdscr.attron(self._attr(C_HEADER, curses.A_BOLD))
        stdscr.addnstr(0, 0, bar.ljust(w), w - 1)
        stdscr.attroff(self._attr(C_HEADER, curses.A_BOLD))
        stdscr.addnstr(1, 0, screen.title, w - 1, self._attr(C_FOCUS, curses.A_BOLD))
        if screen.subtitle:
            stdscr.addnstr(1, min(len(screen.title) + 2, w - 2), screen.subtitle, w - 2)

        # Body
        body_top = 2
        body_h = max(h - 4, 1)
        rows = screen.rows
        self._ensure_visible(h)
        y = body_top
        for index, row in enumerate(rows[self.offset : self.offset + body_h], start=self.offset):
            if row.advanced and not self.show_advanced:
                continue
            focused = index == self.focus and row.focusable and self.editing is None
            self._draw_row(stdscr, y, w, row, focused, index)
            y += 1
            if y >= body_top + body_h:
                break

        # Status + footer
        level_color = {"ok": C_OK, "warn": C_WARN, "bad": C_BAD}.get(self.message_level, C_DIM)
        if self.message:
            stdscr.addnstr(h - 2, 0, self.message, w - 1, self._attr(level_color))
        stdscr.addnstr(h - 1, 0, screen.footer.ljust(w), w - 1, self._attr(C_HELP))

        if self.modal is not None:
            self.modal.draw(self)
        elif self.editing is not None:
            self._place_cursor(stdscr, h, w)
        stdscr.refresh()

    def _draw_row(
        self, stdscr: curses.window, y: int, w: int, row: Row, focused: bool, index: int
    ) -> None:
        if row.kind == "heading":
            stdscr.addnstr(
                y, 1, row.label, w - 2, self._attr(C_FOCUS, curses.A_BOLD | curses.A_UNDERLINE)
            )
            return
        if row.kind == "note":
            color = {"ok": C_OK, "warn": C_WARN, "bad": C_BAD}.get(row.result, C_DIM)
            extra = curses.A_BOLD if row.result else curses.A_DIM
            stdscr.addnstr(y, 2, row.label, w - 3, self._attr(color, extra))
            return

        marker = "▸ " if focused else "  "
        label = row.label
        if focused and self.editing is row:
            marker = "▸ "
        prefix = marker + label
        value_x = max(len(prefix) + 2, 24)
        attr = self._attr(C_FOCUS, curses.A_BOLD) if focused else self._attr(C_VALUE)
        stdscr.addnstr(y, 1, prefix.ljust(value_x - 1), w - 2, attr)

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
        stdscr.addnstr(
            y,
            value_x,
            (value + suffix),
            w - value_x - 1,
            self._attr({"ok": C_OK, "warn": C_WARN, "bad": C_BAD}.get(row.result, C_VALUE)),
        )

        if focused and row.generate and not self.editing:
            hint = "[g genera]"
            stdscr.addnstr(y, max(w - len(hint) - 1, 1), hint, len(hint), self._attr(C_HELP))
        if row.help and focused:
            stdscr.addnstr(
                y + 1 if y + 1 < self._max_body_bottom(stdscr) else y,
                4,
                row.help,
                w - 5,
                self._attr(C_HELP, curses.A_DIM),
            )

    def _max_body_bottom(self, stdscr: curses.window) -> int:
        h, _ = stdscr.getmaxyx()
        return h - 3

    def _place_cursor(self, stdscr: curses.window, h: int, w: int) -> None:
        row = self.editing
        if row is None:
            return
        index = self._row_index(row)
        screen_top = 2
        y = screen_top + (index - self.offset)
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
        height = min(len(lines) + 4 + (1 if buttons else 0), h - 2)
        top = max((h - height) // 2, 0)
        left = max((w - width) // 2, 0)
        for row in range(height):
            scr.addnstr(top + row, left, " " * width, width)
        box = self._attr(C_FOCUS)
        for row in range(height):
            scr.addnstr(top + row, left, " " * width, width, box)
        title_line = f" {title} "
        scr.addnstr(top, left + 2, title_line, width - 4, self._attr(C_HEADER, curses.A_BOLD))
        for i, line in enumerate(lines):
            scr.addnstr(top + 1 + i, left + 2, line, width - 4, self._attr(C_VALUE))
        if buttons:
            y = top + height - 1
            x = left + 2
            for i, label in enumerate(buttons):
                text = f" {label} " if i != index else f"[ {label} ]"
                attr = self._attr(C_FOCUS, curses.A_BOLD) if i == index else self._attr(C_VALUE)
                scr.addnstr(y, x, text, width - 4, attr)
                x += len(text) + 1
        elif highlight and lines:
            y = top + 1 + index
            if y < top + height:
                scr.addnstr(
                    y, left, lines[index].ljust(width), width, self._attr(C_FOCUS, curses.A_BOLD)
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
        if key == ord("?"):
            self.info(
                "Ayuda",
                "↑/↓  mover · Enter  editar/abrir · Esc  atrás",
                "Tab  siguiente campo · a  opciones avanzadas",
                "g  generar secreto · Ctrl+R  valor por defecto",
                "q  salir",
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
