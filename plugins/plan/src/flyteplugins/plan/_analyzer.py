"""Static analysis of Python files to extract Flyte task call graphs.

Parses Python source using the AST to find:
- All functions decorated with ``@env.task``
- Which tasks call which other tasks (direct calls and via ``flyte.map``)
- Source code and line ranges for each task
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set, Tuple


@dataclass
class TaskInfo:
    """Metadata about a single Flyte task discovered via AST analysis."""

    name: str
    """Function name (e.g. ``my_task``)."""

    env_name: str
    """Name of the variable used as the task environment decorator (e.g. ``env``)."""

    source: str
    """Full source text of the function (including decorator lines)."""

    lineno: int
    """1-based start line of the function definition in the file."""

    end_lineno: int
    """1-based end line of the function definition."""

    calls: List[str] = field(default_factory=list)
    """Names of other tasks called directly (``await other_task(...)`` or ``other_task(...)``)."""

    map_calls: List[str] = field(default_factory=list)
    """Names of other tasks called via ``flyte.map`` / ``flyte.map.aio``."""

    @property
    def all_callees(self) -> List[str]:
        return self.calls + self.map_calls


@dataclass
class PlanGraph:
    """The full call-graph for a file."""

    file_path: str
    source_lines: List[str]
    tasks: Dict[str, TaskInfo] = field(default_factory=dict)

    @property
    def root_tasks(self) -> List[str]:
        """Tasks not referenced from any other task — the entry points."""
        all_callees: Set[str] = set()
        for t in self.tasks.values():
            all_callees.update(t.all_callees)
        return [name for name in self.tasks if name not in all_callees]


def analyze_file(file_path: str) -> PlanGraph:
    """Parse *file_path* and return a ``PlanGraph``."""
    path = Path(file_path)
    source = path.read_text()
    source_lines = source.splitlines()
    tree = ast.parse(source, filename=file_path)

    # Step 1: find all TaskEnvironment variable names
    env_vars = _find_env_vars(tree)

    # Step 2: find all @env.task decorated functions
    tasks: Dict[str, TaskInfo] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            env_name = _get_task_decorator_env(node, env_vars)
            if env_name is not None:
                # Include decorator lines
                start = node.lineno
                if node.decorator_list:
                    start = min(d.lineno for d in node.decorator_list)
                end = node.end_lineno or node.lineno
                func_source = "\n".join(source_lines[start - 1 : end])
                tasks[node.name] = TaskInfo(
                    name=node.name,
                    env_name=env_name,
                    source=func_source,
                    lineno=start,
                    end_lineno=end,
                )

    # Step 3: for each task, find calls to other tasks
    task_names = set(tasks.keys())
    for task_info in tasks.values():
        func_node = _find_func_node(tree, task_info.name)
        if func_node is None:
            continue
        direct, mapped = _find_task_calls(func_node, task_names)
        task_info.calls = direct
        task_info.map_calls = mapped

    return PlanGraph(file_path=file_path, source_lines=source_lines, tasks=tasks)


def _find_env_vars(tree: ast.Module) -> Set[str]:
    """Find variable names assigned to ``flyte.TaskEnvironment(...)``."""
    env_vars: Set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            if _is_task_env_call(node.value):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        env_vars.add(target.id)
    return env_vars


def _is_task_env_call(node: ast.expr) -> bool:
    """Check if *node* is a call to ``flyte.TaskEnvironment(...)`` or ``TaskEnvironment(...)``."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "TaskEnvironment":
        return True
    if isinstance(func, ast.Name) and func.id == "TaskEnvironment":
        return True
    return False


def _get_task_decorator_env(
    node: ast.FunctionDef | ast.AsyncFunctionDef, env_vars: Set[str]
) -> str | None:
    """If *node* has an ``@env.task`` decorator where *env* is in *env_vars*, return the env var name."""
    for dec in node.decorator_list:
        # @env.task
        if isinstance(dec, ast.Attribute) and dec.attr == "task":
            if isinstance(dec.value, ast.Name) and dec.value.id in env_vars:
                return dec.value.id
        # @env.task(...)  — call form
        if isinstance(dec, ast.Call):
            func = dec.func
            if isinstance(func, ast.Attribute) and func.attr == "task":
                if isinstance(func.value, ast.Name) and func.value.id in env_vars:
                    return func.value.id
    return None


def _find_func_node(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Find the function definition node with the given *name*."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _find_task_calls(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    task_names: Set[str],
) -> Tuple[List[str], List[str]]:
    """Walk *func_node* and find direct task calls and map task calls.

    Returns (direct_calls, map_calls) — each is a list of task function names.
    """
    direct: List[str] = []
    mapped: List[str] = []
    seen_direct: Set[str] = set()
    seen_mapped: Set[str] = set()

    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call):
            continue

        # Direct call: ``my_task(...)`` or ``await my_task(...)``
        if isinstance(node.func, ast.Name) and node.func.id in task_names:
            if node.func.id not in seen_direct:
                direct.append(node.func.id)
                seen_direct.add(node.func.id)
            continue

        # flyte.map(task_name, ...) or flyte.map.aio(task_name, ...)
        if _is_map_call(node):
            if node.args:
                first_arg = node.args[0]
                if isinstance(first_arg, ast.Name) and first_arg.id in task_names:
                    if first_arg.id not in seen_mapped:
                        mapped.append(first_arg.id)
                        seen_mapped.add(first_arg.id)
            continue

    return direct, mapped


def _is_map_call(node: ast.Call) -> bool:
    """Check if *node* is ``flyte.map(...)``, ``flyte.map.aio(...)``."""
    func = node.func
    # flyte.map(...)
    if isinstance(func, ast.Attribute) and func.attr == "map":
        if isinstance(func.value, ast.Name) and func.value.id == "flyte":
            return True
    # flyte.map.aio(...)
    if isinstance(func, ast.Attribute) and func.attr == "aio":
        if isinstance(func.value, ast.Attribute) and func.value.attr == "map":
            if isinstance(func.value.value, ast.Name) and func.value.value.id == "flyte":
                return True
    return False
