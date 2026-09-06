"""
增量索引文件监听器

基于 watchdog（macOS 走 FSEvents 原生事件），递归监听源目录，
事件去抖（debounce）后触发一次增量同步，避免频繁改动造成重复索引。
"""

import threading
from pathlib import Path
from typing import Callable, List, Optional

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from src.logging_setup import get_logger

logger = get_logger(__name__)


class SyncEventHandler(FileSystemEventHandler):
    """
    文件系统事件处理器：去抖后回调

    任意文件事件（创建/修改/删除/移动）都会重置去抖计时器，
    停顿 debounce 秒后才真正触发 on_change（增量同步）。
    """

    def __init__(self, on_change: Callable[[], None], debounce: float = 2.0):
        super().__init__()
        self.on_change = on_change
        self.debounce = debounce
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    def on_any_event(self, event):
        # 目录事件忽略（其下的文件事件会单独触发），避免重复同步
        if event.is_directory:
            return
        self._schedule()

    def _schedule(self):
        """重置去抖计时器"""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self):
        """去抖结束，触发同步回调"""
        with self._lock:
            self._timer = None
        try:
            self.on_change()
        except Exception as e:  # 监听回调不应让观察者线程崩溃
            logger.warning("增量同步回调异常: %s", e)


class IndexWatcher:
    """增量索引文件监听器（守护线程运行）"""

    def __init__(
            self,
            source_dirs: List[str],
            on_change: Callable[[], None],
            debounce: float = 2.0,
    ):
        """
        初始化监听器

        Args:
            source_dirs: 要监听的源目录列表
            on_change: 触发增量同步的回调
            debounce: 事件去抖秒数
        """
        self.source_dirs = source_dirs
        self.on_change = on_change
        self.debounce = debounce
        self._observer: Optional[Observer] = None

    def start(self):
        """启动监听（守护线程，随进程退出）"""
        if self._observer is not None:
            return

        observer = Observer()
        handler = SyncEventHandler(self.on_change, self.debounce)

        for d in self.source_dirs:
            path = Path(d).expanduser().resolve()
            if path.exists():
                observer.schedule(handler, str(path), recursive=True)
            else:
                logger.warning("监听目录不存在，已跳过: %s", path)

        observer.daemon = True  # 守护线程：进程退出时自动结束
        observer.start()
        self._observer = observer
        logger.info("👀 增量索引监听已启动: %s (去抖 %.1fs)", self.source_dirs, self.debounce)

    def stop(self):
        """停止监听"""
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
            logger.info("⏹️ 增量索引监听已停止")