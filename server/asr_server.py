###############################################################################
#  ASR WebSocket Server — Local SenseVoice/FunASR Integration
#
#  Resolves: https://github.com/lipku/LiveTalking/issues/604
#
#  This module provides a WebSocket endpoint (/api/asr) that speaks the same
#  protocol as the external FunASR server (wss://www.funasr.com:10096/).
#  The browser client (web/asr/main.js) can connect here instead, keeping
#  all ASR processing local and cutting ~600ms of network + Whisper latency.
#
#  Copyright (C) 2024 LiveTalking@lipku https://github.com/lipku/LiveTalking
#  Licensed under the Apache License, Version 2.0
###############################################################################

import json
import time
import io
import os
import asyncio
import threading
import numpy as np
from aiohttp import web

from utils.logger import logger
from server.session_manager import session_manager

# 音频留存目录：判停识别的同时把原始音频落盘，
# 作为说话人分离（实时比对 + 会后离线聚类）的数据源
AUDIO_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "audio")

# SmartTurn 语义复核的结束状态枚举（在 _load_smart_turn 里懒加载，这里仅作类型引用）
try:
    from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
except Exception:
    EndOfTurnState = None


# ─── Lazy Model Loader ────────────────────────────────────────────────────

_sensevoice_model = None
_sensevoice_load_lock = threading.Lock()
_sensevoice_inference_lock = threading.Lock()

# Silero VAD 模型（说话停顿自动判停）
_silero_vad_model = None
_silero_vad_load_lock = threading.Lock()

# 流式 ASR 模型（paraformer-zh-streaming，2-pass 第一遍）
_streaming_model = None
_streaming_load_lock = threading.Lock()

# 全局用户说话状态：VAD 检测到用户说话时设 True，判停/结束后设 False。
# 供 TTS play_stream 检查——用户正在说话时数字人不应开始播放。
_user_is_speaking = False


def is_user_speaking() -> bool:
    """查询用户当前是否正在说话（由 ASR VAD 实时更新）。"""
    return _user_is_speaking


def _load_streaming_model():
    """
    Load the streaming Paraformer model (paraformer-zh-streaming) on first call.
    Used for 2-pass streaming ASR (Pass 1: real-time partial results).
    """
    global _streaming_model
    if _streaming_model is not None:
        return _streaming_model

    with _streaming_load_lock:
        if _streaming_model is not None:
            return _streaming_model

        from funasr import AutoModel
        import torch
        device = "cuda:0" if torch.cuda.is_available() else "cpu"
        logger.info(f"[ASR] Loading paraformer-zh-streaming on device='{device}'...")
        _streaming_model = AutoModel(
            model="paraformer-zh-streaming",
            device=device,
            disable_update=True,
        )
        logger.info("[ASR] ✅ Streaming Paraformer ready")
    return _streaming_model


def _streaming_infer(model, audio_chunk, cache):
    """在线程池中执行流式推理（问题B：避免阻塞事件循环）。"""
    return model.generate(
        input=audio_chunk,
        cache=cache,
        is_final=False,
        chunk_size=[0, 10, 5],
        encoder_chunk_look_back=4,
        decoder_chunk_look_back=1,
    )


def _load_silero_vad():
    """
    Load the Silero VAD model on first call (lazy singleton).
    Used for automatic end-of-speech detection (VAD auto turn-taking).
    """
    global _silero_vad_model
    if _silero_vad_model is not None:
        return _silero_vad_model

    with _silero_vad_load_lock:
        if _silero_vad_model is not None:
            return _silero_vad_model

        from silero_vad import load_silero_vad
        logger.info("[ASR] Loading Silero VAD for auto turn-taking...")
        _silero_vad_model = load_silero_vad()
        logger.info("[ASR] ✅ Silero VAD ready")
    return _silero_vad_model


# SmartTurn 语义复核模型（判断用户是否真说完，防止把一句话切成两段）
_smart_turn_model = None
_smart_turn_load_lock = threading.Lock()


