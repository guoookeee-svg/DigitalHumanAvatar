###############################################################################
#  离线说话人精修 — 会后全局聚类修正
#
#  实时路径（speaker_store.identify_speaker）是增量匹配：早期样本少不稳定，
#  且一旦判错无法回头修正。离线路径在会后拿到全部音频，做一次全局聚类修正。
#
#  两条路径共用同一套声纹库与音频留存数据。
#
#  ─── 为什么不用 funasr 自带的 spk_model 聚类 ────────────────────────────
#  实测：funasr 的 ClusterBackend 在「2 人交替说话」场景下切出 8-10 个簇，
#  调整 vad_kwargs.max_single_segment_time（30000/10000/5000/3000ms）均无效。
#  对照实验证明问题不在 CAM++ —— 同一批音频用 CAM++ 自提 embedding +
#  scipy 层次聚类，3 人场景纯度 100%。
#
#  因此改为：逐段 CAM++ 提 embedding + scipy 层次聚类（距离阈值自适应簇数）。
#  附带收益：不受 funasr 聚类器「≥20 段语音」的门槛限制，2 段即可聚类。
#
#  实测阈值表现（3 人场景）：0.5~0.8 均为「簇数=3、纯度 100%」，取 0.55。
###############################################################################

import os
import json
import time
import threading

from dotenv import load_dotenv
load_dotenv()

from utils.logger import logger

# 音频留存根目录（与 server/asr_server.py 的 AUDIO_ROOT 保持一致）
AUDIO_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "audio")

# 层次聚类的距离阈值（距离 = 1 - 余弦相似度）
# 同人短段相似度实测 0.55~0.65（距离 0.35~0.45），异人≈0（距离≈1）
CLUSTER_DISTANCE_THRESHOLD = 0.55

# 低于此段数不做聚类（样本太少，聚类无意义，保留实时标签）
MIN_SEGMENTS = 3

_lock = threading.RLock()

# 分析任务状态：{ conv_id: {"status", "speakers", "updated", "msg"} }
_jobs = {}


def _get_cfg() -> dict:
    """读取配置（阈值可从 llm_config.json 的 speaker 段覆盖）。"""
    cfg = {
        "distance_threshold": CLUSTER_DISTANCE_THRESHOLD,
        "min_segments": MIN_SEGMENTS,
    }
    try:
        from agent.llm_router import _load_llm_config
        raw = _load_llm_config().get("speaker", {}) or {}
        if isinstance(raw.get("distance_threshold"), (int, float)):
            cfg["distance_threshold"] = float(raw["distance_threshold"])
        if isinstance(raw.get("min_segments"), int):
            cfg["min_segments"] = raw["min_segments"]
    except Exception:
        pass
    return cfg


def get_status(conv_id: str) -> dict:
    """查询某个会话的离线分析状态。"""
    with _lock:
        return dict(_jobs.get(conv_id, {"status": "idle"}))


def _safe_audio_path(rel: str) -> str:
    """把消息里的相对音频路径解析为绝对路径，拒绝越界路径。

    落盘侧（asr_server._save_audio_clip）会过滤 conv_id，这里读取侧同样要校验，
    防止消息记录被篡改后读到 data/audio 以外的文件。
    """
    if not rel:
        return ""
    s = str(rel)
    # 含 ".." 或绝对路径一律拒绝（不做静默改写，避免掩盖数据异常）
    if s.startswith(("/", "\\")) or ".." in s.replace("\\", "/").split("/"):
        logger.warning(f"[Diarize] 音频路径非法，已拒绝: {rel}")
        return ""
    parts = [p for p in s.replace("\\", "/").split("/") if p and p != "."]
    if len(parts) != 2:  # 约定格式："{conv_id}/{unix_ms}.wav"
        return ""
    full = os.path.abspath(os.path.join(AUDIO_ROOT, parts[0], parts[1]))
    if os.path.commonpath([full, os.path.abspath(AUDIO_ROOT)]) != os.path.abspath(AUDIO_ROOT):
        logger.warning(f"[Diarize] 音频路径越界，已拒绝: {rel}")
        return ""
    return full


