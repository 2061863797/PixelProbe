"""媒体会话：在一个进程内为同一媒体复用已加载的索引。

MCP 常驻工作进程会连续处理同一视频的多次请求。会话按媒体的快速指纹缓存索引，
文件内容变化后指纹改变、旧会话自然失效；缓存条目数有上限（最近使用优先保留）。
尚未建立索引的媒体不会被缓存为"无索引"，建立后下一次请求即可读到。

结果编号（如 event3、seg2）由 MCP 服务端按媒体登记，并随请求传给工作进程。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from pixelprobe.utils.fingerprint import quick_fingerprint

MAX_SESSIONS = 4


@dataclass
class MediaSession:
    fingerprint: str
    index: object | None


class SessionCache:
    def __init__(self, index_dir: Path | None = None, capacity: int = MAX_SESSIONS) -> None:
        self.index_dir = index_dir
        self.capacity = capacity
        self._sessions: OrderedDict[str, MediaSession] = OrderedDict()
        self._lock = threading.Lock()

    def session_for(self, media: Path) -> MediaSession:
        fingerprint = quick_fingerprint(Path(media))
        with self._lock:
            session = self._sessions.get(fingerprint)
            if session is not None:
                self._sessions.move_to_end(fingerprint)
                if session.index is not None:
                    return session
        index = self._load_index(media)
        with self._lock:
            session = self._sessions.get(fingerprint) or MediaSession(fingerprint, None)
            session.index = index
            self._sessions[fingerprint] = session
            self._sessions.move_to_end(fingerprint)
            while len(self._sessions) > self.capacity:
                self._sessions.popitem(last=False)
            return session

    def index_for(self, media: Path):
        """返回媒体当前内容的索引；未建立或不是视频时返回 None。"""
        return self.session_for(media).index

    def _load_index(self, media: Path):
        from pixelprobe.core.media_reader import detect_media_type
        from pixelprobe.index import IndexStore
        if detect_media_type(Path(media)) != "video":
            return None
        return IndexStore(self.index_dir).lookup(media)


_DEFAULT: SessionCache | None = None
_DEFAULT_LOCK = threading.Lock()


def media_sessions() -> SessionCache:
    """进程级会话缓存（MCP 常驻工作进程中跨请求保留）。"""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = SessionCache()
        return _DEFAULT
