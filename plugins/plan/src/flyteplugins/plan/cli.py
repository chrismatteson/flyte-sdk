"""CLI entry point for ``flyte plan <file.py>``."""

from __future__ import annotations

import rich_click as click


@click.command()
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
def plan(file: str) -> None:
    """Explore the task call-graph of a Flyte Python file in an interactive TUI.

    \b
    Usage:
        flyte plan file.py

    The TUI shows a tree of all @env.task functions on the left.
    Root entries are tasks not called by any other task.
    Click a task to view its source code on the right.
    Tasks called via flyte.map are marked with a parallel symbol.
    """
    try:
        from textual.app import App  # noqa: F401 — availability check
    except ImportError:
        raise click.ClickException(
            "The plan TUI requires the 'textual' package. Install it with:  pip install flyte[tui]"
        )

    from ._analyzer import analyze_file
    from ._app import PlanTUIApp

    graph = analyze_file(file)
    if not graph.tasks:
        raise click.ClickException(f"No Flyte tasks found in {file}")

    app = PlanTUIApp(graph=graph)
    app.run()
