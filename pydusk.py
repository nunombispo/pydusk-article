"""
pydusk — a terminal disk usage analyzer, inspired by ncdu.

Usage:
    python pydusk.py [PATH]

Keyboard shortcuts:
    ↑           Move up
    ↓           Move down
    →   Enter directory
    ←  Go up one level
    d           Delete selected entry (with confirmation)
    r           Re-scan current directory
    q           Quit
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import typer
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Vertical
from textual.coordinate import Coordinate
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Label, Static


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class DiskEntry:
    path: Path
    size: int
    is_dir: bool
    children: list["DiskEntry"] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.name or str(self.path)


def scan(path: Path) -> DiskEntry:
    """
    Scan *path* and return a DiskEntry tree (including subdirectories),
    children sorted largest-first.
    """
    path = path.resolve()
    children: list[DiskEntry] = []

    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    ep = Path(entry.path)
                    if entry.is_symlink():
                        size = entry.stat(follow_symlinks=False).st_size
                        children.append(DiskEntry(ep, size, False))
                    elif entry.is_dir(follow_symlinks=False):
                        children.append(scan(ep))
                    else:
                        size = entry.stat(follow_symlinks=False).st_size
                        children.append(DiskEntry(ep, size, False))
                except (PermissionError, OSError):
                    pass
    except (PermissionError, OSError):
        pass

    children.sort(key=lambda e: e.size, reverse=True)
    total = sum(c.size for c in children)
    return DiskEntry(path, total, True, children)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BAR_WIDTH = 20


def fmt_size(n: int) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024:
            return f"{n:6.1f} {unit}" if unit != "B" else f"{n:6}   B"
        n /= 1024  # type: ignore[assignment]
    return f"{n:6.1f} P"


def bar(ratio: float, width: int = BAR_WIDTH) -> Text:
    filled = round(ratio * width)
    empty = width - filled
    t = Text("[", style="dim green")
    t.append("#" * filled, style="bold green")
    t.append("." * empty, style="dim green")
    t.append("]", style="dim green")
    return t


# ---------------------------------------------------------------------------
# Confirmation modal
# ---------------------------------------------------------------------------


class ConfirmDelete(ModalScreen[bool]):
    """A simple yes / no modal."""

    DEFAULT_CSS = """
    ConfirmDelete {
        align: center middle;
    }
    ConfirmDelete > Vertical {
        background: $surface;
        border: tall $error;
        padding: 1 3;
        width: 60;
        height: auto;
    }
    ConfirmDelete Label {
        width: 100%;
        content-align: center middle;
        margin-bottom: 1;
    }
    ConfirmDelete #buttons {
        layout: horizontal;
        align: center middle;
        height: 3;
    }
    ConfirmDelete Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("y", "yes", "Yes"),
        Binding("n,escape", "no", "No"),
    ]

    def __init__(self, entry: DiskEntry) -> None:
        super().__init__()
        self.entry = entry

    def compose(self) -> ComposeResult:
        kind = "directory" if self.entry.is_dir else "file"
        yield Vertical(
            Label(f"Delete {kind}?"),
            Label(f"[bold]{self.entry.name}[/bold]  ({fmt_size(self.entry.size).strip()})"),
            Label("[dim]This cannot be undone.[/dim]"),
            Center(
                Button("Yes (y)", id="yes", variant="error"),
                Button("No (n)", id="no", variant="default"),
                id="buttons",
            ),
        )

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#yes")
    def clicked_yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def clicked_no(self) -> None:
        self.dismiss(False)


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------