def _load_smart_turn():
    """
    Load the SmartTurn v3 semantic end-of-turn analyzer on first call (lazy singleton).
    参考 VoxEMW：Silero 提供 is_speech 标记，SmartTurn 用 ML 模型判断语义是否说完。
    """
    global _smart_turn_model
    if _smart_turn_model is not None:
        return _smart_turn_model

    with _smart_turn_load_lock:
        if _smart_turn_model is not None:
            return _smart_turn_model

        try:
            from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
            from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
            logger.info("[ASR] Loading SmartTurn v3 semantic end-of-turn analyzer...")
            # stop_secs 为静音判停阈值（1s），让 SmartTurn 在足够长的静音后才判停，
            # 避免把一句话中间的自然停顿（思考/换气）误判为说完而切成碎片
            _smart_turn_model = LocalSmartTurnAnalyzerV3(
                sample_rate=16000,
                # stop_secs 为静音判停阈值，用户停顿超过此时间才判定为说完。
                 # 长段说话时用户可能需要停顿 2-3 秒思考，2.5s 能有效减少抢话。
                 # 用户说完了只需多等 1 秒，体验远好于"还没说完就被打断"。
                 params=SmartTurnParams(stop_secs=2.5, pre_speech_ms=500, max_duration_secs=15),
            )
            # 显式设置采样率（BaseTurnAnalyzer 默认 _sample_rate=0，需 set_sample_rate 生效）
            _smart_turn_model.set_sample_rate(16000)
            logger.info("[ASR] ✅ SmartTurn v3 ready")
        except Exception as e:
            logger.warning(f"[ASR] SmartTurn load failed, fallback to Silero-only: {e}")
            _smart_turn_model = None
    return _smart_turn_model


def _load_sensevoice():
    """
    Load the SenseVoice model on first call (lazy singleton).
    Concurrent first requests must share the same model initialization.
    """
    global _sensevoice_model
    if _sensevoice_model is not None:
        return _sensevoice_model

    with _sensevoice_load_lock:
        if _sensevoice_model is not None:
            return _sensevoice_model

        import torch
        from funasr import AutoModel

        device = "cuda:0" if torch.cuda.is_available() else "cpu"
        logger.info(
            f"[ASR] Loading SenseVoiceSmall on device='{device}' "
            f"(first run will download ~500MB from ModelScope)..."
        )

        t0 = time.perf_counter()
        _sensevoice_model = AutoModel(
            model="iic/SenseVoiceSmall",
            vad_model="fsmn-vad",
            vad_kwargs={"max_single_segment_time": 30000},
            device=device,
            trust_remote_code=True,
        )
        elapsed = time.perf_counter() - t0
        logger.info(
            f"[ASR] ✅ SenseVoiceSmall ready — loaded in {elapsed:.1f}s on {device}"
        )
    return _sensevoice_model


# ─── Audio Persistence ─────────────────────────────────────────────────────

# 已落盘的音频总量上限（字节）。单条 2-10 秒约 60-300KB，
# 一场 30 分钟多人会议约 5-10MB；超出后从最老的开始清理，防止无限增长。
AUDIO_QUOTA_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB


def _save_audio_clip(audio_float32: np.ndarray, sample_rate: int, conv_id: str) -> str:
    """把一段说话音频落盘到 data/audio/{conv_id}/{unix_ms}.wav。

    返回相对 AUDIO_ROOT 的路径（如 "e085e65b.../1787734961400.wav"）。
    conv_id 为空、音频太短或写盘失败时返回 ""，绝不抛异常 —— 这是硬要求。
    """
    try:
        if not conv_id or len(audio_float32) < sample_rate * 0.2:  # <200ms 不存
            return ""
        import soundfile as sf
        safe_id = "".join(c for c in str(conv_id) if c.isalnum() or c in "-_")
        if not safe_id:
            return ""
        conv_dir = os.path.join(AUDIO_ROOT, safe_id)
        os.makedirs(conv_dir, exist_ok=True)
        fname = f"{int(time.time() * 1000)}.wav"
        fpath = os.path.join(conv_dir, fname)
        sf.write(fpath, audio_float32, sample_rate, format="WAV")
        try:
            _enforce_audio_quota(os.path.getsize(fpath))
        except OSError:
            pass
        return f"{safe_id}/{fname}"
    except Exception as e:
        logger.warning(f"[ASR] 音频落盘失败（不影响识别）: {e}")
        return ""


