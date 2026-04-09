"""TUI application for ``flyte plan`` — static task call-graph explorer.

Left panel: nested tree of tasks (roots at top, expandable children).
Right panel: syntax-highlighted source code for the selected task,
with highlighted references to called tasks.
"""

from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Footer, Header, Static, Tree
from textual.widgets.tree import TreeNode

from ._analyzer import PlanGraph, TaskInfo

# ── Symbols ──────────────────────────────────────────────────────────
_TASK_ICON = "◆"
_MAP_ICON = "⫘"  # parallel / map call

# ── Flyte brand palette (matches core TUI) ───────────────────────────
_FLYTE_PURPLE = "#7652a2"
_FLYTE_PURPLE_LIGHT = "#f7f5fd"
_FLYTE_PURPLE_DARK = "#171020"


def _task_label(name: str, is_map: bool = False) -> Text:
    label = Text()
    if is_map:
        label.append(f"{_MAP_ICON} ", style="yellow")
    else:
        label.append(f"{_TASK_ICON} ", style="cyan")
    label.append(name)
    return label


def _root_label(name: str) -> Text:
    label = Text()
    label.append(f"{_TASK_ICON} ", style="bold green")
    label.append(name, style="bold")
    return label


# ── Left panel: task call-graph tree ─────────────────────────────────


