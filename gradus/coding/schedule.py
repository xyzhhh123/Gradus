"""定时任务：cron 表达式、任务表与持久化。

本模块只有纯逻辑：解析表达式、判断某个时刻是否该触发、维护任务状态、
读写 `<workspace>/.gradus/schedules.json`。它不碰 asyncio、不启动对话、
不认识 `Session`——"到点之后谁来跑"由上层决定。

状态机（与 `learn-claude-code` s12 一致）：

    add            due()             ack()
     ↓              ↓                 ↓
   pending=False → pending=True   → pending=False（周期任务）/ 删除（一次性）
                        └── fail() ──┘ 超过重试上限后放弃到下一次匹配

`due()` 在**标记之后**才落盘，所以进程在"到点"和"跑完"之间崩溃时，
重启后仍能看到这条任务处于待交付状态。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
FIELD_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")
MINUTE_FORMAT = "%Y-%m-%d %H:%M"

DEFAULT_MAX_ATTEMPTS = 3


class ScheduleError(ValueError):
    """cron 表达式或任务定义不合法，或落盘失败。"""


@dataclass(frozen=True)
class CronExpression:
    """五段式 cron 表达式的解析结果。

    支持 `*`、`*/N`、`N`、`a-b`、`N,M,...`；不支持名称、`@daily` 别名、
    `a-b/N` 这类扩展写法。星期以 0 为周日（标准 cron），与
    `datetime.weekday()`（周一为 0）差一位。
    """

    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    day_any: bool
    weekday_any: bool

    @classmethod
    def parse(cls, text: str) -> "CronExpression":
        fields = text.strip().split()
        if len(fields) != 5:
            raise ScheduleError(f"expected 5 fields, got {len(fields)}")

        parsed: list[frozenset[int]] = []
        for name, field, (low, high) in zip(FIELD_NAMES, fields, FIELD_RANGES):
            try:
                parsed.append(_parse_field(field, low, high))
            except ScheduleError as exc:
                raise ScheduleError(f"{name}: {exc}") from exc

        return cls(
            minutes=parsed[0],
            hours=parsed[1],
            days=parsed[2],
            months=parsed[3],
            weekdays=parsed[4],
            day_any=fields[2] == "*",
            weekday_any=fields[4] == "*",
        )

    def matches(self, moment: datetime) -> bool:
        if moment.minute not in self.minutes:
            return False
        if moment.hour not in self.hours:
            return False
        if moment.month not in self.months:
            return False

        day_matches = moment.day in self.days
        weekday_matches = (moment.weekday() + 1) % 7 in self.weekdays

        # 标准 cron：日与星期都被限定时取「或」，任一为 * 时只看另一个。
        if self.day_any and self.weekday_any:
            return True
        if self.day_any:
            return weekday_matches
        if self.weekday_any:
            return day_matches
        return day_matches or weekday_matches


def _parse_field(field: str, low: int, high: int) -> frozenset[int]:
    if field == "*":
        return frozenset(range(low, high + 1))

    if field.startswith("*/"):
        step = field[2:]
        if not step.isdigit() or int(step) <= 0:
            raise ScheduleError(f"invalid step: {field}")
        return frozenset(range(low, high + 1, int(step)))

    if "," in field:
        values: set[int] = set()
        for part in field.split(","):
            values |= _parse_field(part.strip(), low, high)
        return frozenset(values)

    if "-" in field:
        start_text, end_text = field.split("-", 1)
        if not start_text.isdigit() or not end_text.isdigit():
            raise ScheduleError(f"invalid range: {field}")
        start, end = int(start_text), int(end_text)
        if start > end:
            raise ScheduleError(f"range start is greater than end: {field}")
        if start < low or end > high:
            raise ScheduleError(f"range {field} is outside [{low}-{high}]")
        return frozenset(range(start, end + 1))

    if not field.isdigit():
        raise ScheduleError(f"invalid field: {field}")
    value = int(field)
    if value < low or value > high:
        raise ScheduleError(f"value {value} is outside [{low}-{high}]")
    return frozenset({value})


@dataclass
class ScheduledJob:
    """一条定时任务：什么时候跑、跑什么、跑到哪一步了。"""

    id: str
    cron: str
    prompt: str
    recurring: bool = True
    durable: bool = True
    pending_delivery: bool = False
    last_fired: str | None = None
    attempts: int = 0
    last_error: str | None = None


class Schedule:
    """任务表。`path=None` 表示只在内存里，不落盘。"""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._path = Path(path) if path is not None else None
        self._max_attempts = max(1, max_attempts)
        self._jobs: dict[str, ScheduledJob] = {}
        self._expressions: dict[str, CronExpression] = {}
        self._warnings: list[str] = []
        if self._path is not None:
            self._load()

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def warnings(self) -> tuple[str, ...]:
        """加载时被跳过的条目等非致命问题。"""
        return tuple(self._warnings)

    # ---- 增删查 ----

    def add(
        self,
        cron: str,
        prompt: str,
        *,
        recurring: bool = True,
        durable: bool = True,
    ) -> ScheduledJob:
        expression = CronExpression.parse(cron)
        text = prompt.strip()
        if not text:
            raise ScheduleError("prompt cannot be empty")

        self._expressions[cron] = expression
        job = ScheduledJob(
            id=f"job_{uuid4().hex[:8]}",
            cron=cron,
            prompt=text,
            recurring=recurring,
            durable=durable,
        )
        self._jobs[job.id] = job
        try:
            self._write()
        except OSError as exc:
            self._jobs.pop(job.id, None)
            raise ScheduleError(f"cannot save schedule: {exc}") from exc
        return job

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.pop(job_id, None)
        if job is None:
            return False
        try:
            self._write()
        except OSError as exc:
            self._jobs[job_id] = job
            raise ScheduleError(f"cannot save schedule: {exc}") from exc
        return True

    def jobs(self) -> tuple[ScheduledJob, ...]:
        return tuple(self._jobs.values())

    def pending(self) -> tuple[ScheduledJob, ...]:
        return tuple(job for job in self._jobs.values() if job.pending_delivery)

    # ---- 状态流转 ----

    def due(self, moment: datetime | None = None) -> tuple[ScheduledJob, ...]:
        """标出这一分钟该触发的任务；同一分钟不会重复标记。"""
        now = moment or datetime.now()
        marker = now.strftime(MINUTE_FORMAT)
        fired: list[ScheduledJob] = []

        for job in self._jobs.values():
            if job.pending_delivery or job.last_fired == marker:
                continue
            try:
                expression = self._expression_for(job.cron)
            except ScheduleError:
                continue
            if not expression.matches(now):
                continue

            job.pending_delivery = True
            job.last_fired = marker
            job.attempts = 0
            fired.append(job)

        if fired:
            self._write_quiet()
        return tuple(fired)

    def ack(self, job_id: str) -> None:
        """模型已经接收：周期任务清待交付，一次性任务删除。"""
        job = self._jobs.get(job_id)
        if job is None:
            return

        job.attempts = 0
        job.last_error = None
        if job.recurring:
            job.pending_delivery = False
        else:
            self._jobs.pop(job_id, None)
        self._write_quiet()

    def fail(self, job_id: str, reason: str) -> None:
        """这一轮没跑成：留待重试，超过上限就放弃到下一次匹配。"""
        job = self._jobs.get(job_id)
        if job is None:
            return

        job.attempts += 1
        job.last_error = reason
        if job.attempts >= self._max_attempts:
            if job.recurring:
                job.pending_delivery = False
            else:
                self._jobs.pop(job_id, None)
        self._write_quiet()

    # ---- 持久化 ----

    def _expression_for(self, cron: str) -> CronExpression:
        cached = self._expressions.get(cron)
        if cached is None:
            cached = CronExpression.parse(cron)
            self._expressions[cron] = cached
        return cached

    def _write_quiet(self) -> None:
        try:
            self._write()
        except OSError as exc:
            self._warnings.append(f"cannot save schedule: {exc}")

    def _write(self) -> None:
        if self._path is None:
            return

        payload = [
            asdict(job) for job in self._jobs.values() if job.durable
        ]
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(
            f"{self._path.name}.{os.getpid()}.tmp"
        )
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

        for item in payload:
            try:
                job = _job_from_payload(item)
            except ScheduleError as exc:
                self._warnings.append(f"skipped saved job: {exc}")
                continue
            self._jobs[job.id] = job


def _job_from_payload(item: object) -> ScheduledJob:
    if not isinstance(item, dict):
        raise ScheduleError("job entry must be an object")

    job_id = item.get("id")
    cron = item.get("cron")
    prompt = item.get("prompt")
    if not isinstance(job_id, str) or not job_id.startswith("job_"):
        raise ScheduleError("invalid job id")
    if not isinstance(cron, str):
        raise ScheduleError("cron must be a string")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ScheduleError("prompt cannot be empty")

    CronExpression.parse(cron)

    return ScheduledJob(
        id=job_id,
        cron=cron,
        prompt=prompt,
        recurring=bool(item.get("recurring", True)),
        durable=bool(item.get("durable", True)),
        pending_delivery=bool(item.get("pending_delivery", False)),
        last_fired=item.get("last_fired")
        if isinstance(item.get("last_fired"), str)
        else None,
        attempts=int(item.get("attempts", 0) or 0),
        last_error=item.get("last_error")
        if isinstance(item.get("last_error"), str)
        else None,
    )