# 已统计的音频总字节数（None = 尚未统计）。增量累加，避免每次落盘都全盘扫描
_audio_total_bytes = None
_audio_quota_lock = threading.Lock()


def _scan_audio_files() -> list:
    """扫描全部留存音频，返回 [(mtime, path, size), ...]。"""
    files = []
    for dirpath, _, filenames in os.walk(AUDIO_ROOT):
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            try:
                st = os.stat(p)
                files.append((st.st_mtime, p, st.st_size))
            except OSError:
                continue
    return files


def _enforce_audio_quota(added_bytes: int = 0):
    """超过总配额时从最老的音频文件开始清理。

    只在「累计量可能超限」时才全盘扫描 —— 每段语音落盘都 os.walk + stat
    全部文件是 O(n²) 开销，且它在实时链路里同步执行，会拖慢识别。
    """
    global _audio_total_bytes
    with _audio_quota_lock:
        try:
            if not os.path.isdir(AUDIO_ROOT):
                return
            if _audio_total_bytes is None:
                # 首次：全量校准一次
                _audio_total_bytes = sum(size for _, _, size in _scan_audio_files())
            else:
                _audio_total_bytes += added_bytes
            if _audio_total_bytes <= AUDIO_QUOTA_BYTES:
                return
            # 可能超限：扫描一次拿到准确清单再清理（顺便修正累计误差）
            files = _scan_audio_files()
            total = sum(size for _, _, size in files)
            files.sort()  # 最老的在前
            for mtime, p, size in files:
                if total <= AUDIO_QUOTA_BYTES:
                    break
                try:
                    os.remove(p)
                    total -= size
                except OSError:
                    continue
            _audio_total_bytes = total
            logger.info(f"[ASR] 音频配额清理完成，当前总量 {total / 1024 / 1024:.1f} MB")
        except Exception as e:
            logger.warning(f"[ASR] 音频配额清理失败: {e}")


# ─── Realtime Speaker Identification ───────────────────────────────────────

def _identify_speaker_safe(conv_id: str, audio_float32: np.ndarray, sample_rate: int) -> dict:
    """说话人判定的异常安全包装：任何失败都返回空结果，绝不影响 ASR 主流程。"""
    empty = {"is_self": False, "speaker": "", "conf": 0.0, "source": ""}
    try:
        from agent.speaker_store import identify_speaker
        return identify_speaker(conv_id, audio_float32, sample_rate)
    except Exception as e:
        logger.warning(f"[ASR] 说话人判定失败（不影响识别）: {e}")
        return empty


def _warmup_speaker_model():
    """后台预热 CAM++ 模型并注册数字人自身声纹。

    首次加载约 8-10 秒 + 自身声纹提取，放在 daemon 线程里跑，
    避免第一句话的说话人判定被拖慢。失败无害（届时懒加载）。
    """
    try:
        from agent.speaker_store import _load_model, ensure_avatar_voiceprint
        _load_model()
        ensure_avatar_voiceprint()
        logger.info("[Speaker] 模型预热完成（实时说话人判定就绪）")
    except Exception as e:
        logger.warning(f"[Speaker] 预热失败（将懒加载）: {e}")


# 模块加载即启动预热（daemon 线程，不阻塞 import）
threading.Thread(target=_warmup_speaker_model, daemon=True, name="speaker-warmup").start()