def _clusters_file(conv_id: str) -> str:
    """某会话的簇中心落盘路径。"""
    safe = "".join(c for c in str(conv_id) if c.isalnum() or c in "-_")
    return os.path.join(AUDIO_ROOT, safe, "clusters.json") if safe else ""


def _save_clusters(conv_id: str, clusters: list):
    """把聚类中心落盘 —— 服务重启后仍可命名，无需重跑分析。"""
    path = _clusters_file(conv_id)
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"conv_id": conv_id, "clusters": clusters,
                       "updated": time.time()}, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"[Diarize] 簇中心落盘失败: {e}")


def load_clusters(conv_id: str) -> list:
    """读取落盘的簇中心（供 speaker_store 在重启后恢复）。"""
    path = _clusters_file(conv_id)
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        clusters = data.get("clusters", [])
        return [c for c in clusters if isinstance(c, dict) and c.get("vector")]
    except Exception as e:
        logger.warning(f"[Diarize] 读取簇中心失败: {e}")
        return []


def _cluster(embeddings, distance_threshold: float):
    """层次聚类，返回每段的簇标签（0 起）。"""
    from scipy.cluster.hierarchy import linkage, fcluster
    from scipy.spatial.distance import squareform
    import numpy as np

    n = len(embeddings)
    if n == 1:
        return [0]
    sim = np.array([[float(np.dot(embeddings[i], embeddings[j])) for j in range(n)]
                    for i in range(n)])
    dist = squareform(np.clip(1.0 - sim, 0.0, None), checks=False)
    Z = linkage(dist, method="average")
    labels = fcluster(Z, t=distance_threshold, criterion="distance")
    # 归一化为 0 起的连续编号
    uniq = sorted(set(int(x) for x in labels))
    remap = {u: i for i, u in enumerate(uniq)}
    return [remap[int(x)] for x in labels]


