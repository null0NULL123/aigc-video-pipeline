"""
Pipeline 任务状态追踪器（进程内单例）

设计要点：
- 内存存储，重启即丢（demo 阶段不持久化）
- 每个 batch 维护一个 shots dict + pub/sub queue 列表
- SSE 客户端通过 subscribe 拿到 asyncio.Queue，每次状态变更写入队列
- 队列满时丢弃最老事件，防止内存膨胀
"""
from __future__ import annotations
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


# 终态：这些状态标 finished_at
_TERMINAL = {"done", "failed", "confirmed"}


@dataclass
class ShotState:
    """单个镜头的状态；key 由调用方决定（temp_id 或 table_id/shot_id）"""
    key: str
    status: str = "pending"     # pending/analyzing/selecting/optimizing/submitting/waiting/reviewing/done/failed/confirmed
    stage: str = ""             # 当前阶段描述（中文，给前端展示）
    video_path: str | None = None
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    # 关联回表格的字段（前端 review 时用）
    table_id: str | None = None
    shot_id: str | None = None
    scene_desc: str = ""
    duration: int = 5

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "status": self.status,
            "stage": self.stage,
            "video_path": self.video_path,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "table_id": self.table_id,
            "shot_id": self.shot_id,
            "scene_desc": self.scene_desc,
            "duration": self.duration,
        }


@dataclass
class BatchState:
    """一个 batch（一次 pipeline 运行）的整体状态"""
    batch_id: str
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    exit_code: int | None = None
    shots: dict[str, ShotState] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "shots": [s.to_dict() for s in self.shots.values()],
        }


class Tracker:
    """进程内单例"""

    def __init__(self, max_queue_size: int = 100):
        self.batches: dict[str, BatchState] = {}
        self.queues: dict[str, list[asyncio.Queue]] = {}
        self.max_queue_size = max_queue_size

    # ---- batch 维度 ----

    def ensure_batch(self, batch_id: str) -> BatchState:
        if batch_id not in self.batches:
            self.batches[batch_id] = BatchState(batch_id=batch_id)
            self.queues.setdefault(batch_id, [])
        return self.batches[batch_id]

    def get_batch(self, batch_id: str) -> BatchState | None:
        return self.batches.get(batch_id)

    def list_batches(self) -> list[str]:
        return list(self.batches.keys())

    def list_batches_with_meta(self) -> list[dict]:
        return [
            {
                "batch_id": b.batch_id,
                "started_at": b.started_at,
                "finished_at": b.finished_at,
                "exit_code": b.exit_code,
                "total": len(b.shots),
                "done": sum(1 for s in b.shots.values() if s.status == "done"),
                "failed": sum(1 for s in b.shots.values() if s.status == "failed"),
                "confirmed": sum(1 for s in b.shots.values() if s.status == "confirmed"),
            }
            for b in self.batches.values()
        ]

    # ---- shot 维度 ----

    def update_shot(self, batch_id: str, shot_key: str, **fields) -> ShotState:
        """更新 shot 字段；触发 SSE 推送"""
        batch = self.ensure_batch(batch_id)
        shot = batch.shots.get(shot_key)
        if shot is None:
            shot = ShotState(key=shot_key)
            batch.shots[shot_key] = shot

        for k, v in fields.items():
            if k in ("key",):
                continue
            setattr(shot, k, v)

        if shot.status in _TERMINAL and shot.finished_at is None:
            shot.finished_at = time.time()

        # 推 SSE
        self._publish(batch_id, {
            "type": "shot_update",
            "batch_id": batch_id,
            "shot_key": shot_key,
            "fields": fields,
        })
        return shot

    def list_shots(self, batch_id: str, status_filter: str | None = None) -> list[ShotState]:
        batch = self.batches.get(batch_id)
        if not batch:
            return []
        shots = list(batch.shots.values())
        if status_filter:
            shots = [s for s in shots if s.status == status_filter]
        # 按 key 自然排序（数字 key 按数值排）
        shots.sort(key=lambda s: (len(s.key), s.key))
        return shots

    def get_shot(self, batch_id: str, shot_key: str) -> ShotState | None:
        batch = self.batches.get(batch_id)
        if not batch:
            return None
        return batch.shots.get(shot_key)

    def mark_confirmed(self, batch_id: str, shot_key: str) -> None:
        self.update_shot(batch_id, shot_key, status="confirmed")

    def mark_unconfirmed(self, batch_id: str, shot_key: str) -> None:
        """撤回 confirm：回到 done（如果之前是 done）或当前 status"""
        shot = self.get_shot(batch_id, shot_key)
        if shot and shot.status == "confirmed":
            shot.status = "done"
            shot.finished_at = shot.finished_at  # 保留原 finished_at
            self._publish(batch_id, {
                "type": "shot_update",
                "batch_id": batch_id,
                "shot_key": shot_key,
                "fields": {"status": "done"},
            })

    def mark_done(self, batch_id: str, exit_code: int | None = 0) -> None:
        """标记整个 batch 结束"""
        batch = self.batches.get(batch_id)
        if batch:
            batch.finished_at = time.time()
            batch.exit_code = exit_code
        self._publish(batch_id, {
            "type": "batch_done",
            "batch_id": batch_id,
            "exit_code": exit_code,
        })

    # ---- pub/sub ----

    def subscribe(self, batch_id: str) -> asyncio.Queue:
        """注册订阅。返回 queue，调用方通过 get() 读取事件"""
        self.ensure_batch(batch_id)
        q: asyncio.Queue = asyncio.Queue(maxsize=self.max_queue_size)
        self.queues[batch_id].append(q)
        return q

    def unsubscribe(self, batch_id: str, queue: asyncio.Queue) -> None:
        subs = self.queues.get(batch_id, [])
        if queue in subs:
            subs.remove(queue)

    def _publish(self, batch_id: str, event: dict) -> None:
        """推送到该 batch 的所有订阅者；满队列丢最老"""
        for q in self.queues.get(batch_id, []):
            self._put_nowait(q, event)

    @staticmethod
    def _put_nowait(q: asyncio.Queue, event: dict) -> None:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # 满队列丢最老：先 get 一条再 put
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def reset(self) -> None:
        """测试 / reload 用：清空所有状态"""
        self.batches.clear()
        self.queues.clear()


_singleton: Tracker | None = None


def get_tracker() -> Tracker:
    """进程内单例"""
    global _singleton
    if _singleton is None:
        _singleton = Tracker()
    return _singleton


def reset_tracker() -> None:
    """测试用"""
    global _singleton
    _singleton = None