def _run_inference(audio_float32: np.ndarray, sample_rate: int, use_itn: bool,
                   conv_id: str = ""):
    """
    Run SenseVoice inference on a float32 audio array.

    This is a **blocking** call — always invoke from ``run_in_executor``.

    当 conv_id 非空时，把这段音频（归一化前的原始数据）落盘到
    data/audio/{conv_id}/ 下，供说话人分离使用。

    Returns
    -------
    tuple[str, float, float, str]
        (transcribed_text, inference_ms, audio_duration_s, audio_rel_path)
        audio_rel_path 为相对 AUDIO_ROOT 的路径；未落盘时为 ""
    """
    import soundfile as sf
    from funasr.utils.postprocess_utils import rich_transcription_postprocess

    # ── 音频留存：归一化前的原始数据才保留真实音量特征，利于声纹比对。
    #    独立 try/except —— 落盘失败只记日志，绝不影响识别主流程。
    audio_rel = _save_audio_clip(audio_float32, sample_rate, conv_id)

    model = _load_sensevoice()

    # 音频能量检测：如果能量太低（基本是静音），直接返回空
    rms = float(np.sqrt(np.mean(audio_float32 ** 2))) if len(audio_float32) > 0 else 0.0
    if rms < 0.005:  # 静音阈值
        logger.info(f"[ASR] ⚠️ Audio too quiet (rms={rms:.4f}), skipping recognition")
        # 必须返回与正常路径等长的 4 元组，否则调用方解包会抛 ValueError
        return "", 0.0, len(audio_float32) / sample_rate, audio_rel

    # 音频归一化：放大到满幅，提高识别率（麦克风音量低时尤其有效）
    max_abs = float(np.max(np.abs(audio_float32))) if len(audio_float32) > 0 else 0.0
    if max_abs > 0:
        audio_norm = audio_float32 / max_abs * 0.9  # 归一化到 0.9 满幅
    else:
        audio_norm = audio_float32

    # Write to in-memory WAV so funasr can read the sample rate from the header
    wav_buf = io.BytesIO()
    sf.write(wav_buf, audio_norm, sample_rate, format="WAV")
    wav_buf.seek(0)

    t0 = time.perf_counter()
    with _sensevoice_inference_lock:
        res = model.generate(
            input=wav_buf,
            cache={},
            language="auto",
            use_itn=use_itn,
            batch_size_s=60,
            merge_vad=True,  # 合并所有 VAD 段为一条完整转写，避免只取第一段导致后半句丢失
        )
    inference_ms = (time.perf_counter() - t0) * 1000

    # 合并所有段（兜底：即使 merge_vad 未生效，也把多段拼接成完整句）
    text = ""
    if res:
        for r in res:
            if r.get("text"):
                text += rich_transcription_postprocess(r["text"])

    audio_duration_s = len(audio_float32) / sample_rate

    logger.info(
        f"[ASR] ✅ SenseVoice inference complete\n"
        f"       ├─ Latency     : {inference_ms:>8.0f} ms\n"
        f"       ├─ Audio length: {audio_duration_s:>8.1f} s\n"
        f"       ├─ RTF         : {inference_ms / 1000 / max(audio_duration_s, 0.001):>8.3f}\n"
        f"       └─ Text        : \"{text[:100]}{'…' if len(text) > 100 else ''}\""
    )

    return text, inference_ms, audio_duration_s, audio_rel


# ─── WebSocket Handler ─────────────────────────────────────────────────────

SAMPLE_RATE = 16000  # The browser client records at 16 kHz mono PCM16

# VAD 自动判停参数
VAD_SPEECH_THRESHOLD = 0.3      # 说话概率阈值（> 此值判定为说话，降低以更易触发）
VAD_SILENCE_MS = 600            # 静音持续多少毫秒判定为"说话结束"
VAD_CHUNK_SAMPLES = 512         # Silero VAD 每次检测的样本数（32ms @ 16kHz）
VAD_MIN_SPEECH_MS = 300         # 最短有效语音（太短忽略，防误触发）


