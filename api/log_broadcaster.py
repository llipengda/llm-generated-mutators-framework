"""Thread-safe log broadcaster for WebSocket streaming.

Each session has a queue.Queue.  The log system pushes lines into it;
the WebSocket endpoint pulls lines out and sends them to the client.

Messages are buffered even before a WebSocket connects, so no log is
lost due to connection timing.
"""

from __future__ import annotations

import queue
import threading


class LogBroadcaster:
    """Per-session message queues for real-time log streaming."""

    def __init__(self):
        self._queues: dict[str, queue.Queue[str]] = {}
        self._buffers: dict[str, list[str]] = {}  # before WS connects
        self._lock = threading.Lock()

    def subscribe(self, session_id: str) -> queue.Queue[str]:
        """Create / return the message queue for *session_id*."""
        with self._lock:
            q: queue.Queue[str] = queue.Queue(maxsize=2000)
            self._queues[session_id] = q
            # Drain buffered messages.
            buf = self._buffers.pop(session_id, [])
            for line in buf:
                try:
                    q.put_nowait(line)
                except queue.Full:
                    break
            return q

    def unsubscribe(self, session_id: str) -> None:
        """Remove the queue for *session_id* (client disconnected)."""
        with self._lock:
            self._queues.pop(session_id, None)

    def broadcast(self, session_id: str, line: str) -> None:
        """Push a single line to the session's queue (non-blocking)."""
        with self._lock:
            q = self._queues.get(session_id)
            if q is not None:
                try:
                    q.put_nowait(line)
                except queue.Full:
                    try:
                        q.get_nowait()
                        q.put_nowait(line)
                    except queue.Empty:
                        pass
            else:
                # WS not connected yet — buffer for later.
                buf = self._buffers.setdefault(session_id, [])
                buf.append(line)
                # Cap buffer at 2000 lines.
                if len(buf) > 2000:
                    buf.pop(0)


# Singleton.
broadcaster = LogBroadcaster()
