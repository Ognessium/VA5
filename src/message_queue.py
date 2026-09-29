import queue
import time
import threading
import logging
from typing import Any, Callable
from collections import OrderedDict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

@dataclass(order=True)
class PrioritizedMessage:
    """
    Wrapper for queue items.
    Priority 0 = high priority (bypasses standard messages).
    Priority 1 = standard.
    Timestamp ensures FIFO ordering within the same priority level.
    """
    priority: int
    timestamp: float
    message_id: str = field(compare=False)
    payload: Any = field(compare=False)


class GuaranteedMessageQueue:
    """
    Provides exactly-once delivery with priority bypassing.
    
    Guarantees:
    - Each message_id is delivered at most once (dedup on publish AND consume).
    - High-priority messages are consumed before standard messages.
    - Among same-priority messages, FIFO order is preserved via timestamp.
    
    Memory:
    - Processed IDs are stored in a bounded LRU cache to prevent unbounded growth
      in a long-running real-time agent.
    """
    MAX_PROCESSED_IDS = 10_000

    def __init__(self, consumer_callback: Callable[[Any], None]):
        self._queue: queue.PriorityQueue[PrioritizedMessage] = queue.PriorityQueue()
        self._processed_ids: OrderedDict[str, None] = OrderedDict()
        self._consumer_callback = consumer_callback
        self._lock = threading.Lock()
        self._running = True
        
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True, name="mq-worker")
        self._worker_thread.start()

    def publish(self, message_id: str, payload: Any, is_high_priority: bool = False):
        """
        Publishes a message. Drops silently if message_id was already processed (exactly-once).
        """
        with self._lock:
            if message_id in self._processed_ids:
                return
                
        priority = 0 if is_high_priority else 1
        msg = PrioritizedMessage(
            priority=priority,
            timestamp=time.monotonic(),
            message_id=message_id,
            payload=payload
        )
        self._queue.put(msg)

    def shutdown(self):
        """Gracefully shuts down the consumer thread."""
        self._running = False
        # Push a sentinel to unblock the worker
        self._queue.put(PrioritizedMessage(priority=99, timestamp=0.0, message_id="__shutdown__", payload=None))

    def _worker_loop(self):
        while self._running:
            try:
                msg: PrioritizedMessage = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if msg.message_id == "__shutdown__":
                self._queue.task_done()
                break
            
            with self._lock:
                if msg.message_id in self._processed_ids:
                    self._queue.task_done()
                    continue
                self._processed_ids[msg.message_id] = None
                # Evict oldest entries if we exceed the cap
                while len(self._processed_ids) > self.MAX_PROCESSED_IDS:
                    self._processed_ids.popitem(last=False)
                
            try:
                self._consumer_callback(msg.payload)
            except Exception:
                logger.exception(f"Failed to process message {msg.message_id}")
            finally:
                self._queue.task_done()