def _vad_detect_speech_end(vad_model, audio_float32: np.ndarray) -> bool:
    """
    用 Silero VAD 检测音频末尾是否有"说话结束"（静音段超过阈值）。
    返回 True 表示检测到说话结束，应触发识别。
    仅当音频中检测到过说话，且末尾静音超过阈值时才返回 True。
    """
    import torch

    # 逐帧检测说话状态
    speech_frames = []  # 每帧是否说话
    n_frames = len(audio_float32) // VAD_CHUNK_SAMPLES
    for i in range(n_frames):
        chunk = audio_float32[i * VAD_CHUNK_SAMPLES:(i + 1) * VAD_CHUNK_SAMPLES]
        if len(chunk) < VAD_CHUNK_SAMPLES:
            break
        prob = vad_model(torch.from_numpy(chunk), SAMPLE_RATE).item()
        speech_frames.append(prob > VAD_SPEECH_THRESHOLD)

    if not speech_frames:
        return False

    # 必须检测到过说话（否则纯静音不应触发识别）
    if not any(speech_frames):
        return False

    # 统计末尾连续静音帧数
    trailing_silence_frames = 0
    for is_speech in reversed(speech_frames):
        if is_speech:
            break
        trailing_silence_frames += 1

    trailing_silence_ms = trailing_silence_frames * (VAD_CHUNK_SAMPLES * 1000 / SAMPLE_RATE)
    return trailing_silence_ms >= VAD_SILENCE_MS


# VAD 打断：用户开口（speech_started 上升沿）且数字人在说话 → 打断。
# is_speaking() 由 TTS play_stream 即时设为 True（不再依赖渲染线程异步更新），
# 由 base_tts flush_talk 清空队列时保持 PAUSE 状态，故检查 is_speaking 已足够可靠。
# 0.5 秒防自激窗口防止回声/多路 VAD 误触发导致的频繁打断。
_last_maybe_interrupt_time = 0.0

def _maybe_interrupt(audio_seg=None):
    """用户开口（speech_started 上升沿）且数字人在说话 → 打断。

    门控：如果这段实际上来自数字人自己的声音（扬声器回灌，is_avatar_voice=True），
    则不打断，避免数字人被自己的声音打断（回声自我打断）。
    """
    global _last_maybe_interrupt_time, _user_is_speaking
    now = time.perf_counter()
    if now - _last_maybe_interrupt_time < 0.5:
        return
    try:
        # 数字人自身声音门控：当数字人正在说话，且麦克风这段被判为数字人自己的声音时忽略
        if audio_seg is not None:
            try:
                from agent.speaker_store import is_avatar_voice
                if is_avatar_voice(audio_seg, SAMPLE_RATE):
                    logger.info("[ASR] 检测到数字人自身声音，忽略打断（回声门控）")
                    return
            except Exception as e:
                logger.warning(f"[ASR] _maybe_interrupt 回声门控失败: {e}")
        for sid, avatar in session_manager.sessions.items():
            if avatar is None:
                continue
            is_spk = hasattr(avatar, "is_speaking") and avatar.is_speaking()
            logger.info(f"[ASR] _maybe_interrupt: session={sid} is_speaking={is_spk}")
            if is_spk:
                logger.info(f"[ASR] 🛑 用户开口，打断数字人 (session={sid})")
                avatar.flush_talk()
                _user_is_speaking = False  # 打断后重置用户说话状态，避免下一轮误判
                _last_maybe_interrupt_time = now
                return
    except Exception as e:
        logger.warning(f"[ASR] _maybe_interrupt error: {e}")