def analyze_conversation(conv_id: str, force: bool = False) -> dict:
    """对某个会话的全部留存音频做一次全局说话人聚类，并回写标注。

    这是**阻塞**调用，请放在 run_in_executor 里执行。
    返回 {"status": "done"/"skipped"/"error", "speakers": [...], "msg": ...}
    """
    from agent.conversation_store import get_audio_messages, apply_speaker_results
    import agent.speaker_store as SP
    import numpy as np

    cfg = _get_cfg()

    with _lock:
        if not force and _jobs.get(conv_id, {}).get("status") == "running":
            return {"status": "running", "msg": "分析进行中"}

    def _set(**kw):
        with _lock:
            job = _jobs.setdefault(conv_id, {})
            job.update(kw)
            job["updated"] = time.time()

    _set(status="running", msg="")
    try:
        # 1) 取出所有带音频的用户消息
        msgs = get_audio_messages(conv_id)
        if len(msgs) < cfg["min_segments"]:
            msg = f"语音段不足（{len(msgs)} < {cfg['min_segments']}），跳过离线精修，保留实时标签"
            logger.info(f"[Diarize] {msg}")
            _set(status="skipped", msg=msg, speakers=[])
            return {"status": "skipped", "msg": msg, "speakers": []}

        # 2) 逐段提取声纹（GPU 推理在锁外）
        import soundfile as sf
        valid, embeddings = [], []
        for m in msgs:
            rel = m.get("audio", "")
            path = _safe_audio_path(rel)
            if not rel or not os.path.exists(path):
                logger.warning(f"[Diarize] 音频缺失，跳过: {rel}")
                continue
            try:
                wav, fs = sf.read(path, dtype="float32")
                if fs != 16000:
                    continue
                vec = SP.extract_embedding(wav)
                if vec is None:
                    continue
                valid.append(m)
                embeddings.append(vec)
            except Exception as e:
                logger.warning(f"[Diarize] 读取/提取失败 {rel}: {e}")

        if len(valid) < cfg["min_segments"]:
            msg = f"有效语音段不足（{len(valid)} < {cfg['min_segments']}），跳过离线精修"
            _set(status="skipped", msg=msg, speakers=[])
            return {"status": "skipped", "msg": msg, "speakers": []}

        # 3) 全局聚类
        labels = _cluster(embeddings, cfg["distance_threshold"])
        n_clusters = len(set(labels))
        logger.info(f"[Diarize] 会话 {conv_id[:8]}：{len(valid)} 段聚为 {n_clusters} 簇")

        # 4) 每簇算 centroid，与声纹库比对命名
        # 注意：match_speaker 未命中时返回的 role 是「库里最高分条目的角色」，
        # 只有 name 命中时 role 才有意义（避免把未命中的真人簇误判为数字人）
        cluster_names, cluster_vecs = {}, {}
        for c in range(n_clusters):
            members = [embeddings[i] for i, lb in enumerate(labels) if lb == c]
            centroid = np.mean(np.stack(members), axis=0)
            norm = float(np.linalg.norm(centroid))
            if norm > 1e-9:
                centroid = centroid / norm
            name, score, role = SP.match_speaker(centroid)
            if name and role == "avatar":
                cluster_names[c] = "数字人"
            elif name:
                cluster_names[c] = name
            else:
                cluster_names[c] = f"说话人{c + 1}"
            # 存 (centroid, 匹配置信度, 角色)，供回写与「会后命名」复用
            cluster_vecs[c] = (centroid, float(score or 0.0), role)

        # 5) 回写消息标注
        audio_to_speaker = {}
        for i, m in enumerate(valid):
            c = labels[i]
            # cluster_vecs[c] = (centroid, 匹配分数, 角色)，回写只需要分数
            score = cluster_vecs[c][1]
            audio_to_speaker[m.get("audio")] = {
                "speaker": cluster_names[c],
                "conf": round(float(score), 4),
            }
        updated = apply_speaker_results(conv_id, audio_to_speaker)

        # 6) 汇总每个说话人的发言段数，供会后命名界面使用
        #    按簇号计数而非按名字：两个簇匹配到同一姓名时不会重复计数
        from collections import Counter
        counts = Counter(labels)
        speakers = [{"cluster": c, "label": cluster_names[c], "segments": counts[c]}
                    for c in range(n_clusters)]

        # 记住各簇 centroid，供「会后命名」直接复用（无需重新提 embedding）
        clusters = [
            {"label": cluster_names[c], "vector": cluster_vecs[c][0].tolist(),
             "count": counts[cluster_names[c]]}
            for c in range(n_clusters)
        ]
        # 经公开方法写入，避免跨模块直接改私有变量
        SP.set_session_centers(f"__diarize__{conv_id}", clusters)
        # 落盘：服务重启后仍可命名，不必重跑分析
        _save_clusters(conv_id, clusters)

        msg = f"{len(valid)} 段语音聚为 {n_clusters} 个说话人，已更新 {updated} 条消息"
        logger.info(f"[Diarize] {msg}")
        result = {"status": "done", "msg": msg, "speakers": speakers, "clusters": n_clusters,
                  "updated": updated}
        _set(**result)
        return result

    except Exception as e:
        logger.exception(f"[Diarize] 分析失败: {e}")
        result = {"status": "error", "msg": str(e), "speakers": []}
        _set(**result)
        return result


# ─── 路由处理 ──────────────────────────────────────────────────────────────

def _json_ok(data=None):
    from aiohttp import web
    return web.json_response({"code": 0, "msg": "ok", "data": data or {}})


def _json_err(msg, code=-1):
    from aiohttp import web
    return web.json_response({"code": code, "msg": str(msg)})


async def start_diarize(request):
    """触发离线精修（异步执行，立即返回 running）。"""
    import asyncio
    conv_id = request.match_info.get("id", "")
    if not conv_id:
        return _json_err("缺少会话 id")
    try:
        params = await request.json()
    except Exception:
        params = {}
    force = bool(params.get("force", False))

    st = get_status(conv_id)
    if st.get("status") == "running":
        return _json_ok(st)

    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, analyze_conversation, conv_id, force)
    return _json_ok({"status": "running"})


