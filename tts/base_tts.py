from threading import Thread
import queue
from queue import Queue
from io import BytesIO
from enum import Enum
from concurrent.futures import ThreadPoolExecutor

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from avatars.base_avatar import BaseAvatar

from utils.logger import logger

class State(Enum):
    RUNNING = 0
    PAUSE = 1

class BaseTTS:
    def __init__(self, opt, parent: "BaseAvatar"):
        self.opt = opt
        self.parent = parent

        #self.fps = opt.fps # 20 ms per frame
        self.sample_rate = 16000
        self.chunk = self.sample_rate // (opt.fps*2) # 320 samples per chunk (20ms * 16000 / 1000)
        self.input_stream = BytesIO()

        self.msgqueue = Queue()
        self.state = State.RUNNING

        # ── 句间流水线预取（修复"说话断断续续"）──
        # 旧实现：句1 全部推完帧 → 才 POST 句2 → 等首包(0.2~2.3s 不稳定)
        #         → 句1 播完、句2 未到 → 音频队列耗尽 → 静音间隙
        # 新实现：播句1 的推帧间隙，后台线程提前打开句2 的合成请求（POST 立即返回
        #         header，body 由服务端排队生成），句1 播完句2 立即可用 → 无缝衔接
        self._prefetch_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='tts-prefetch')
        self._pending = None  # (future, msg)：已预取未播放的下一句

    def flush_talk(self):
        logger.info(f"[TTS] flush_talk called, queue had {self.msgqueue.qsize()} items")
        self.msgqueue.queue.clear()
        self.state = State.PAUSE
        # 打断时丢弃预取（POST 已发出的由 play_stream 消费时检查 state 丢弃）
        if self._pending:
            try:
                self._pending[0].cancel()
            except Exception:
                pass
            self._pending = None

    def put_msg_txt(self, msg: str, datainfo: dict = {}):
        if len(msg) > 0:
            self.msgqueue.put((msg, datainfo))

    def render(self, quit_event):
        process_thread = Thread(target=self.process_tts, args=(quit_event,))
        process_thread.start()

    # ── 流水线接口（子类可选实现；不实现则走原 txt_to_audio 路径） ──

    def open_stream(self, msg: tuple[str, dict]):
        """预打开一句的合成（返回句柄，body 未消费）。子类重写。"""
        return None

    def play_stream(self, handle, msg: tuple[str, dict]):
        """消费 open_stream 的句柄，逐帧推给渲染（推帧间隙应调 _maybe_prefetch）。子类重写。"""
        pass

    def _prefetch_supported(self) -> bool:
        return type(self).open_stream is not BaseTTS.open_stream

    def _maybe_prefetch(self):
        """在推帧间隙调用：非阻塞取下一句并后台预打开。"""
        if self._pending is not None or not self._prefetch_supported():
            return
        try:
            next_msg = self.msgqueue.get_nowait()
        except queue.Empty:
            return
        self.state = State.RUNNING
        fut = self._prefetch_pool.submit(self._do_prefetch, next_msg)
        self._pending = (fut, next_msg)
        logger.info(f"[TTS-Prefetch] 已预取下一句（队列中剩余 {self.msgqueue.qsize()})")

    def _do_prefetch(self, msg):
        """后台预取线程执行。"""
        logger.info(f"[TTS-Prefetch] 后台开始预取: {msg[0][:30]!r}")
        handle = self.open_stream(msg)
        if handle is None:
            logger.warning("[TTS-Prefetch] 预取返回 None，跳过")
        return handle

    def _get_handle(self, msg):
        """取当前句句柄：命中预取用预取结果，否则现场打开。"""
        if self._pending is not None:
            fut, pending_msg = self._pending
            self._pending = None
            if pending_msg is msg:
                return fut.result()
            # 预取的不是这句（顺序异常）→ 丢弃重来
            try:
                fut.cancel()
            except Exception:
                pass
        return self.open_stream(msg)

    def process_tts(self, quit_event):
        while not quit_event.is_set():
            # 取句：优先用推帧间隙预取到的下一句（顺序保持）
            if self._pending is not None:
                fut, msg = self._pending
                self._pending = None
            else:
                try:
                    msg: tuple[str, dict] = self.msgqueue.get(block=True, timeout=1)
                    self.state = State.RUNNING
                except queue.Empty:
                    continue
            try:
                if self._prefetch_supported():
                    handle = self._get_handle(msg)
                    if handle is not None:
                        self.play_stream(handle, msg)
                else:
                    self.txt_to_audio(msg)
            except Exception as e:
                # 单句合成失败不应阻塞整个 TTS 链路，记录后继续处理下一句
                logger.exception(f"tts error: {e}")
        self.stop_tts()
        logger.info('ttsreal thread stop')

    def txt_to_audio(self, msg: tuple[str, dict]):
        pass

    def stop_tts(self):
        pass
