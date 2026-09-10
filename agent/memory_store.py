###############################################################################
#  RAG 记忆存储 — 对话历史向量化 + 文件存储 + 检索
#
#  利用 OpenAI 兼容接口的向量模型（bge-m3）和重排序模型（bge-reranker-v2-m3）
#  实现长期记忆检索。对话历史向量化后存文件（data/memory.json），重启不丢失。
#
#  设计约束：
#    - 网络 IO（embed / rerank）一律放在锁外，避免下游服务变慢时阻塞整条对话链路
#    - 记忆文件带 mtime 缓存，避免每次检索都全量解析（满配 500 条约 15MB）
#    - 检索有相关性阈值过滤，低于阈值的不注入上下文（阈值可在控制台调整）
###############################################################################

import os
import re
import json
import time
import threading
import urllib.request
import numpy as np

from dotenv import load_dotenv
load_dotenv()  # 加载 .env，确保 LLM API 配置可用

from utils.logger import logger

# 记忆文件路径
MEMORY_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "memory.json")

# 检索参数
TOP_K = 3          # 向量检索取 Top-K（降为 3，减少注入量）
RERANK_K = 2       # 返回 Top-K（无 rerank 时即取前 N 条）

# 存储参数（以下均为默认值，实际值从 llm_config.json 的 memory 段读取）
MAX_ENTRIES = 1000     # 记忆条数上限
DEDUP_SCORE = 0.95     # 余弦相似度高于此值视为重复内容，不重复存储

# memory 段全部可配置项及默认值
DEFAULT_MEMORY_CFG = {
    "min_score": 0.35,             # 检索相关性阈值，低于此值不注入上下文
    "max_entries": 1000,           # 记忆条数上限
    "decay_weight": 0.25,          # 近因在检索排序中的权重（剩余权重给相关性）
    "decay_half_life_days": 30,    # 近因半衰期（天）
    "min_len_user": 6,             # 用户话术进记忆的最短长度
    "min_len_assistant": 6,        # 数字人回复进记忆的最短长度
    "filter_greetings": True,      # 是否过滤寒暄客套
    "rerank_enabled": False,       # 是否启用 rerank 重排序（默认关，省一次网络请求）
    "cache_ttl": 30,               # 检索结果缓存秒数（同 query 复用，避免重复请求）
}

# 寒暄/客套：没有长期回忆价值，全量存进去只会稀释记忆密度
_GREETINGS = frozenset({
    "你好", "您好", "嗨", "哈喽", "早上好", "中午好", "下午好", "晚上好",
    "谢谢", "感谢", "多谢", "辛苦了", "再见", "拜拜", "好的", "好", "嗯", "嗯嗯",
    "哦", "啊", "是的", "对", "不对", "明白", "明白了", "知道了", "收到", "行",
    "ok", "okay", "hi", "hello", "hey", "thanks", "thankyou", "bye", "goodbye",
})

# 可重入：内部函数会互相调用（_load_memory_cached → _load_memory）
_lock = threading.RLock()

# 记忆文件缓存：仅在文件 mtime 变化时重新解析
_mem_cache = None
_mem_mtime = 0.0

# 检索结果缓存：{query: (timestamp, result)}，同 query 在 TTL 内直接复用，避免重复网络请求
_search_cache = {}


def _get_llm_config():
    """从环境变量读取 LLM API 配置。"""
    return {
        "base_url": os.getenv("LLM_BASE_URL", ""),
        "api_key": os.getenv("LLM_API_KEY", ""),
        "embed_model": os.getenv("LLM_EMBED_MODEL", "bge-m3"),
        "rerank_model": os.getenv("LLM_RERANK_MODEL", "bge-reranker-v2-m3"),
    }


def _get_memory_cfg() -> dict:
    """读取记忆模块配置（llm_config.json 的 memory 段）。

    所有参数都可在控制台通过 /api/llm/config 调整，不硬编码。
    缺失项回落到 DEFAULT_MEMORY_CFG。
    """
    try:
        from agent.llm_router import _load_llm_config
        raw = _load_llm_config().get("memory", {})
    except Exception:
        raw = {}
    merged = dict(DEFAULT_MEMORY_CFG)
    if isinstance(raw, dict):
        merged.update({k: v for k, v in raw.items() if v is not None})
    return merged


def _is_low_value(text: str, role: str, cfg: dict) -> bool:
    """写入门控：判断这条内容是否值得进长期记忆。

    流水账式的极短回复和寒暄客套没有回忆价值，全量存进去只会稀释记忆密度。
    门控在向量化之前执行 —— 被过滤的内容不会产生任何网络请求开销。
    """
    t = (text or "").strip()
    if not t:
        return True

    raw_min = cfg.get("min_len_assistant") if role == "assistant" else cfg.get("min_len_user")
    try:
        min_len = int(raw_min or 0)
    except (TypeError, ValueError):
        min_len = 0
    if len(t) < min_len:
        return True

    if cfg.get("filter_greetings"):
        # 去掉标点与空白后再比对，避免「你好！」「好的。」这类漏网
        stripped = re.sub(r"[^\w\u4e00-\u9fff]", "", t).lower()
        if not stripped or stripped in _GREETINGS:
            return True
    return False