async def query_diarize(request):
    """查询离线精修状态与结果（前端轮询）。"""
    conv_id = request.match_info.get("id", "")
    return _json_ok(get_status(conv_id))


async def list_speakers_handler(request):
    """声纹库列表。"""
    import agent.speaker_store as SP
    return _json_ok({"speakers": SP.list_speakers()})


async def add_speaker_handler(request):
    """注册声纹：multipart 表单，字段 name + audio（16k wav）。"""
    import io
    import soundfile as sf
    import agent.speaker_store as SP
    try:
        form = await request.post()
        name = (form.get("name") or "").strip()
        fileobj = form.get("audio")
        if not name:
            return _json_err("name 不能为空")
        if fileobj is None:
            return _json_err("缺少 audio 文件")
        filebytes = fileobj.file.read()
        try:
            wav, fs = sf.read(io.BytesIO(filebytes), dtype="float32")
        except Exception as e:
            return _json_err(f"音频解析失败（需 wav 16kHz）：{e}")
        if fs != 16000:
            return _json_err(f"采样率需为 16000，实际 {fs}")
        if len(wav) < 16000:
            return _json_err("录音太短，请至少录 1 秒")
        entry = SP.register_speaker(name=name, audio=wav, source="upload")
        if entry is None:
            return _json_err("声纹提取失败")
        return _json_ok(entry)
    except Exception as e:
        logger.exception(f"[Diarize] 注册声纹失败: {e}")
        return _json_err(str(e))


async def delete_speaker_handler(request):
    """删除声纹库条目。"""
    import agent.speaker_store as SP
    spk_id = request.match_info.get("sid", "")
    if not SP.delete_speaker(spk_id):
        return _json_err("声纹不存在")
    return _json_ok()


async def list_session_speakers_handler(request):
    """列出某会话识别出的说话人（供会后命名界面使用）。"""
    import agent.speaker_store as SP
    conv_id = request.match_info.get("id", "")
    return _json_ok({"speakers": SP.get_session_speakers(conv_id)})


async def rename_speaker_handler(request):
    """给会话内的说话人命名，并注册进全局声纹库。

    body: { "label": "说话人2", "name": "王五" }
    """
    import agent.speaker_store as SP
    conv_id = request.match_info.get("id", "")
    try:
        params = await request.json()
    except Exception:
        return _json_err("请求体解析失败")
    label = (params.get("label") or "").strip()
    name = (params.get("name") or "").strip()
    if not label or not name:
        return _json_err("label 与 name 均不能为空")
    if not SP.name_speaker(conv_id, label, name):
        return _json_err("未找到该说话人（可能已被清理或未分析）")
    # 同步改掉会话记录里的旧标签，让改名在当前会话立刻可见
    # （否则只有下次新会话命中声纹库、或重跑一次分析才会生效）
    updated = 0
    try:
        from agent.conversation_store import rename_speaker_messages
        updated = rename_speaker_messages(conv_id, label, name)
    except Exception as e:
        logger.warning(f"[Diarize] 改名回写失败（声纹已注册）: {e}")
    return _json_ok({"name": name, "updated": updated})


def setup_speaker_routes(app):
    """注册说话人相关路由。"""
    app.router.add_post("/api/conversations/{id}/diarize", start_diarize)
    app.router.add_get("/api/conversations/{id}/diarize", query_diarize)
    app.router.add_get("/api/conversations/{id}/speakers", list_session_speakers_handler)
    app.router.add_post("/api/conversations/{id}/speakers/rename", rename_speaker_handler)
    app.router.add_get("/api/speakers", list_speakers_handler)
    app.router.add_post("/api/speakers", add_speaker_handler)
    app.router.add_delete("/api/speakers/{sid}", delete_speaker_handler)
    logger.info("[Speaker] 说话人相关路由已注册")