async def asr_websocket_handler(request):
    """
    WebSocket handler implementing the FunASR client protocol with VAD auto turn-taking.

    Protocol flow
    -------------
    1. Client opens connection
    2. Client sends JSON config::

           {"chunk_size":[5,10,5], "wav_name":"h5",
            "is_speaking":true, "mode":"2pass", "itn":false, ...}

    3. Client streams binary PCM16 audio chunks (960 bytes = 60 ms @ 16 kHz)
    4. Server uses Silero VAD to detect end-of-speech automatically
    5. Server responds with transcription::

           {"text":"hello world", "mode":"2pass-offline",
            "is_final":true, "timestamp":null}
    """
    global _user_is_speaking
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    client_ip = request.remote
    logger.info(f"[ASR] 🔌 WebSocket connected from {client_ip}")

    audio_buffer = bytearray()
    config: dict = {}
    session_start = time.perf_counter()
    chunks_received = 0

    # VAD 自动判停状态
    vad_model = None
    smart_turn = None               # SmartTurn 语义复核模型（判断是否真说完）
    speech_started = False          # 是否已检测到说话开始
    last_speech_time = None         # 最近一次说话的时间
    speech_audio = bytearray()      # 当前说话段的音频
    vad_pending = bytearray()       # VAD 逐块检测的待处理缓冲区

    # 流式识别状态（2-pass 第一遍）
    streaming_model = None
    stream_cache = {}               # 流式模型跨块状态
    stream_pending = bytearray()    # 流式识别的待处理块
    last_partial_text = ""          # 上一次 partial 结果

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    logger.warning("[ASR] Received invalid JSON, ignoring")
                    continue

                if data.get("is_speaking") is True:
                    # ── Session start ──────────────────────────────────
                    config = data
                    audio_buffer = bytearray()
                    chunks_received = 0
                    session_start = time.perf_counter()
                    speech_started = False
                    last_speech_time = None
                    speech_audio = bytearray()
                    vad_pending = bytearray()
                    # 流式识别状态重置
                    stream_cache = {}
                    stream_pending = bytearray()
                    last_partial_text = ""
                    # 懒加载 VAD 模型
                    try:
                        vad_model = _load_silero_vad()
                    except Exception as e:
                        logger.warning(f"[ASR] Silero VAD load failed, fallback to manual stop: {e}")
                        vad_model = None
                    # 懒加载 SmartTurn 语义复核模型（失败则回退到纯 Silero 判停）
                    try:
                        smart_turn = _load_smart_turn()
                    except Exception as e:
                        logger.warning(f"[ASR] SmartTurn load failed, fallback to Silero-only: {e}")
                        smart_turn = None
                    # 懒加载流式模型（2-pass 第一遍）
                    try:
                        streaming_model = _load_streaming_model()
                    except Exception as e:
                        logger.warning(f"[ASR] Streaming model load failed, fallback to offline: {e}")
                        streaming_model = None
                    logger.info(
                        f"[ASR] 🎙️  Recording started (VAD auto turn-taking + streaming) | "
                        f"mode={config.get('mode', 'offline')} | "
                        f"itn={config.get('itn', False)}"
                    )

                elif data.get("is_speaking") is False:
                    _user_is_speaking = False
                    # ── Manual stop (fallback) → run inference ─────────
                    if len(speech_audio) > 0:
                        audio_buffer = speech_audio
                    await _run_and_send_inference(ws, audio_buffer, config)
                    # 重置状态
                    audio_buffer = bytearray()
                    speech_audio = bytearray()
                    speech_started = False
                    last_speech_time = None

            elif msg.type == web.WSMsgType.BINARY:
                audio_buffer.extend(msg.data)
                chunks_received += 1

                # VAD 自动判停（仅当 VAD 可用时）
                if vad_model is not None:
                    # 累积到 speech_audio（当前说话段）
                    speech_audio.extend(msg.data)

                    # 逐块 VAD 检测：累积到 512 样本（32ms）就检测一次
                    import torch
                    vad_pending.extend(msg.data)
                    while len(vad_pending) >= VAD_CHUNK_SAMPLES * 2:
                        # 取一个 512 样本块（1024 字节）
                        chunk_bytes = bytes(vad_pending[:VAD_CHUNK_SAMPLES * 2])
                        del vad_pending[:VAD_CHUNK_SAMPLES * 2]
                        chunk_int16 = np.frombuffer(chunk_bytes, dtype=np.int16)
                        chunk_float32 = chunk_int16.astype(np.float32) / 32768.0
                        prob = vad_model(torch.from_numpy(chunk_float32), SAMPLE_RATE).item()
                        is_speech = prob > VAD_SPEECH_THRESHOLD
                        if is_speech:
                            if not speech_started:
                                _user_is_speaking = True
                                # 用户开口（speech_started 从 false 变 true）→ 打断数字人
                                # 传入当前说话段，先做数字人自身声音门控（回声不自打断）
                                _seg = np.frombuffer(bytes(speech_audio), dtype=np.int16).astype(np.float32) / 32768.0 if speech_audio else None
                                _maybe_interrupt(_seg)
                            speech_started = True
                            last_speech_time = time.perf_counter()

                        # 喂给 SmartTurn 语义复核（带 is_speech 标记），用其返回值判断是否说完
                        if smart_turn is not None:
                            st_state = smart_turn.append_audio(chunk_bytes, is_speech)
                            if st_state == EndOfTurnState.COMPLETE:
                                _user_is_speaking = False
                                logger.info("[ASR] 🛑 SmartTurn detected end-of-speech (semantic)")
                                await _run_and_send_inference(ws, speech_audio, config)
                                # 重置说话段
                                audio_buffer = bytearray()
                                speech_audio = bytearray()
                                vad_pending = bytearray()
                                speech_started = False
                                last_speech_time = None
                                stream_cache = {}
                                stream_pending = bytearray()
                                last_partial_text = ""
                                smart_turn.clear()

                    # 回退：SmartTurn 不可用时，用 Silero 静音阈值判停
                    if smart_turn is None and speech_started and last_speech_time is not None:
                        silence_ms = (time.perf_counter() - last_speech_time) * 1000
                        if silence_ms >= VAD_SILENCE_MS:
                            _user_is_speaking = False
                            logger.info(f"[ASR] 🛑 VAD detected end-of-speech ({silence_ms:.0f}ms silence)")
                            await _run_and_send_inference(ws, speech_audio, config)
                            # 重置说话段（问题1：同时清空 audio_buffer，避免内存泄漏）
                            audio_buffer = bytearray()
                            speech_audio = bytearray()
                            vad_pending = bytearray()
                            speech_started = False
                            last_speech_time = None
                            stream_cache = {}
                            stream_pending = bytearray()
                            last_partial_text = ""

                # 流式识别（2-pass 第一遍）：边说边出字
                if streaming_model is not None and speech_started:
                    stream_pending.extend(msg.data)
                    # 每 600ms（9600 字节 @16kHz）识别一次
                    STREAM_CHUNK_BYTES = 9600  # 600ms * 16000 * 2
                    while len(stream_pending) >= STREAM_CHUNK_BYTES:
                        chunk_bytes = bytes(stream_pending[:STREAM_CHUNK_BYTES])
                        del stream_pending[:STREAM_CHUNK_BYTES]
                        chunk_int16 = np.frombuffer(chunk_bytes, dtype=np.int16)
                        chunk_float32 = chunk_int16.astype(np.float32) / 32768.0
                        try:
                            # 问题B：流式推理是阻塞调用，放到线程池执行，避免阻塞事件循环
                            loop = asyncio.get_event_loop()
                            res = await loop.run_in_executor(
                                None,
                                _streaming_infer,
                                streaming_model, chunk_float32, stream_cache,
                            )
                            if res and len(res) > 0 and res[0].get("text"):
                                partial = res[0]["text"]
                                if partial != last_partial_text:
                                    last_partial_text = partial
                                    # 发送 partial 结果（前端实时显示）
                                    await ws.send_str(json.dumps({
                                        "text": partial,
                                        "mode": "online",
                                        "is_final": False,
                                        "timestamp": None,
                                    }))
                        except Exception as e:
                            logger.warning(f"[ASR] Streaming inference error: {e}")

            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break

    except asyncio.CancelledError:
        logger.info("[ASR] WebSocket handler cancelled")
    except Exception as e:
        logger.exception(f"[ASR] ❌ WebSocket handler error: {e}")

    # 断开时重置用户说话状态，避免影响下一次连接（_user_is_speaking 已在函数顶部声明为 global）
    _user_is_speaking = False
    logger.info(f"[ASR] 🔌 WebSocket disconnected ({client_ip}), _user_is_speaking reset")
    return ws


