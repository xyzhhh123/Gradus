"""任务图：节点、依赖、认领与解锁。

本模块只有纯逻辑：建节点、连边、判断能不能开始、认领、完成并解锁下游，
以及读写 `<workspace>/.gradus/tasks.json`。它不碰 asyncio、不认识 `Agent`——
"谁来跑这个节点"由上层决定。

状态机（与 `learn-claude-code` s10 一致）：

    add            claim              complete
     ↓              ↓                   ↓
   pending ──────→ in_progress ──────→ completed
     │
     └── remove（删除节点，并把它从其它节点的依赖里摘掉）

`blocked_by` 是唯一的边：任务只能在它的前驱**全部 completed** 之后被认领。
写入是整表重写 + 原子替换，失败时回滚内存改动。
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

TASK_PREFIX = "task_"
STATUS_PENDING = "pending"
STATUS_RUNNING = "in_progress"
STATUS_COMPLETED = "completed"
STATUSES = (STATUS_PENDING, STATUS_RUNNING, STATUS_COMPLETED)


class TaskError(ValueError):
    """任务定义或状态流转不合法，或落盘失败。"""


@dataclass
class Task:
    """任务图上的一个节点。"""

    id: str
    subject: str
    description: str = ""
    status: str = STATUS_PENDING
    owner: str | None = None
    blocked_by: tuple[str, ...] = ()
    role: str | None = None
    model: str | None = None
    result: str | None = None

    @property
    def done(self) -> bool:
        return self.status == STATUS_COMPLETED


class TaskGraph:
    """任务表。`path=None` 表示只在内存里，不落盘。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._tasks: dict[str, Task] = {}
        self._warnings: list[str] = []
        if self._path is not None:
            self._load()

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def warnings(self) -> tuple[str, ...]:
        """加载时被跳过的条目、被恢复的中断任务等非致命问题。"""
        return tuple(self._warnings)

    # ---- 增删查 ----

    def add(
        self,
        subject: str,
        description: str = "",
        *,
        role: str | None = None,
        model: str | None = None,
        blocked_by: Sequence[str] = (),
    ) -> Task:
        text = subject.strip()
        if not text:
            raise TaskError("subject cannot be empty")

        # 新节点还没有下游，所以这里的依赖不可能成环，只需确认它们都已存在。
        edges: list[str] = []
        for dependency in _ids(blocked_by):
            if dependency not in self._tasks:
                raise TaskError(f"unknown task: {dependency}")
            if dependency not in edges:
                edges.append(dependency)

        task = Task(
            id=f"{TASK_PREFIX}{uuid4().hex[:8]}",
            subject=text,
            description=description.strip(),
            role=_optional_text(role),
            model=_optional_text(model),
            blocked_by=tuple(edges),
        )
        snapshot = copy.deepcopy(self._tasks)
        self._tasks[task.id] = task
        self._save(snapshot)
        return task

    def update_dependencies(
        self,
        task_id: str,
        *,
        add: tuple[str, ...] = (),
        remove: tuple[str, ...] = (),
    ) -> Task:
        """连边或摘边；只有还无人认领的任务可以改。"""
        task = self._require(task_id)
        if task.status != STATUS_PENDING or task.owner is not None:
            raise TaskError(f"task {task_id} is {task.status}, cannot change dependencies")

        edges = list(task.blocked_by)
        for dependency in _ids(add):
            if dependency == task_id:
                raise TaskError("a task cannot depend on itself")
            if dependency not in self._tasks:
                raise TaskError(f"unknown task: {dependency}")
            if dependency in edges:
                continue
            if self._depends_on(dependency, task_id):
                raise TaskError(f"cycle: {dependency} already depends on {task_id}")
            edges.append(dependency)

        for dependency in _ids(remove):
            if dependency in edges:
                edges.remove(dependency)

        snapshot = copy.deepcopy(self._tasks)
        task.blocked_by = tuple(edges)
        self._save(snapshot)
        return task

    def remove(self, task_id: str) -> bool:
        """删掉一个节点，并把它从其它节点的依赖里摘掉。"""
        task = self._tasks.get(task_id)
        if task is None:
            return False
        if task.status == STATUS_RUNNING:
            raise TaskError(f"task {task_id} is running, cannot remove")

        snapshot = copy.deepcopy(self._tasks)
        self._tasks.pop(task_id, None)
        for other in self._tasks.values():
            if task_id in other.blocked_by:
                other.blocked_by = tuple(dep for dep in other.blocked_by if dep != task_id)
        self._save(snapshot)
        return True

    def tasks(self) -> tuple[Task, ...]:
        return tuple(self._tasks.values())

    def get(self, task_id: str) -> Task:
        return self._require(task_id)

    # ---- 依赖与就绪 ----

    def blockers(self, task_id: str) -> tuple[str, ...]:
        """还没完成的前驱；依赖指向不存在的节点也算阻塞。"""
        task = self._require(task_id)
        pending: list[str] = []
        for dependency in task.blocked_by:
            other = self._tasks.get(dependency)
            if other is None or not other.done:
                pending.append(dependency)
        return tuple(pending)

    def can_start(self, task_id: str) -> bool:
        task = self._require(task_id)
        return task.status == STATUS_PENDING and task.owner is None and not self.blockers(task_id)

    def ready(self) -> tuple[Task, ...]:
        """当前可以被认领的全部节点；可能 0 个、1 个或多个。"""
        return tuple(
            task
            for task in self._tasks.values()
            if task.status == STATUS_PENDING and task.owner is None and not self.blockers(task.id)
        )

    @property
    def finished(self) -> bool:
        return all(task.done for task in self._tasks.values())

    # ---- 状态流转 ----

    def claim(self, task_id: str, owner: str) -> Task:
        """认领：pending → in_progress。同一个 owner 是否只能有一个任务由上层保证。"""
        task = self._require(task_id)
        who = owner.strip()
        if not who:
            raise TaskError("owner cannot be empty")
        if task.status != STATUS_PENDING:
            raise TaskError(f"task {task_id} is {task.status}, cannot claim")
        if task.owner is not None:
            raise TaskError(f"task {task_id} is owned by {task.owner}")

        blockers = self.blockers(task_id)
        if blockers:
            raise TaskError(f"task {task_id} is blocked by {', '.join(blockers)}")

        snapshot = copy.deepcopy(self._tasks)
        task.status = STATUS_RUNNING
        task.owner = who
        self._save(snapshot)
        return task

    def complete(
        self, task_id: str, owner: str, result: str = ""
    ) -> tuple[Task, tuple[Task, ...]]:
        """完成并返回**刚刚**被解锁的下游节点。"""
        task = self._require(task_id)
        if task.status != STATUS_RUNNING:
            raise TaskError(f"task {task_id} is {task.status}, cannot complete")
        if task.owner != owner:
            raise TaskError(f"task {task_id} is owned by {task.owner}, not {owner}")

        before = {item.id for item in self.ready()}
        snapshot = copy.deepcopy(self._tasks)
        task.status = STATUS_COMPLETED
        task.result = result.strip() or None
        unblocked = tuple(item for item in self.ready() if item.id not in before)
        self._save(snapshot)
        return task, unblocked

    def release(self, task_id: str) -> bool:
        """把进行中的任务放回 pending：执行者崩了、又不想直接判它完成。"""
        task = self._tasks.get(task_id)
        if task is None or task.status != STATUS_RUNNING:
            return False

        snapshot = copy.deepcopy(self._tasks)
        task.status = STATUS_PENDING
        task.owner = None
        self._save(snapshot)
        return True

    # ---- 内部 ----

    def _require(self, task_id: str) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskError(f"unknown task: {task_id}")
        return task

    def _depends_on(self, task_id: str, target: str) -> bool:
        """`task_id` 是否（间接）依赖 `target`。"""
        seen: set[str] = set()
        stack = [task_id]
        while stack:
            current = stack.pop()
            if current == target:
                return True
            if current in seen:
                continue
            seen.add(current)
            node = self._tasks.get(current)
            if node is not None:
                stack.extend(node.blocked_by)
        return False

    def _save(self, snapshot: dict[str, Task]) -> None:
        try:
            self._write()
        except OSError as exc:
            self._tasks = snapshot
            raise TaskError(f"cannot save tasks: {exc}") from exc

    def _write(self) -> None:
        if self._path is None:
            return

        payload = [asdict(task) for task in self._tasks.values()]
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, self._path)
        finally:
            temporary.unlink(missing_ok=True)

    def _load(self) -> None:
        assert self._path is not None
        if not self._path.exists():
            return

        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                raise ValueError("expected a JSON list")
        except (OSError, ValueError) as exc:
            self._warnings.append(f"cannot load {self._path.name}: {exc}")
            return

        recovered = 0
        for item in payload:
            try:
                task = _task_from_payload(item)
            except TaskError as exc:
                self._warnings.append(f"skipped saved task: {exc}")
                continue
            if task.status == STATUS_RUNNING:
                # 进程重启后没有 agent 还记得这个任务，直接放回 pending 才不会永久卡住。
                task.status = STATUS_PENDING
                task.owner = None
                recovered += 1
            self._tasks[task.id] = task

        if recovered:
            self._warnings.append(f"released {recovered} interrupted task(s)")
        for task in self._tasks.values():
            for dependency in task.blocked_by:
                if dependency not in self._tasks:
                    self._warnings.append(
                        f"task {task.id} depends on missing task {dependency}"
                    )