class DuskApp(App):
    """pydusk — disk usage analyzer TUI."""

    TITLE = "pydusk"
    CSS = """
    Screen {
        background: $background;
    }

    #breadcrumb {
        height: 1;
        background: $panel;
        color: $text-muted;
        padding: 0 1;
        overflow: hidden;
    }

    #breadcrumb .hi {
        color: $accent;
    }

    DataTable {
        height: 1fr;
    }

    DataTable > .datatable--header {
        background: $panel;
        color: $text-muted;
        text-style: bold;
    }

    DataTable > .datatable--cursor {
        background: $accent 20%;
        color: $text;
    }

    #status {
        height: 1;
        background: $panel;
        color: $text-muted;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("up", "move_up", "Up", show=True),
        Binding("down", "move_down", "Down", show=True),
        Binding("right", "enter_dir", "Enter", show=True),
        Binding("left", "go_up", "Up dir", show=True),
        Binding("d", "delete", "Delete", show=True),
        Binding("r", "rescan", "Rescan", show=True),
        Binding("q", "quit", "Quit", show=True),
    ]

    # Navigation stack: list of DiskEntry (root at index 0)
    _stack: reactive[list[DiskEntry]] = reactive([], layout=True)
    _scanning: reactive[bool] = reactive(False)

    def __init__(self, root: Path) -> None:
        super().__init__()
        self._root_path = root

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="breadcrumb")
        yield DataTable(cursor_type="row", zebra_stripes=True)
        yield Static("Scanning…", id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("   Size", "  ", "Name", "Usage")
        self._do_scan(self._root_path)

    # ------------------------------------------------------------------
    # Scanning (runs in a worker thread so the UI stays responsive)
    # ------------------------------------------------------------------

    @work(thread=True)
    def _do_scan(self, path: Path) -> None:
        self._scanning = True
        entry = scan(path)
        self.call_from_thread(self._push_entry, entry)

    def _push_entry(self, entry: DiskEntry) -> None:
        self._stack = [entry]
        self._scanning = False
        self._refresh_table()

    # ------------------------------------------------------------------
    # Table rendering
    # ------------------------------------------------------------------

    def _current(self) -> Optional[DiskEntry]:
        return self._stack[-1] if self._stack else None

    def _refresh_table(self) -> None:
        table = self.query_one(DataTable)
        table.clear()

        current = self._current()
        if current is None:
            return

        # Breadcrumb
        crumb = self.query_one("#breadcrumb", Static)
        parts = [s.name for s in self._stack]
        crumb.update(" / ".join(parts))

        # Status
        status = self.query_one("#status", Static)
        status.update(
            f"  {len(current.children)} items  ·  total {fmt_size(current.size).strip()}  "
            f"  {'[yellow]Scanning…[/yellow]' if self._scanning else ''}"
        )

        # Parent row
        if len(self._stack) > 1:
            table.add_row(
                Text("", justify="right"),
                Text("←", style="dim"),
                Text(".."),
                Text(""),
                key="__parent__",
            )

        # Entry rows
        if not current.children:
            return

        max_size = current.children[0].size or 1

        for entry in current.children:
            ratio = entry.size / max_size
            icon = Text("/", style="bold cyan") if entry.is_dir else Text(" ", style="dim")
            name_style = "bold cyan" if entry.is_dir else "default"
            table.add_row(
                Text(fmt_size(entry.size), justify="right", style="green"),
                icon,
                Text(entry.name, style=name_style),
                bar(ratio),
                key=str(entry.path),
            )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_move_down(self) -> None:
        self.query_one(DataTable).action_scroll_cursor_down()

    def action_move_up(self) -> None:
        self.query_one(DataTable).action_scroll_cursor_up()

    def action_enter_dir(self) -> None:
        entry = self._selected_entry()
        if entry is None or not entry.is_dir:
            return

        self._enter_entry(entry)

    def action_go_up(self) -> None:
        if len(self._stack) > 1:
            # After going up, keep the highlight on the directory we came from.
            came_from = self._stack[-1]
            self._stack = self._stack[:-1]
            self._refresh_table()
            self._restore_cursor_to_row_key(str(came_from.path))

    def action_rescan(self) -> None:
        current = self._current()
        if current:
            path = current.path
            self._do_scan(path)

    def action_delete(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        self.push_screen(ConfirmDelete(entry), self._handle_delete_result)

    def _handle_delete_result(self, confirmed: bool) -> None:
        if not confirmed:
            return
        entry = self._selected_entry()
        if entry is None:
            return
        try:
            if entry.is_dir:
                shutil.rmtree(entry.path)
            else:
                entry.path.unlink()
        except OSError as exc:
            self.notify(f"Delete failed: {exc}", severity="error")
            return

        # Remove from in-memory tree and refresh
        current = self._current()
        if current:
            current.children = [c for c in current.children if c.path != entry.path]
            current.size = sum(c.size for c in current.children)
            self._refresh_table()
            self.notify(f"Deleted {entry.name}", severity="warning")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _selected_row_key(self) -> Optional[str]:
        table = self.query_one(DataTable)
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:
            return None
        return row_key.value if row_key is not None else None

    def _enter_entry(self, entry: DiskEntry) -> None:
        self._stack = self._stack + [entry]
        self._refresh_table()

    def _restore_cursor_to_row_key(self, row_key: str) -> None:
        table = self.query_one(DataTable)
        try:
            row_index = table.get_row_index(row_key)
        except Exception:
            return
        table.cursor_coordinate = Coordinate(row_index, 0)

    def _selected_entry(self) -> Optional[DiskEntry]:
        table = self.query_one(DataTable)
        try:
            cell_key = table.coordinate_to_cell_key(
                table.cursor_coordinate
            ).row_key.value
        except Exception:
            return None

        if cell_key == "__parent__" or cell_key is None:
            return None

        current = self._current()
        if current is None:
            return None

        for child in current.children:
            if str(child.path) == cell_key:
                return child
        return None

    def _entry_for_row_key(self, row_key: str) -> Optional[DiskEntry]:
        current = self._current()
        if current is None:
            return None
        for child in current.children:
            if str(child.path) == row_key:
                return child
        return None

    @on(DataTable.RowSelected)
    def _on_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Mouse (and keyboard) selection handler for the table."""
        row_key = event.row_key.value
        if row_key == "__parent__":
            self.action_go_up()
            return

        if row_key is None:
            return
        entry = self._entry_for_row_key(row_key)
        if entry is not None and entry.is_dir:
            self._enter_entry(entry)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

cli = typer.Typer(add_completion=False, help="Terminal disk usage analyzer.")


@cli.command()
def main(
    path: Path = typer.Argument(
        Path("."),
        help="Directory to analyze.",
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
    ),
) -> None:
    """Analyze disk usage for PATH (defaults to current directory)."""
    DuskApp(path).run()


if __name__ == "__main__":
    cli()