def _recency_score(timestamp: float, half_life_days: float) -> float:
    """近因分数：按半衰期指数衰减，取值 (0, 1]。

    只用于检索排序，让老记忆自然淡出而不是被硬删除 —— 真需要时仍然能召回。
    """
    try:
        half_life = max(float(half_life_days or 0), 1e-6) * 86400.0
    except (TypeError, ValueError):
        half_life = 30.0 * 86400.0
    age = max(time.time() - float(timestamp or 0), 0.0)
    return float(2.0 ** (-age / half_life))


def _embed(texts):
    """调用 bge-m3 向量模型，返回向量列表。"""
    cfg = _get_llm_config()
    payload = {"model": cfg["embed_model"], "input": texts}
    req = urllib.request.Request(
        cfg["base_url"] + "/embeddings",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read())
        return [item["embedding"] for item in data.get("data", [])]
    except Exception as e:
        logger.warning(f"[Memory] embeddings 失败: {e}")
        return []


def _rerank(query, documents):
    """调用 bge-reranker 重排序，返回排序后的文档索引。"""
    cfg = _get_llm_config()
    payload = {"model": cfg["rerank_model"], "query": query, "documents": documents}
    req = urllib.request.Request(
        cfg["base_url"] + "/rerank",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read())
        results = sorted(data.get("results", []), key=lambda x: x.get("relevance_score", 0), reverse=True)
        return [r["index"] for r in results]
    except Exception as e:
        logger.warning(f"[Memory] rerank 失败: {e}")
        return list(range(len(documents)))


def _cosine_similarity(a, b):
    """计算余弦相似度。"""
    a = np.asarray(a, dtype=np.float32).ravel()
    b = np.asarray(b, dtype=np.float32).ravel()
    if a.size == 0 or b.size == 0 or a.shape != b.shape:
        return 0.0
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom < 1e-9:
        return 0.0
    return float(np.dot(a, b) / denom)


def _load_memory():
    """读取记忆文件（无缓存，直接读盘）。"""
    try:
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"[Memory] 读取记忆失败: {e}")
    return {"entries": []}


def _load_memory_cached():
    """带 mtime 缓存的记忆读取：文件未变化则复用内存副本。"""
    global _mem_cache, _mem_mtime
    try:
        mtime = os.path.getmtime(MEMORY_FILE)
    except OSError:
        mtime = 0.0

    if _mem_cache is not None and mtime == _mem_mtime:
        return _mem_cache

    with _lock:
        # 双重检查：并发下可能已有其他线程完成加载
        try:
            mtime = os.path.getmtime(MEMORY_FILE)
        except OSError:
            mtime = 0.0
        if _mem_cache is not None and mtime == _mem_mtime:
            return _mem_cache
        data = _load_memory()
        _mem_cache = data
        _mem_mtime = mtime
        return data


def _save_memory(data):
    """保存记忆文件并同步缓存。"""
    global _mem_cache, _mem_mtime
    try:
        os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        _mem_cache = data
        _mem_mtime = os.path.getmtime(MEMORY_FILE)
    except Exception as e:
        logger.exception(f"[Memory] 保存记忆失败: {e}")


def add_memory(text: str, role: str = "user", session_id: str = "") -> bool:
    """添加一条对话记忆（向量化后存储）。返回是否真正写入。

    向量化是网络请求，放在锁外执行；只有文件读改写才持锁。
    """
    text = (text or "").strip()
    if not text:
        return False

    cfg = _get_memory_cfg()

    # 写入门控：在向量化之前过滤。被拦下的内容不产生任何网络请求开销
    if _is_low_value(text, role, cfg):
        logger.info(f"[Memory] 门控过滤，不写入（role={role}）: {text[:30]}")
        return False

    vectors = _embed([text])  # 锁外：网络 IO
    if not vectors:
        logger.warning("[Memory] 向量化失败，跳过记忆存储")
        return False
    vec = vectors[0]

    with _lock:
        data = _load_memory_cached()
        entries = data.setdefault("entries", [])

        # 去重：与已有记忆高度相似则跳过，避免同一句话反复存储
        for e in entries:
            if _cosine_similarity(vec, e.get("vector", [])) >= DEDUP_SCORE:
                return False

        entries.append({
            "text": text,
            "role": role,
            "session_id": session_id,
            "vector": vec,
            "timestamp": time.time(),
        })

        # 淘汰：超出上限时按时间丢弃最老的。有了写入门控，这里只是兜底
        try:
            max_entries = int(cfg.get("max_entries", MAX_ENTRIES) or MAX_ENTRIES)
        except (TypeError, ValueError):
            max_entries = MAX_ENTRIES
        if len(entries) > max_entries:
            entries.sort(key=lambda e: e.get("timestamp", 0))
            del entries[:len(entries) - max_entries]

        _save_memory(data)
    return True