def _task_from_payload(item: object) -> Task:
    if not isinstance(item, dict):
        raise TaskError("task entry must be an object")

    task_id = item.get("id")
    subject = item.get("subject")
    status = item.get("status", STATUS_PENDING)
    if not isinstance(task_id, str) or not task_id.startswith(TASK_PREFIX):
        raise TaskError("invalid task id")
    if not isinstance(subject, str) or not subject.strip():
        raise TaskError(f"{task_id}: subject cannot be empty")
    if status not in STATUSES:
        raise TaskError(f"{task_id}: invalid status {status!r}")

    owner = item.get("owner")
    blocked_by = item.get("blocked_by", [])
    if owner is not None and not isinstance(owner, str):
        raise TaskError(f"{task_id}: owner must be a string")
    if not isinstance(blocked_by, list) or not all(
        isinstance(dep, str) for dep in blocked_by
    ):
        raise TaskError(f"{task_id}: blocked_by must be a list of ids")

    return Task(
        id=task_id,
        subject=subject.strip(),
        description=str(item.get("description") or ""),
        status=status,
        owner=owner,
        blocked_by=tuple(blocked_by),
        role=_optional_text(item.get("role")),
        model=_optional_text(item.get("model")),
        result=item.get("result") if isinstance(item.get("result"), str) else None,
    )


def _ids(values: tuple[str, ...]) -> list[str]:
    return [value.strip() for value in values if value.strip()]


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
