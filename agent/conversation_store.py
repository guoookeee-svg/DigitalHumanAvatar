###############################################################################
#  对话会话存储 — 独立的对话会话（类似 OpenAI/Claude 的会话列表）
#
#  每个对话会话有独立的 ID 和上下文，聊天记录（用户说 / 数字人答）
#  持久化到 data/conversations/，重启不丢失。
#  对话内容通过数字人连接（avatar_session）播放。
###############################################################################

import os
import json
import time
import uuid
import shutil
import threading

from aiohttp import web

from utils.logger import logger

# 对话记录目录
CONVERSATION_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "conversations")
# 会话列表索引文件
INDEX_FILE = os.path.join(CONVERSATION_DIR, "index.json")
# 说话人音频留存目录（与 speaker_diarize.AUDIO_ROOT 保持一致）
AUDIO_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "audio")

# 可重入：get_active_conversation 持锁后会调用 create_conversation
_lock = threading.RLock()


def _safe_id(conv_id: str) -> str:
    """防止路径穿越：只允许字母数字和连字符。"""
    return "".join(c for c in str(conv_id) if c.isalnum() or c in "-_")


def _conv_file(conv_id: str) -> str:
    """返回某个会话的消息文件路径。"""
    return os.path.join(CONVERSATION_DIR, f"{_safe_id(conv_id)}.json")