def search_memory(query: str, top_k: int = TOP_K, rerank_k: int = RERANK_K) -> list:
    """检索相关记忆：向量化 → 余弦相似度 → 阈值过滤 → Top-K（可选重排序）。

    两段网络请求（embed / rerank）都在锁外，锁内只做纯计算。
    默认关闭 rerank、启用 30s 缓存，减少对 vLLM 的重复请求。

    返回 [(text, role), ...]。记忆里同时存有用户和数字人说过的话，
    必须把角色一并返回，否则调用方无法区分「用户曾说」和「你曾回答」。
    """
    query = (query or "").strip()
    if not query:
        return []
    try:
        cfg = _get_memory_cfg()
        ttl = float(cfg.get("cache_ttl", 30) or 0)
    except Exception:
        cfg, ttl = {}, 30.0
    # 缓存命中：同 query 在 TTL 内直接复用，避免重复 embed/rerank 网络请求
    if ttl > 0:
        cached = _search_cache.get(query)
        if cached and (time.time() - cached[0]) < ttl:
            return cached[1]

    query_vectors = _embed([query])  # 锁外：网络 IO
    if not query_vectors:
        return []
    query_vec = query_vectors[0]

    with _lock:
        entries = _load_memory_cached().get("entries", [])
        if not entries:
            return []

        cfg = _get_memory_cfg()

        scored = []
        for i, entry in enumerate(entries):
            vec = entry.get("vector")
            if vec:
                scored.append((i, _cosine_similarity(query_vec, vec)))

        # 相关性阈值过滤：低于阈值的不注入上下文，避免噪音污染。
        # 这里过滤的是纯相关性，保持 min_score 原有语义不受衰减影响。
        scored = [(i, s) for i, s in scored if s >= cfg.get("min_score", 0.35)]
        if not scored:
            return []

        # 排序用「相关性 + 近因」加权：让老记忆自然淡出，而不是被硬删除
        try:
            decay_w = float(cfg.get("decay_weight", 0.25) or 0.0)
        except (TypeError, ValueError):
            decay_w = 0.25
        if decay_w > 0:
            half_life = cfg.get("decay_half_life_days", 30)
            scored.sort(
                key=lambda x: (1.0 - decay_w) * x[1]
                              + decay_w * _recency_score(entries[x[0]].get("timestamp", 0), half_life),
                reverse=True,
            )
        else:
            scored.sort(key=lambda x: x[1], reverse=True)

        # 同时取出文本与角色，后续重排序无需再访问 entries
        top = [(entries[i].get("text", ""), entries[i].get("role", "user"))
               for i, _ in scored[:top_k]]

    # 重排序（锁外：网络 IO）—— 默认关闭 rerank 以省一次网络请求；如需更精准，在 cfg 打开
    if cfg.get("rerank_enabled"):
        reranked = _rerank(query, [t for t, _ in top])
        result = [top[i] for i in reranked[:rerank_k] if i < len(top)]
    else:
        # 无 rerank：直接返回余弦排序后的 Top-K（去掉 rerank 这一路网络延迟）
        result = top[:rerank_k]
    # 写入缓存（同 query 在 TTL 内复用）
    if ttl > 0:
        with _lock:
            _search_cache[query] = (time.time(), result)
    return result


def get_all_memory() -> list:
    """获取所有记忆（不含向量）。"""
    with _lock:
        entries = _load_memory_cached().get("entries", [])
        return [
            {
                "text": e.get("text", ""),
                "role": e.get("role", ""),
                "session_id": e.get("session_id", ""),
                "timestamp": e.get("timestamp", 0),
            }
            for e in entries
        ]


def clear_memory() -> bool:
    """清空记忆。"""
    with _lock:
        _save_memory({"entries": []})
        return True


# ─── 路由处理 ───────────────────────────────────────────────

async def get_memory(request):
    """获取所有记忆。"""
    from aiohttp import web
    return web.json_response({"code": 0, "msg": "ok", "data": {"entries": get_all_memory()}})


async def delete_memory(request):
    """清空记忆。"""
    from aiohttp import web
    clear_memory()
    return web.json_response({"code": 0, "msg": "ok"})


def setup_memory_routes(app):
    """注册记忆相关路由。"""
    from aiohttp import web
    app.router.add_get("/api/memory", get_memory)
    app.router.add_delete("/api/memory", delete_memory)
