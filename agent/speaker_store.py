###############################################################################
#  说话人分离 — 实时增量识别 + 声纹库
#
#  基于 funasr 内置的 CAM++（iic/speech_campplus_sv_zh-cn_16k-common）提取
#  192 维声纹向量。实测：预热后单次提取约 19ms，同人相似度 0.69 vs 异人 ≈0。
#
#  两层能力（与离线精修 speaker_diarize 配合）：
#    1. 声纹库：姓名 ↔ 向量，持久化到 data/speakers.json，跨会话生效
#    2. 会话内临时中心：未注册的人在本次会话内保持「说话人N」标签，
#       中心向量随发言滑动平均，越说越准；会话结束后可命名入库
#
#  数字人自身（role="avatar"）：把 TTS 参考音频注册为特殊声纹，
#  实时识别命中即判定为回声 —— 从根源解决数字人自我打断。
#
#  约定（沿用 memory_store 风格）：
#    - 模型推理（GPU）在锁外执行，只有文件读改写持锁
#    - 阈值等参数写进 llm_config.json 的 speaker 段，控制台可调
###############################################################################

import os
import json
import time
import uuid
import threading
import numpy as np

from dotenv import load_dotenv
load_dotenv()

from utils.logger import logger

# 声纹库文件（data 目录位于项目根）
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
SPEAKER_FILE = os.path.join(DATA_DIR, "speakers.json")

# 数字人自身的 TTS 参考音频（GPT-SoVITS 音色克隆的源音频）
AVATAR_REF_AUDIO = os.getenv(
    "AVATAR_REF_AUDIO",
    os.path.join(DATA_DIR, "avatar_ref_audio.wav"),
)

# speaker 段全部可配置项及默认值
DEFAULT_SPEAKER_CFG = {
    "enabled": True,               # 总开关：关闭后实时链路完全跳过说话人判定
    "match_threshold": 0.55,       # 声纹库匹配阈值（同人实测 0.69，异人 ≈0）
    "session_threshold": 0.65,     # 会话内临时中心匹配阈值（足够高，避免不同人错误合并）
    "self_filter_enabled": True,   # 是否滤除数字人自己的声音（回声自我打断）
    "self_filter_threshold": 0.55, # 自身判定阈值（足够高，避免把其他人声误判为回声）
    "min_audio_ms": 600,           # 短于此的音频不做判定（embedding 不可靠）
    "avatar_ref_audio": AVATAR_REF_AUDIO,
}

# 可重入锁：文件读改写用；模型推理永远在锁外
_lock = threading.RLock()

# 声纹库缓存（mtime 变化时重载）
_spk_cache = None
_spk_mtime = 0.0

# CAM++ 模型（懒加载，独立于实时 ASR 模型）
_model = None
_model_lock = threading.Lock()

# 数字人自身声纹是否已就绪
_avatar_ready = False

# 会话内临时说话人中心（内存态，不落盘）：
# { conv_id: [ {"label": "说话人2", "vector": np.ndarray(192), "count": 3} ] }
_session_centers = {}


# ─── 配置 ───────────────────────────────────────────────────

def _get_speaker_cfg() -> dict:
    """读取说话人模块配置（llm_config.json 的 speaker 段）。"""
    try:
        from agent.llm_router import _load_llm_config
        raw = _load_llm_config().get("speaker", {})
    except Exception:
        raw = {}
    merged = dict(DEFAULT_SPEAKER_CFG)
    if isinstance(raw, dict):
        merged.update({k: v for k, v in raw.items() if v is not None})
    return merged


# ─── 模型 ───────────────────────────────────────────────────

def _load_model():
    """懒加载 CAM++ 说话人模型（线程安全，只加载一次）。"""
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        from funasr import AutoModel
        t0 = time.perf_counter()
        _model = AutoModel(
            model="iic/speech_campplus_sv_zh-cn_16k-common",
            device="cuda:0",
            disable_update=True,
        )
        logger.info(f"[Speaker] CAM++ 模型加载完成（{time.perf_counter()-t0:.1f}s）")
        return _model