def _load_index() -> list:
    """读取会话列表索引。"""
    try:
        if os.path.exists(INDEX_FILE):
            with open(INDEX_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
    except Exception as e:
        logger.warning(f"[Conversation] 读取会话列表失败: {e}")
    return []


def _save_index(conversations: list):
    """保存会话列表索引。"""
    try:
        os.makedirs(CONVERSATION_DIR, exist_ok=True)
        with open(INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump(conversations, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception(f"[Conversation] 保存会话列表失败: {e}")


def _load_messages(conv_id: str) -> list:
    """读取某个会话的消息。"""
    path = _conv_file(conv_id)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
    except Exception as e:
        logger.warning(f"[Conversation] 读取消息失败: {e}")
    return []


def _save_messages(conv_id: str, entries: list):
    """保存某个会话的消息。"""
    try:
        os.makedirs(CONVERSATION_DIR, exist_ok=True)
        with open(_conv_file(conv_id), "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception(f"[Conversation] 保存消息失败: {e}")


# ─── 会话管理 ───────────────────────────────────────────────

def list_conversations() -> list:
    """获取所有会话（按创建时间倒序）。

    message_count 在写入时一并维护在 index 里；只有旧数据缺该字段时才回退读文件，
    并且补齐后立即写回 index —— 否则每次列表都要全量读一遍消息文件。
    """
    with _lock:
        convs = _load_index()
        dirty = False
        for c in convs:
            if c.get("message_count") is None:
                c["message_count"] = len(_load_messages(c["id"]))
                dirty = True
        if dirty:
            _save_index(convs)
        return sorted(convs, key=lambda x: x.get("created_at", 0), reverse=True)


def create_conversation(title: str = "") -> dict:
    """新建一个会话，返回会话信息。"""
    with _lock:
        conv_id = str(uuid.uuid4())
        conv = {
            "id": conv_id,
            "title": title or "新对话",
            "created_at": time.time(),
            "updated_at": time.time(),
            "message_count": 0,
        }
        convs = _load_index()
        convs.append(conv)
        _save_index(convs)
        _save_messages(conv_id, [])
        return conv


def delete_conversation(conv_id: str) -> bool:
    """删除一个会话。"""
    with _lock:
        convs = _load_index()
        new_convs = [c for c in convs if c.get("id") != conv_id]
        if len(new_convs) == len(convs):
            return False
        _save_index(new_convs)
        try:
            if os.path.exists(_conv_file(conv_id)):
                os.remove(_conv_file(conv_id))
            # 同步清理该会话的说话人音频留存，避免产生孤儿音频
            audio_dir = os.path.join(AUDIO_ROOT, _safe_id(conv_id))
            if os.path.isdir(audio_dir):
                shutil.rmtree(audio_dir, ignore_errors=True)
        except Exception as e:
            logger.warning(f"[Conversation] 删除消息文件失败: {e}")
        # 同步清理该会话的音频留存目录（说话人分离的数据源），避免孤儿音频
        try:
            audio_dir = os.path.join(AUDIO_ROOT, _safe_id(conv_id))
            if os.path.isdir(audio_dir):
                shutil.rmtree(audio_dir)
        except Exception as e:
            logger.warning(f"[Conversation] 删除音频目录失败: {e}")
        return True


def get_messages(conv_id: str) -> list:
    """获取某个会话的消息。"""
    with _lock:
        return _load_messages(conv_id)


def append_message(conv_id: str, role: str, content: str, extra: dict = None):
    """追加一条消息（role: user / assistant）。

    extra 中的键值对（如 audio / speaker / speaker_conf）会一并写进消息条目，
    供说话人分离等扩展能力使用；既有读取方只关心 role/content，不受影响。
    """
    if not content:
        return
    entry = {
        "role": role,
        "content": content,
        "timestamp": time.time(),
    }
    if isinstance(extra, dict):
        for k, v in extra.items():
            if k not in ("role", "content", "timestamp") and v is not None:
                entry[k] = v
    with _lock:
        entries = _load_messages(conv_id)
        # 首条用户消息用于自动生成标题
        is_first_user = (not entries) and role == "user"
        entries.append(entry)
        _save_messages(conv_id, entries)
        # 更新会话的 updated_at / message_count / title
        convs = _load_index()
        for c in convs:
            if c.get("id") == conv_id:
                c["updated_at"] = time.time()
                c["message_count"] = len(entries)
                if is_first_user and (not c.get("title") or c.get("title") == "新对话"):
                    c["title"] = _make_title(content)
                break
        _save_index(convs)


def apply_speaker_results(conv_id: str, audio_to_speaker: dict) -> int:
    """按 audio 字段批量回填说话人标注（离线精修用）。返回更新的条数。

    audio_to_speaker: { "会话id/时间戳.wav": {"speaker": "张三", "conf": 0.82}, ... }
    只覆盖带音频路径的消息，其余消息不受影响。
    """
    if not audio_to_speaker:
        return 0
    with _lock:
        entries = _load_messages(conv_id)
        n = 0
        for e in entries:
            key = e.get("audio")
            if key and key in audio_to_speaker:
                info = audio_to_speaker[key]
                e["speaker"] = info.get("speaker", "")
                e["speaker_conf"] = info.get("conf", 0)
                e["speaker_source"] = "offline"
                n += 1
        if n:
            _save_messages(conv_id, entries)
        return n


def rename_speaker_messages(conv_id: str, old_label: str, new_name: str) -> int:
    """把会话里某个说话人标签统一改名为新名字（前端改名后立即生效）。

    离线精修会把标签写进每条消息（如「说话人1」）。用户在界面上给它命名后，
    需要把这些历史消息的标签一并改掉 —— 否则改名只在声纹库生效，
    当前会话的记录仍显示旧标签。

    返回更新的消息条数。
    """
    if not conv_id or not old_label or not new_name or old_label == new_name:
        return 0
    with _lock:
        entries = _load_messages(conv_id)
        n = 0
        for e in entries:
            if e.get("speaker") == old_label:
                e["speaker"] = new_name
                e["speaker_source"] = "named"
                n += 1
        if n:
            _save_messages(conv_id, entries)
        return n


def get_speaker_names(conv_id: str) -> list:
    """返回该会话里出现过的说话人名字（去重，按首次出现顺序）。

    供 LLM 链路判断「是否多人会话」—— 只有多人时才值得给消息加说话人前缀。
    """
    try:
        entries = get_messages(conv_id)
    except Exception:
        return []
    names, seen = [], set()
    for e in entries:
        if e.get("role") != "user":
            continue
        n = e.get("speaker")
        if n and n not in seen:
            seen.add(n)
            names.append(n)
    return names


def get_audio_messages(conv_id: str) -> list:
    """取出该会话所有带音频留存的用户消息（按时间升序）。"""
    entries = get_messages(conv_id)
    out = [e for e in entries
           if e.get("role") == "user" and e.get("audio")]
    out.sort(key=lambda e: e.get("timestamp", 0))
    return out


def build_message_extra(datainfo: dict) -> dict:
    """从 datainfo 提取要写进消息记录的扩展字段（audio / speaker）。

    语音入口会带上这些（音频留存路径 + 实时说话人标签）；
    文本入口没有，返回空 dict —— 调用方直接把它传给 append_message 的 extra 参数。
    """
    if not isinstance(datainfo, dict):
        return {}
    extra = {}
    if datainfo.get("audio"):
        extra["audio"] = datainfo.get("audio")
    if datainfo.get("speaker"):
        extra["speaker"] = datainfo.get("speaker")
        extra["speaker_conf"] = datainfo.get("speaker_conf", 0)
        extra["speaker_source"] = datainfo.get("speaker_source", "realtime")
    return extra


def clear_conversation(conv_id: str) -> bool:
    """清空某个会话的消息。

    会话不存在时直接返回 False，不再凭空创建消息文件
    （否则任意 id 都能建文件，磁盘会被撑爆）。
    """
    with _lock:
        if not _conv_exists(conv_id):
            return False
        try:
            _save_messages(conv_id, [])
            convs = _load_index()
            for c in convs:
                if c.get("id") == conv_id:
                    c["message_count"] = 0
                    break
            _save_index(convs)
            return True
        except Exception as e:
            logger.exception(f"[Conversation] 清空消息失败: {e}")
            return False


def _make_title(text: str, max_len: int = 20) -> str:
    """用首条用户消息生成会话标题，避免列表里清一色「新对话」。"""
    t = " ".join((text or "").split())
    if not t:
        return "新对话"
    return t[:max_len] + ("…" if len(t) > max_len else "")


def reconcile_orphans() -> int:
    """扫描消息文件，把不在 index.json 里的会话补登记进去。

    index.json 写入失败、或被手工删改过，就会产生孤儿会话：
    它们占着磁盘，却永远不出现在会话列表里。启动时调用一次回收。
    """
    with _lock:
        convs = _load_index()
        known = {c.get("id") for c in convs}
        try:
            files = [f for f in os.listdir(CONVERSATION_DIR) if f.endswith(".json")]
        except OSError:
            return 0
        added = 0
        for fn in files:
            if fn in ("index.json", "_active.json"):
                continue
            conv_id = fn[:-5]
            if conv_id in known:
                continue
            try:
                mtime = os.path.getmtime(os.path.join(CONVERSATION_DIR, fn))
            except OSError:
                mtime = time.time()
            convs.append({
                "id": conv_id,
                "title": "新对话",
                "created_at": mtime,
                "updated_at": mtime,
                "message_count": None,  # 延迟到 list_conversations 时补齐
            })
            known.add(conv_id)
            added += 1
        if added:
            _save_index(convs)
            logger.info(f"[Conversation] 回收孤儿会话 {added} 个")
        return added


# ─── 活动会话指针 ───────────────────────────────────────────
#  数字人连接号（WebRTC sessionid，如 "0"）→ 当前对话会话 ID。
#  语音入口 /human 不带 conversation_id，靠这张表解析出该连接当前绑定的会话，
#  使语音对话与控制台文本对话落在同一个会话里，而不是各写各的。

ACTIVE_FILE = os.path.join(CONVERSATION_DIR, "_active.json")


def _load_active() -> dict:
    """读取活动会话指针表。"""
    try:
        if os.path.exists(ACTIVE_FILE):
            with open(ACTIVE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception as e:
        logger.warning(f"[Conversation] 读取活动会话失败: {e}")
    return {}


def _save_active(mapping: dict):
    """保存活动会话指针表。"""
    try:
        os.makedirs(CONVERSATION_DIR, exist_ok=True)
        with open(ACTIVE_FILE, "w", encoding="utf-8") as f:
            json.dump(mapping, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception(f"[Conversation] 保存活动会话失败: {e}")


def _conv_exists(conv_id: str) -> bool:
    """判断会话是否仍存在（以消息文件是否存在为准）。"""
    return bool(conv_id) and os.path.exists(_conv_file(conv_id))


def get_active_conversation(conn_sessionid: str, create: bool = True) -> str:
    """返回该数字人连接当前绑定的会话 ID。

    指针缺失、或指向的会话已被删除时：create=True 则新建并登记，否则返回空串。
    """
    key = str(conn_sessionid or "")
    if not key:
        return ""
    with _lock:
        mapping = _load_active()
        conv_id = mapping.get(key, "")
        if conv_id and _conv_exists(conv_id):
            return conv_id
        if not create:
            return ""
        conv = create_conversation()  # 内部会登记进 index.json 并创建消息文件
        mapping[key] = conv["id"]
        _save_active(mapping)
        logger.info(f"[Conversation] 连接 {key} 绑定新会话 {conv['id']}")
        return conv["id"]


def set_active_conversation(conn_sessionid: str, conv_id: str) -> bool:
    """显式绑定某个数字人连接到指定会话。"""
    key = str(conn_sessionid or "")
    if not key or not conv_id:
        return False
    with _lock:
        mapping = _load_active()
        mapping[key] = _safe_id(conv_id)
        _save_active(mapping)
        logger.info(f"[Conversation] 连接 {key} 切换到会话 {conv_id}")
        return True


# ─── 路由处理 ───────────────────────────────────────────────

async def list_conversations_handler(request):
    """获取会话列表。"""
    return web.json_response({
        "code": 0,
        "msg": "ok",
        "data": {"conversations": list_conversations()},
    })


async def create_conversation_handler(request):
    """新建会话。"""
    try:
        params = await request.json()
    except Exception:
        params = {}
    conv = create_conversation(params.get("title", ""))
    return web.json_response({"code": 0, "msg": "ok", "data": conv})


async def delete_conversation_handler(request):
    """删除会话。"""
    conv_id = request.match_info.get("id", "")
    if delete_conversation(conv_id):
        return web.json_response({"code": 0, "msg": "ok"})
    return web.json_response({"code": -1, "msg": "会话不存在"})


async def get_messages_handler(request):
    """获取某个会话的消息。"""
    conv_id = request.match_info.get("id", "")
    return web.json_response({
        "code": 0,
        "msg": "ok",
        "data": {"id": conv_id, "entries": get_messages(conv_id)},
    })


async def clear_messages_handler(request):
    """清空某个会话的消息。"""
    conv_id = request.match_info.get("id", "")
    if not clear_conversation(conv_id):
        return web.json_response({"code": -1, "msg": "会话不存在"})
    return web.json_response({"code": 0, "msg": "ok"})


async def get_active_handler(request):
    """查询某个数字人连接当前绑定的会话。"""
    conn_sessionid = request.query.get("sessionid", "")
    conv_id = get_active_conversation(conn_sessionid, create=False)
    return web.json_response({"code": 0, "msg": "ok", "data": {"conversation_id": conv_id}})


async def set_active_handler(request):
    """把某个数字人连接绑定到指定会话（控制台切换会话时调用）。"""
    try:
        params = await request.json()
    except Exception:
        params = {}
    conn_sessionid = params.get("sessionid", "")
    conv_id = params.get("conversation_id") or params.get("id", "")
    if not conn_sessionid or not conv_id:
        return web.json_response({"code": -1, "msg": "sessionid 与 conversation_id 均不能为空"})
    if not _conv_exists(conv_id):
        return web.json_response({"code": -1, "msg": "会话不存在"})
    set_active_conversation(conn_sessionid, conv_id)
    return web.json_response({"code": 0, "msg": "ok", "data": {"conversation_id": conv_id}})


def setup_conversation_routes(app):
    """注册对话会话相关路由。"""
    app.router.add_get("/api/conversations", list_conversations_handler)
    app.router.add_post("/api/conversations", create_conversation_handler)
    # active 必须注册在 {id} 之前，否则路径 "active" 会被 {id} 吞掉
    app.router.add_get("/api/conversations/active", get_active_handler)
    app.router.add_post("/api/conversations/active", set_active_handler)
    app.router.add_delete("/api/conversations/{id}", delete_conversation_handler)
    app.router.add_get("/api/conversations/{id}/messages", get_messages_handler)
    app.router.add_delete("/api/conversations/{id}/messages", clear_messages_handler)
