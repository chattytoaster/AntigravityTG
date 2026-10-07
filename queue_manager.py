import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple, Any, Callable, Coroutine

logger = logging.getLogger(__name__)

@dataclass
class QueueItem:
    item_id: str
    message: Any  # aiogram.types.Message
    prompt: str
    user_id: int
    chat_id: int
    thread_id: Optional[int]
    session_key: str
    enqueued_at: float
    notify_msg: Optional[Any] = None  # Message object for "Added to queue" notification

class SessionQueue:
    def __init__(self, session_key: str, process_item_fn: Callable[[QueueItem], Coroutine[Any, Any, None]]):
        self.session_key = session_key
        self.process_item_fn = process_item_fn
        self._items: List[QueueItem] = []
        self._current_item: Optional[QueueItem] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    @property
    def current_item(self) -> Optional[QueueItem]:
        if self._current_item is not None:
            return self._current_item
        if self._worker_task is not None and not self._worker_task.done() and self._items:
            return self._items[0]
        return None

    def get_pending_items(self) -> List[QueueItem]:
        if self._current_item is not None:
            return list(self._items)
        if self._worker_task is not None and not self._worker_task.done() and self._items:
            return list(self._items[1:])
        return list(self._items)

    def is_busy(self) -> bool:
        return (self._current_item is not None) or (self._worker_task is not None and not self._worker_task.done())

    def queue_size(self) -> int:
        if self._current_item is not None:
            return len(self._items)
        if self._worker_task is not None and not self._worker_task.done():
            return max(0, len(self._items) - 1)
        return 0

    async def enqueue(self, item: QueueItem) -> Tuple[int, bool]:
        """
        Adds item to queue.
        Returns:
            pos: 1-based position in waiting queue, or 0 if starting immediately.
            is_queued: True if it has to wait in queue, False if it starts immediately.
        """
        async with self._lock:
            already_busy = self.is_busy()
            self._items.append(item)
            if not already_busy:
                pos = 0
                is_queued = False
            else:
                pos = len(self._items) if self._current_item is not None else (len(self._items) - 1)
                is_queued = True

            if self._worker_task is None or self._worker_task.done():
                self._worker_task = asyncio.create_task(self._worker_loop())

            return pos, is_queued

    async def _worker_loop(self):
        while True:
            item = None
            async with self._lock:
                if not self._items:
                    self._current_item = None
                    self._worker_task = None
                    break
                item = self._items.pop(0)
                self._current_item = item

            try:
                await self.process_item_fn(item)
            except asyncio.CancelledError:
                logger.info(f"Worker for {self.session_key} was cancelled.")
                break
            except Exception as e:
                logger.exception(f"Error in queue worker processing for {self.session_key}: {e}")
            finally:
                async with self._lock:
                    self._current_item = None

    async def remove_item(self, item_id: str) -> Optional[QueueItem]:
        """Removes a specific waiting item by item_id."""
        async with self._lock:
            start_idx = 0 if self._current_item is not None else (1 if (self._worker_task and not self._worker_task.done()) else 0)
            for i in range(start_idx, len(self._items)):
                if self._items[i].item_id == item_id:
                    return self._items.pop(i)
        return None

    async def clear_pending(self) -> List[QueueItem]:
        """Removes all waiting items from the queue (does not abort active running item)."""
        async with self._lock:
            if self._current_item is not None:
                removed = list(self._items)
                self._items.clear()
                return removed
            elif self._worker_task is not None and not self._worker_task.done() and self._items:
                removed = list(self._items[1:])
                self._items = self._items[:1]
                return removed
            else:
                removed = list(self._items)
                self._items.clear()
                return removed


class QueueManager:
    def __init__(self):
        self._queues: Dict[str, SessionQueue] = {}
        self._process_item_fn: Optional[Callable[[QueueItem], Coroutine[Any, Any, None]]] = None

    def set_processor(self, fn: Callable[[QueueItem], Coroutine[Any, Any, None]]):
        self._process_item_fn = fn

    def get_queue(self, session_key: str) -> SessionQueue:
        if session_key not in self._queues:
            if not self._process_item_fn:
                raise RuntimeError("Process function not set in QueueManager")
            self._queues[session_key] = SessionQueue(session_key, self._process_item_fn)
        return self._queues[session_key]

    async def enqueue(self, item: QueueItem) -> Tuple[int, bool]:
        q = self.get_queue(item.session_key)
        return await q.enqueue(item)

    def is_busy(self, session_key: str) -> bool:
        if session_key in self._queues:
            return self._queues[session_key].is_busy()
        return False

    def get_current_item(self, session_key: str) -> Optional[QueueItem]:
        if session_key in self._queues:
            return self._queues[session_key].current_item
        return None

    def get_pending_items(self, session_key: str) -> List[QueueItem]:
        if session_key in self._queues:
            return self._queues[session_key].get_pending_items()
        return []

    def get_queue_size(self, session_key: str) -> int:
        if session_key in self._queues:
            return self._queues[session_key].queue_size()
        return 0

    async def remove_item(self, session_key: str, item_id: str) -> Optional[QueueItem]:
        if session_key in self._queues:
            return await self._queues[session_key].remove_item(item_id)
        return None

    async def remove_item_anywhere(self, item_id: str) -> Tuple[Optional[str], Optional[QueueItem]]:
        for s_key, q in self._queues.items():
            removed = await q.remove_item(item_id)
            if removed:
                return s_key, removed
        return None, None

    async def clear_queue(self, session_key: str) -> List[QueueItem]:
        if session_key in self._queues:
            return await self._queues[session_key].clear_pending()
        return []

queue_manager = QueueManager()