def extract_embedding(audio: np.ndarray, sample_rate: int = 16000) -> np.ndarray:
    """提取一段音频的声纹向量（192 维，已 L2 归一化）。

    GPU 推理在锁外执行。失败返回 None。
    """
    try:
        audio = np.asarray(audio, dtype=np.float32).ravel()
        if len(audio) < sample_rate * 0.3:
            return None
        m = _load_model()
        res = m.generate(input=audio)
        vec = res[0]["spk_embedding"]
        import torch
        vec = torch.nn.functional.normalize(vec.float().flatten(), dim=0)
        return vec.cpu().numpy()
    except Exception as e:
        logger.warning(f"[Speaker] 声纹提取失败: {e}")
        return None


# ─── 声纹库 ─────────────────────────────────────────────────

def _load_speakers() -> dict:
    """读取声纹库（直接读盘）。"""
    try:
        if os.path.exists(SPEAKER_FILE):
            with open(SPEAKER_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("speakers", [])
                    return data
    except Exception as e:
        logger.warning(f"[Speaker] 读取声纹库失败: {e}")
    return {"speakers": []}


def _load_speakers_cached() -> dict:
    """带 mtime 缓存的声纹库读取。"""
    global _spk_cache, _spk_mtime
    try:
        mtime = os.path.getmtime(SPEAKER_FILE)
    except OSError:
        mtime = 0.0
    if _spk_cache is not None and mtime == _spk_mtime:
        return _spk_cache
    with _lock:
        try:
            mtime = os.path.getmtime(SPEAKER_FILE)
        except OSError:
            mtime = 0.0
        if _spk_cache is not None and mtime == _spk_mtime:
            return _spk_cache
        _spk_cache = _load_speakers()
        _spk_mtime = mtime
        return _spk_cache


def _save_speakers(data: dict):
    """保存声纹库并同步缓存。"""
    global _spk_cache, _spk_mtime
    with _lock:
        try:
            os.makedirs(os.path.dirname(SPEAKER_FILE), exist_ok=True)
            with open(SPEAKER_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            _spk_cache = data
            _spk_mtime = os.path.getmtime(SPEAKER_FILE)
        except Exception as e:
            logger.exception(f"[Speaker] 保存声纹库失败: {e}")


def _cosine(a, b) -> float:
    """两个向量的余弦相似度（输入应已归一化）。"""
    a = np.asarray(a, dtype=np.float32).ravel()
    b = np.asarray(b, dtype=np.float32).ravel()
    if a.size == 0 or b.size == 0 or a.shape != b.shape:
        return -1.0
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom < 1e-9:
        return -1.0
    return float(np.dot(a, b) / denom)


def list_speakers() -> list:
    """声纹库列表（不含向量本体）。"""
    with _lock:
        data = _load_speakers_cached()
        out = []
        for s in data.get("speakers", []):
            out.append({
                "id": s.get("id", ""),
                "name": s.get("name", ""),
                "role": s.get("role", "human"),
                "created_at": s.get("created_at", 0),
                "source": s.get("source", ""),
            })
        return out


def register_speaker(name: str, audio=None, audio_path: str = "",
                     role: str = "human", source: str = "manual") -> dict:
    """注册一个声纹。

    audio 为 float32 numpy 数组或 None；audio_path 为音频文件路径（二选一）。
    role="avatar" 表示数字人自身的 TTS 声音（用于回声滤除，不参与普通命名）。
    返回注册条目；失败返回 None。
    """
    name = (name or "").strip()
    if not name:
        return None

    # 取向量：优先直接给向量来源（如离线聚类的 centroid），否则从音频提取
    vector = None
    if isinstance(audio, np.ndarray):
        vector = extract_embedding(audio)
    elif audio_path and os.path.exists(audio_path):
        try:
            import soundfile as sf
            wav, fs = sf.read(audio_path, dtype="float32")
            if fs != 16000:
                return None
            vector = extract_embedding(wav)
        except Exception as e:
            logger.warning(f"[Speaker] 读取注册音频失败: {e}")
    if vector is None:
        return None

    entry = {
        "id": f"spk_{uuid.uuid4().hex[:12]}",
        "name": name,
        "role": role,
        "vector": [round(float(x), 6) for x in vector],
        "created_at": time.time(),
        "source": source,
    }
    with _lock:
        data = _load_speakers_cached()
        speakers = data.setdefault("speakers", [])
        # 同名（且同 role）覆盖更新，避免重复注册
        speakers = [s for s in speakers if not (s.get("name") == name and s.get("role") == role)]
        speakers.append(entry)
        data["speakers"] = speakers
        _save_speakers(data)
    logger.info(f"[Speaker] 已注册声纹: {name} (role={role}, source={source})")
    return {k: v for k, v in entry.items() if k != "vector"}


def delete_speaker(spk_id: str) -> bool:
    """删除声纹库条目。"""
    with _lock:
        data = _load_speakers_cached()
        speakers = data.get("speakers", [])
        new = [s for s in speakers if s.get("id") != spk_id]
        if len(new) == len(speakers):
            return False
        data["speakers"] = new
        _save_speakers(data)
        return True


def match_speaker(vector, threshold: float = None) -> tuple:
    """把向量与声纹库比对。返回 (姓名, 相似度, role)；无命中返回 (None, best_score, None)。"""
    if vector is None:
        return None, 0.0, None
    if threshold is None:
        threshold = _get_speaker_cfg()["match_threshold"]
    with _lock:
        speakers = _load_speakers_cached().get("speakers", [])
    best_name, best_score, best_role = None, -1.0, None
    for s in speakers:
        score = _cosine(vector, s.get("vector", []))
        if score > best_score:
            best_name, best_score, best_role = s.get("name", ""), score, s.get("role", "human")
    if best_score >= threshold:
        return best_name, best_score, best_role
    # 未命中：role 固定为 None（库里最高分条目的 role 无意义，避免误判）
    return None, best_score, None


# ─── 数字人自身声纹（回声滤除） ──────────────────────────────

def ensure_avatar_voiceprint() -> bool:
    """确保数字人自身的 TTS 声纹已注册（幂等，首次调用时从参考音频提取）。

    命中该声纹的音频段即判定为「数字人自己在说话」（扬声器回灌），
    实时链路可直接丢弃，从根源解决回声自我打断。
    """
    global _avatar_ready
    if _avatar_ready:
        return True
    cfg = _get_speaker_cfg()
    if not cfg.get("self_filter_enabled"):
        return False
    # 已注册过就不再重复提取
    with _lock:
        for s in _load_speakers_cached().get("speakers", []):
            if s.get("role") == "avatar":
                _avatar_ready = True
                return True
    ref = cfg.get("avatar_ref_audio", AVATAR_REF_AUDIO)
    if not ref or not os.path.exists(ref):
        logger.warning(f"[Speaker] 数字人参考音频不存在，自身滤除未启用: {ref}")
        return False
    entry = register_speaker(name="数字人", audio_path=ref, role="avatar", source="auto")
    _avatar_ready = entry is not None
    return _avatar_ready


def is_avatar_voice(audio: np.ndarray, sample_rate: int = 16000) -> bool:
    """判定这段音频是不是数字人自己的声音（扬声器回灌的回声）。

    与 identify_speaker 的区别：只回答「是/不是数字人」，不参与会话说话人
    统计、不依赖会话 id —— 供实时 VAD 的回声门控调用（audio 可以是很短的一段）。
    """
    cfg = _get_speaker_cfg()
    if not cfg.get("self_filter_enabled"):
        return False
    try:
        ensure_avatar_voiceprint()
    except Exception as e:
        logger.warning(f"[Speaker] 自身声纹初始化失败: {e}")
        return False
    vector = extract_embedding(audio, sample_rate)
    if vector is None:
        return False
    threshold = cfg.get("self_filter_threshold", 0.35)
    with _lock:
        avatars = [s.get("vector", []) for s in _load_speakers_cached().get("speakers", [])
                   if s.get("role") == "avatar"]
    return any(_cosine(vector, av) >= threshold for av in avatars)


def update_avatar_voiceprint(audio: np.ndarray, sample_rate: int = 16000) -> bool:
    """用数字人实际播放的一句话 TTS 音频更新/注册自身声纹（自适应）。

    与 ensure_avatar_voiceprint 的区别：不依赖外部参考音频文件，而是直接用
    数字人刚刚真实播出的声音提取声纹。无论参考音频/音色如何变化，都能让
    数字人的回声被 is_self 准确命中。

    仅在 self_filter_enabled 且音频足够长（>=0.3s）时执行；失败不阻断播放。
    返回是否更新成功。
    """
    cfg = _get_speaker_cfg()
    if not cfg.get("self_filter_enabled"):
        return False
    audio = np.asarray(audio, dtype=np.float32).ravel()
    if len(audio) < sample_rate * 0.3:
        return False
    # 提取声纹（GPU 推理，锁外执行）。
    vector = extract_embedding(audio, sample_rate)
    if vector is None:
        return False
    with _lock:
        data = _load_speakers_cached()
        speakers = data.setdefault("speakers", [])
        entry = {
            "id": f"spk_{uuid.uuid4().hex[:12]}",
            "name": "数字人",
            "role": "avatar",
            "vector": [round(float(x), 6) for x in vector],
            "created_at": time.time(),
            "source": "auto_tts",
        }
        # 覆盖更新旧的 avatar 声纹，只保留最新一条，避免重复
        speakers = [s for s in speakers if s.get("role") != "avatar"]
        speakers.append(entry)
        data["speakers"] = speakers
        _save_speakers(data)
    logger.info("[Speaker] 已用实时 TTS 音频更新数字人自身声纹（自适应）")
    return True


# ─── 实时增量识别 ───────────────────────────────────────────

def reset_session(conv_id: str):
    """清空某个会话的临时说话人中心（会话切换/清空时调用）。"""
    with _lock:
        _session_centers.pop(conv_id, None)


def identify_speaker(conv_id: str, audio: np.ndarray, sample_rate: int = 16000) -> dict:
    """实时增量识别：判定这段音频是谁说的。

    判定顺序（短路返回）：
      1. enabled=false / 音频太短        -> {"speaker": "", "source": ""}
      2. 命中数字人自身声纹               -> {"is_self": True, ...}（调用方应丢弃该段）
      3. 命中全局声纹库                   -> {"speaker": "张三", "source": "voiceprint"}
      4. 命中会话内临时中心               -> {"speaker": "说话人N", "source": "session"}
      5. 新建临时中心                     -> {"speaker": "说话人N", "source": "new"}

    GPU 推理在锁外；只有临时中心的读改写持锁。
    """
    cfg = _get_speaker_cfg()
    result = {"is_self": False, "speaker": "", "conf": 0.0, "source": ""}
    if not cfg.get("enabled"):
        return result

    dur_ms = len(audio) * 1000.0 / sample_rate
    if dur_ms < cfg.get("min_audio_ms"):
        return result

    # ── 提取声纹（锁外，GPU 推理）──
    vector = extract_embedding(audio, sample_rate)
    if vector is None:
        return result

    # ── 1) 数字人自身判定（回声滤除）──
    if cfg.get("self_filter_enabled"):
        try:
            ensure_avatar_voiceprint()
        except Exception as e:
            logger.warning(f"[Speaker] 自身声纹初始化失败: {e}")
        with _lock:
            avatar_vecs = [s.get("vector", []) for s in _load_speakers_cached().get("speakers", [])
                           if s.get("role") == "avatar"]
        for av in avatar_vecs:
            if _cosine(vector, av) >= cfg.get("self_filter_threshold"):
                result.update({"is_self": True, "speaker": "数字人", "conf": _cosine(vector, av),
                               "source": "avatar"})
                logger.info("[Speaker] 检测到数字人自身声音（回声），已标记滤除")
                return result

    # ── 2) 全局声纹库 ──
    name, score, role = match_speaker(vector, cfg.get("match_threshold"))
    if name and role == "human":
        result.update({"speaker": name, "conf": round(score, 4), "source": "voiceprint"})
        return result

    # ── 3) 会话内临时中心 ──
    threshold = cfg.get("session_threshold")
    with _lock:
        centers = _session_centers.setdefault(conv_id, [])
        best_idx, best_score = -1, -1.0
        for i, c in enumerate(centers):
            s = _cosine(vector, c["vector"])
            if s > best_score:
                best_idx, best_score = i, s
        if best_idx >= 0 and best_score >= threshold:
            c = centers[best_idx]
            # 滑动平均更新中心：发言越多，中心越准
            n = c["count"]
            c["vector"] = ((np.asarray(c["vector"], dtype=np.float32) * n + vector) / (n + 1)).tolist()
            c["count"] = n + 1
            result.update({"speaker": c["label"], "conf": round(best_score, 4), "source": "session"})
            return result
        # 新建临时中心
        label = f"说话人{len(centers) + 1}"
        centers.append({"label": label, "vector": vector.tolist(), "count": 1})
        result.update({"speaker": label, "conf": 1.0, "source": "new"})
        logger.info(f"[Speaker] 会话 {conv_id[:8]} 检测到新说话人: {label}")
        return result


def set_session_centers(key: str, centers: list):
    """写入会话内说话人中心（供离线精修模块调用，避免外部直接改私有变量）。"""
    with _lock:
        _session_centers[key] = centers


def _load_clusters_from_disk(conv_id: str) -> bool:
    """离线簇中心不在内存时（如服务重启后），从磁盘恢复。

    返回是否恢复成功。延迟导入 speaker_diarize，避免模块级循环依赖。
    """
    with _lock:
        if _session_centers.get(f"__diarize__{conv_id}"):
            return True
    try:
        from agent.speaker_diarize import load_clusters
        clusters = load_clusters(conv_id)
    except Exception:
        return False
    if not clusters:
        return False
    with _lock:
        if not _session_centers.get(f"__diarize__{conv_id}"):
            _session_centers[f"__diarize__{conv_id}"] = clusters
    return True


def get_session_speakers(conv_id: str) -> list:
    """获取某会话当前识别出的说话人（供会后命名界面使用）。

    优先返回离线精修的簇（更准），没有则退回到实时临时中心。
    内存里没有时（服务重启过）尝试从磁盘恢复。
    """
    if not _session_centers.get(f"__diarize__{conv_id}") and not _session_centers.get(conv_id):
        _load_clusters_from_disk(conv_id)
    with _lock:
        centers = _session_centers.get(f"__diarize__{conv_id}")
        if not centers:
            centers = _session_centers.get(conv_id, [])
        return [{"label": c["label"], "count": c.get("count", 1)} for c in centers]


def name_speaker(conv_id: str, label: str, name: str) -> bool:
    """给会话内的说话人命名并注册进全局声纹库。

    优先匹配离线精修的簇（其 centroid 由多段平均而来，更可靠），
    匹配不到再退回到实时的会话临时中心。
    """
    # 服务重启后簇中心只在磁盘上，先恢复，否则命名会报「未找到该说话人」
    _load_clusters_from_disk(conv_id)
    for key in (f"__diarize__{conv_id}", conv_id):
        if name_session_speaker(key, label, name):
            return True
    return False


def name_session_speaker(conv_id: str, label: str, name: str,
                          centroid: list = None) -> bool:
    """给会话内临时说话人命名，并把其中心向量注册进全局声纹库。

    下次会议同一个人再说话，就能直接叫出名字 —— 无需重复注册。
    """
    name = (name or "").strip()
    label = (label or "").strip()
    if not name or not conv_id:
        return False
    with _lock:
        centers = _session_centers.get(conv_id, [])
        target = None
        for c in centers:
            if c["label"] == label:
                target = c
                break
    if target is None and centroid is None:
        return False
    # 直接把中心向量写入全局声纹库（发言越多中心越可信）
    vec = centroid if centroid is not None else target["vector"]
    if target is not None:
        # 同步更新内存里的标签，避免用旧标签重复命名、列表仍显示旧名
        target["label"] = name
    with _lock:
        data = _load_speakers_cached()
        speakers = data.setdefault("speakers", [])
        # 同名覆盖，避免重复
        speakers = [s for s in speakers if not (s.get("name") == name and s.get("role") == "human")]
        speakers.append({
            "id": f"spk_{uuid.uuid4().hex[:12]}",
            "name": name,
            "role": "human",
            "vector": [round(float(x), 6) for x in vec],
            "created_at": time.time(),
            "source": "session",
        })
        data["speakers"] = speakers
        _save_speakers(data)
    logger.info(f"[Speaker] 会话说话人 '{label}' 已命名为 '{name}' 并注册声纹")
    return True