async def _run_and_send_inference(ws, audio_buffer: bytearray, config: dict):
    """对累积的音频做 SenseVoice 识别，并通过 WebSocket 返回结果。"""
    buf_bytes = len(audio_buffer)
    if buf_bytes < 640:  # < 20 ms of audio — skip
        logger.warning("[ASR] Audio too short (< 20ms), returning empty")
        await ws.send_str(json.dumps({
            "text": "",
            "mode": config.get("mode", "offline"),
            "is_final": True,
            "timestamp": None,
        }))
        return

    # Ensure even number of bytes for int16 conversion
    if buf_bytes % 2 != 0:
        audio_buffer = audio_buffer[:-1]
        buf_bytes -= 1

    # Convert PCM16 → float32 in [-1, 1]
    audio_int16 = np.frombuffer(bytes(audio_buffer), dtype=np.int16)
    audio_float32 = audio_int16.astype(np.float32) / 32768.0
    use_itn = config.get("itn", False)

    # 解析这段音频归属的对话会话：握手消息带 sessionid，
    # 通过活动会话指针反查 conv_id —— 与 /human 的归属逻辑保持一致，
    # 保证音频文件与消息记录落在同一个会话目录下
    conv_id = ""
    try:
        sid = str(config.get("sessionid", "") or "")
        if sid:
            from agent.conversation_store import get_active_conversation
            conv_id = get_active_conversation(sid)
    except Exception as e:
        logger.warning(f"[ASR] 解析音频归属会话失败: {e}")

    # Offload blocking inference to a thread —— 说话人判定与 ASR 识别并行执行，
    # 互不阻塞：说话人判定实测约 19ms，且失败不影响识别结果
    loop = asyncio.get_event_loop()
    audio_rel = ""
    speaker_info = {"is_self": False, "speaker": "", "conf": 0.0, "source": ""}
    try:
        fut_asr = loop.run_in_executor(
            None, _run_inference, audio_float32, SAMPLE_RATE, use_itn, conv_id)
        fut_spk = loop.run_in_executor(
            None, _identify_speaker_safe, conv_id, audio_float32, SAMPLE_RATE)
        (text, inference_ms, audio_dur, audio_rel), speaker_info = await asyncio.gather(
            fut_asr, fut_spk)
    except Exception as e:
        logger.exception(f"[ASR] ❌ Inference failed: {e}")
        text = ""

    # Map the client mode to the response mode the frontend expects
    mode = config.get("mode", "offline")
    if mode == "2pass":
        response_mode = "2pass-offline"
    else:
        response_mode = mode  # "online" or "offline"

    await ws.send_str(json.dumps({
        "text": text,
        "mode": response_mode,
        "is_final": True,
        "timestamp": None,
        # 音频留存路径（相对 data/audio/）。前端会透传给 /human，
        # 由 LLM 链路写进消息记录，供说话人分离事后分析用
        "audio": audio_rel,
        # 实时说话人判定结果：speaker 为姓名或「说话人N」；
        # is_self=true 表示这段其实是数字人自己的声音（扬声器回灌），
        # 前端不应把它送给 LLM —— 从根源滤除回声自我打断
        "speaker": speaker_info.get("speaker", ""),
        "speaker_conf": speaker_info.get("conf", 0.0),
        "is_self": speaker_info.get("is_self", False),
    }))
    logger.info(f"[ASR] 📤 Result sent to client (mode={response_mode})")


# ─── Availability Check ───────────────────────────────────────────────────

def is_funasr_available() -> bool:
    """Return True if the ``funasr`` package is importable."""
    try:
        import funasr  # noqa: F401
        return True
    except ImportError:
        return False