class PlanTreeWidget(Tree[str]):
    """Navigable tree showing the task call hierarchy.

    Root-level entries are tasks that are never called by another task.
    Children are the tasks they call; map-calls get a different icon.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("down,j", "cursor_down", "Cursor Down", show=False),
        Binding("up,k", "cursor_up", "Cursor Up", show=False),
    ]

    def __init__(self, graph: PlanGraph, **kwargs: Any) -> None:
        super().__init__("Tasks", **kwargs)
        self.show_root = False
        self._graph = graph
        self._name_to_nodes: dict[str, list[TreeNode[str]]] = {}

    def on_mount(self) -> None:
        self.root.expand()
        self._build_tree()

    def _build_tree(self) -> None:
        roots = self._graph.root_tasks
        if not roots:
            roots = list(self._graph.tasks.keys())
        for name in roots:
            self._add_task_node(name, self.root, is_map=False, visited=set())

    def _add_task_node(
        self,
        name: str,
        parent: TreeNode[str],
        is_map: bool,
        visited: set[str],
    ) -> None:
        task = self._graph.tasks.get(name)
        if task is None:
            return

        if parent is self.root:
            label = _root_label(name)
        else:
            label = _task_label(name, is_map=is_map)

        node = parent.add(label, data=name, expand=False)
        self._name_to_nodes.setdefault(name, []).append(node)

        if name in visited:
            return
        visited = visited | {name}

        for callee in task.calls:
            self._add_task_node(callee, node, is_map=False, visited=visited)
        for callee in task.map_calls:
            self._add_task_node(callee, node, is_map=True, visited=visited)

    def select_task(self, name: str) -> None:
        """Programmatically select and reveal a task node in the tree."""
        nodes = self._name_to_nodes.get(name, [])
        if not nodes:
            return
        node = nodes[0]
        parent = node.parent
        while parent is not None:
            parent.expand()
            parent = parent.parent
        self.select_node(node)
        node.expand()


# ── Right panel widgets ──────────────────────────────────────────────


class CodeBox(Static):
    """Box for displaying source code."""

    def __init__(self, *args: Any, **kwargs: Any):
        kwargs.setdefault("markup", False)
        super().__init__(*args, **kwargs)


class InfoBox(Static):
    """Box for displaying task metadata."""

    def __init__(self, *args: Any, **kwargs: Any):
        kwargs.setdefault("markup", False)
        super().__init__(*args, **kwargs)


# ── Source highlighting helpers ──────────────────────────────────────


def _highlight_source(graph: PlanGraph, task: TaskInfo) -> Text:
    """Build Rich Text with line numbers and highlighted task references."""
    lines = task.source.splitlines()
    text = Text()
    all_callees = set(task.all_callees)
    task_names = set(graph.tasks.keys())
    highlight_names = all_callees & task_names

    for i, line in enumerate(lines):
        line_num = task.lineno + i
        text.append(f"{line_num:4d} ", style="dim")

        if not highlight_names:
            text.append(line)
        else:
            _append_highlighted_line(text, line, highlight_names, task)

        if i < len(lines) - 1:
            text.append("\n")

    return text


def _append_highlighted_line(
    text: Text, line: str, highlight_names: set[str], task: TaskInfo
) -> None:
    """Append a single line with task references highlighted."""
    pos = 0
    while pos < len(line):
        best_idx = len(line)
        best_name = None
        for callee in highlight_names:
            idx = line.find(callee, pos)
            if idx >= 0 and idx < best_idx:
                best_idx = idx
                best_name = callee
        if best_name is None:
            text.append(line[pos:])
            break
        if best_idx > pos:
            text.append(line[pos:best_idx])
        is_map = best_name in task.map_calls
        style = "bold yellow underline" if is_map else "bold cyan underline"
        text.append(best_name, style=style)
        pos = best_idx + len(best_name)


# ── Main app ─────────────────────────────────────────────────────────


class PlanTUIApp(App[None]):
    """Interactive TUI for ``flyte plan <file.py>``."""

    CSS = f"""
    Screen {{
        background: {_FLYTE_PURPLE_DARK};
    }}
    Header {{
        background: {_FLYTE_PURPLE};
        color: {_FLYTE_PURPLE_LIGHT};
    }}
    Footer {{
        background: {_FLYTE_PURPLE};
        color: {_FLYTE_PURPLE_LIGHT};
    }}
    Horizontal {{
        height: 1fr;
    }}
    PlanTreeWidget {{
        width: 1fr;
        min-width: 30;
        border: solid {_FLYTE_PURPLE};
        border-title-color: {_FLYTE_PURPLE_LIGHT};
        background: {_FLYTE_PURPLE_DARK};
        color: {_FLYTE_PURPLE_LIGHT};
    }}
    #code-scroll {{
        width: 2fr;
        background: {_FLYTE_PURPLE_DARK};
    }}
    CodeBox {{
        border: solid {_FLYTE_PURPLE};
        border-title-color: {_FLYTE_PURPLE_LIGHT};
        padding: 0 1;
        margin-bottom: 1;
        height: auto;
        min-height: 5;
        color: {_FLYTE_PURPLE_LIGHT};
    }}
    InfoBox {{
        border: solid {_FLYTE_PURPLE};
        border-title-color: {_FLYTE_PURPLE_LIGHT};
        padding: 0 1;
        height: auto;
        color: {_FLYTE_PURPLE_LIGHT};
    }}
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, graph: PlanGraph, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._graph = graph

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            tree = PlanTreeWidget(self._graph, id="plan-tree")
            tree.border_title = "Task Graph"
            yield tree
            with VerticalScroll(id="code-scroll"):
                yield CodeBox(id="code-box")
                yield InfoBox(id="info-box")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Flyte Plan"
        self.sub_title = self._graph.file_path
        self._update_code_panel(None)

    def on_tree_node_selected(self, event: Tree.NodeSelected[str]) -> None:
        task_name = event.node.data
        if task_name and task_name in self._graph.tasks:
            self._update_code_panel(task_name)

    def _update_code_panel(self, name: str | None) -> None:
        """Update the code and info boxes for the selected task."""
        try:
            code_box = self.query_one("#code-box", CodeBox)
            info_box = self.query_one("#info-box", InfoBox)
        except Exception:
            return

        if name is None:
            code_box.update(Text("Select a task from the tree to view its source.", style="dim"))
            code_box.border_title = "Source Code"
            info_box.display = False
            return

        task = self._graph.tasks.get(name)
        if task is None:
            code_box.update(Text(f"Task '{name}' not found.", style="red"))
            code_box.border_title = "Source Code"
            info_box.display = False
            return

        code_box.border_title = f"Source: {name}  (lines {task.lineno}-{task.end_lineno})"
        code_box.update(_highlight_source(self._graph, task))

        info_parts: list[str] = []
        info_parts.append(f"Environment: @{task.env_name}.task")
        if task.calls:
            info_parts.append(f"Calls: {', '.join(task.calls)}")
        if task.map_calls:
            info_parts.append(f"Map calls ({_MAP_ICON} parallel): {', '.join(task.map_calls)}")
        callers = [t.name for t in self._graph.tasks.values() if name in t.all_callees]
        if callers:
            info_parts.append(f"Called by: {', '.join(callers)}")
        else:
            info_parts.append("Entry point (not called by other tasks)")

        info_box.border_title = "Task Info"
        info_box.update("\n".join(info_parts))
        info_box.display = True

    def action_navigate_to_task(self, name: str) -> None:
        """Navigate to a specific task in both tree and code viewer."""
        tree = self.query_one("#plan-tree", PlanTreeWidget)
        tree.select_task(name)
        self._update_code_panel(name)
