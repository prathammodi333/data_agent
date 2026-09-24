"""Process-local anonymous session registry and artifact cleanup hooks."""

import threading
import time
import uuid


SESSION_TTL_SECONDS = 15 * 60


class AnonymousSessionRegistry:
    def __init__(self, ttl_seconds: int = SESSION_TTL_SECONDS):
        self.ttl_seconds = ttl_seconds
        self._sessions: dict[str, float] = {}
        self._lock = threading.Lock()
        self._cleaner = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleaner.start()

    def _cleanup_loop(self) -> None:
        while True:
            time.sleep(60)
            self.cleanup()

    def create(self) -> tuple[str, int]:
        self.cleanup()
        session_id = str(uuid.uuid4())
        expires_at = time.monotonic() + self.ttl_seconds
        with self._lock:
            self._sessions[session_id] = expires_at
        return session_id, self.ttl_seconds

    def is_active(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        with self._lock:
            expires_at = self._sessions.get(session_id)
            if expires_at is None:
                return False
            if expires_at <= time.monotonic():
                del self._sessions[session_id]
                expired = True
            else:
                expired = False
        if expired:
            self._delete_artifacts(session_id)
        return not expired

    def remaining_seconds(self, session_id: str) -> int:
        with self._lock:
            expires_at = self._sessions.get(session_id)
        if expires_at is None:
            return 0
        return max(0, int(expires_at - time.monotonic()))

    def cleanup(self) -> None:
        now = time.monotonic()
        with self._lock:
            expired = [session_id for session_id, expires_at in self._sessions.items()
                       if expires_at <= now]
            for session_id in expired:
                del self._sessions[session_id]
        for session_id in expired:
            self._delete_artifacts(session_id)

    @staticmethod
    def _delete_artifacts(session_id: str) -> None:
        from utils.etl_tools import delete_session_artifacts

        delete_session_artifacts(session_id)
