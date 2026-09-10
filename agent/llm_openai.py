###############################################################################
#  直接 LLM API 模式 — OpenAI 兼容 /v1/chat/completions 流式调用
#
#  本地管理：
#    - 上下文：每个 session 的 messages 数组（最近 N 轮）
#    - 人设：从 persona.json 读取 system prompt
#    - 记忆：从 memory_store.py 检索 RAG 记忆注入上下文
#    - 打断：复用 avatar_session.llm_generation 机制
###############################################################################

import os
import json
import time
import ssl
import urllib.request
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from avatars.base_avatar import BaseAvatar

from dotenv import load_dotenv
load_dotenv()  # 加载 .env，确保 LLM API 配置可用

from utils.logger import logger
from agent.llm_router import (load_persona, should_annotate_speakers,
                              format_speaker_prefix as _format_user_content)
from agent.memory_store import search_memory, add_memory
from agent.conversation_store import (append_message, get_messages, build_message_extra,
                                      get_speaker_names)


# 每个 session 的对话上下文（messages 数组）
# { sessionid: [ {role, content}, ... ] }
_session_contexts = {}

# 已尝试从持久化记录重建过的 session。用于区分「没加载过」和「加载过但为空」，
# 避免每次请求都读盘。
_contexts_loaded = set()

# 每个会话出现过的说话人（用于判断是否需要给消息加说话人前缀）
_session_speakers = {}


def _note_speakers(session_id: str, names):
    """记录该会话出现过的说话人。"""
    if not session_id:
        return
    bucket = _session_speakers.setdefault(session_id, set())
    for n in (names or []):
        if n:
            bucket.add(n)


def _need_annotate(session_id: str) -> bool:
    """是否需要给消息加说话人前缀（规则在 llm_router，agent 模式共用）。"""
    return should_annotate_speakers(_session_speakers.get(session_id, set()))


def _get_llm_config():
    """从环境变量读取 LLM API 配置。"""
    return {
        "base_url": os.getenv("LLM_BASE_URL", ""),
        "api_key": os.getenv("LLM_API_KEY", ""),
        "model": os.getenv("LLM_MODEL", "ds-v4-flash"),
    }


def _restore_context(session_id: str, max_msgs: int) -> list:
    """冷启动：从 conversation_store 的持久化记录重建最近 N 条上下文。

    重启后内存上下文清空，但 data/conversations/ 里的消息记录仍在。
    直接从记录重建，避免「聊天记录还在、数字人却失忆」。
    """
    try:
        entries = get_messages(session_id)
    except Exception as e:
        logger.warning(f"[LLM-OpenAI] 上下文重建失败: {e}")
        return []
    entries = [e for e in entries
               if e.get("role") in ("user", "assistant") and e.get("content")][-max_msgs:]
    # 记下这个会话出现过哪些说话人（供「是否多人」判断）
    _note_speakers(session_id, [e.get("speaker") for e in entries])
    # content 保持原样、只带上 speaker 字段：是否加前缀由 _build_messages 统一决定。
    # 否则「前几条无前缀、后面突然有前缀」会让模型困惑。
    return [
        {"role": e["role"], "content": e["content"],
         **({"speaker": e["speaker"]} if e.get("speaker") else {})}
        for e in entries
    ]


def _get_context(session_id: str) -> list:
    """获取指定 session 的对话上下文；首次访问时从持久化记录惰性重建（只此一次）。"""
    if session_id not in _contexts_loaded:
        persona = load_persona()
        _session_contexts[session_id] = _restore_context(
            session_id, persona.get("context_rounds", 20) * 2
        )
        _contexts_loaded.add(session_id)
    return _session_contexts.get(session_id, [])


def _append_context(session_id: str, role: str, content: str, max_rounds: int = 20,
                    speaker: str = ""):
    """追加一条对话到上下文，并限制最近 N 轮。

    speaker 仅对 user 消息有意义；content 存原文，说话人前缀在
    _build_messages 里统一添加（保证整段上下文格式一致）。
    """
    ctx = _session_contexts.setdefault(session_id, [])
    entry = {"role": role, "content": content}
    if speaker:
        entry["speaker"] = speaker
    ctx.append(entry)
    # 限制最近 N 轮（每轮 = user + assistant 两条）
    max_msgs = max_rounds * 2
    if len(ctx) > max_msgs:
        ctx = ctx[-max_msgs:]
        _session_contexts[session_id] = ctx


def _clear_context(session_id: str):
    """清空指定 session 的上下文。"""
    _session_contexts.pop(session_id, None)


def _build_messages(session_id: str, user_message: str, speaker: str = "") -> list:
    """构建 messages：system（人设）+ RAG 记忆 + 历史上下文 + 当前问题。

    speaker 非空且是多人会话时，用户消息会带上「[姓名]: 」前缀，
    让数字人知道每句话是谁说的，从而回应特定的人。
    """
    persona = load_persona()
    # 替会模式用"用户分身"人设（meeting_system_prompt），其余模式用默认人设
    try:
        from agent.meeting_gate import get_interaction_mode
        if get_interaction_mode() == "meeting" and persona.get("meeting_system_prompt"):
            system_prompt = persona["meeting_system_prompt"]
        else:
            system_prompt = persona.get("system_prompt", "")
    except Exception:
        system_prompt = persona.get("system_prompt", "")
    reply_style = persona.get("reply_style", "")
    context_rounds = persona.get("context_rounds", 20)

    # 人设 system prompt
    system_content = system_prompt
    if reply_style:
        system_content += f"\n\n回复风格要求：{reply_style}"

    messages = [{"role": "system", "content": system_content}]

    # RAG 记忆检索（注入相关历史记忆）
    try:
        memories = search_memory(user_message)
        if memories:
            # 记忆里同时有用户和数字人说过的话，必须标注角色，
            # 否则模型会把数字人自己的旧回答误当成用户的发言
            lines = []
            for text, role in memories:
                if role == "assistant":
                    lines.append(f"- 你曾回答：{text}")
                else:
                    lines.append(f"- 用户曾说：{text}")
            messages.append({
                "role": "system",
                "content": "以下是相关的历史对话记忆，供参考：\n" + "\n".join(lines),
            })
    except Exception as e:
        logger.warning(f"[LLM-OpenAI] 记忆检索失败: {e}")

    # 历史上下文（多人会话时统一加说话人前缀）
    annotate = _need_annotate(session_id)
    if annotate:
        messages.append({
            "role": "system",
            "content": "现在是多人对话，用户消息前的 [姓名] 表示这句话是谁说的。"
                       "你可以据此称呼对方或回应特定的人，但不要把方括号前缀复述出来。",
        })
    for e in _get_context(session_id):
        if e.get("role") == "user":
            messages.append({
                "role": "user",
                "content": _format_user_content(e["content"], e.get("speaker", ""), annotate),
            })
        else:
            messages.append({"role": e["role"], "content": e["content"]})

    # 当前问题
    messages.append({
        "role": "user",
        "content": _format_user_content(user_message, speaker, annotate),
    })

    return messages


def _stream_chat(messages, max_tokens, avatar_session, datainfo):
    """调用 OpenAI 兼容接口流式生成，分句送 TTS。"""
    cfg = _get_llm_config()
    my_generation = avatar_session.llm_generation

    payload = {
        "model": cfg["model"],
        "messages": messages,
        "stream": True,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        cfg["base_url"] + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
    )

    start = time.perf_counter()
    resp = urllib.request.urlopen(req, timeout=120)

    result = ""
    full_text = ""
    first = True
    for raw in resp:
        line = raw.decode(errors="replace").strip()
        if not line.startswith("data: "):
            continue
        data_str = line[6:]
        if data_str == "[DONE]":
            break
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            continue

        # 检查代次变化（打断）
        if avatar_session.llm_generation != my_generation:
            logger.info("llm interrupted, stop generating")
            break

        delta = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
        if not delta:
            continue
        full_text += delta

        if first:
            end = time.perf_counter()
            logger.info(f"llm Time to first chunk: {end-start}s")
            first = False

        # 按标点分句送 TTS（复用现有逻辑）
        lastpos = 0
        for i, char in enumerate(delta):
            if char in ",.!;:，。！？：；":
                result = result + delta[lastpos:i+1]
                lastpos = i+1
                if len(result) > 4:
                    logger.info(result)
                    avatar_session.put_msg_txt(result, datainfo)
                    result = ""
        result = result + delta[lastpos:]

    end = time.perf_counter()
    logger.info(f"llm Time to last chunk: {end-start}s")
    if result:
        avatar_session.put_msg_txt(result, datainfo)

    # 返回完整回复（而非最后一段残句），供上下文与对话记录使用
    return full_text


def llm_response_openai(message, avatar_session: 'BaseAvatar', datainfo: dict = {}):
    """OpenAI 模式主入口，与 llm_response 签名一致。"""
    try:
        # 上下文 key：优先用对话会话 id（独立会话），否则用数字人连接 sessionid
        conv_id = datainfo.get('conversation_id', '')
        session_id = conv_id or getattr(avatar_session, "sessionid", "default")
        persona = load_persona()
        max_tokens = persona.get("max_tokens", 200)

        # 说话人：语音入口由 ASR 实时判定后写进 datainfo
        speaker = datainfo.get('speaker', '') or ''
        # 该会话出现过哪些说话人（首次访问查一次记录，之后走内存）
        if session_id not in _session_speakers:
            _note_speakers(session_id, get_speaker_names(session_id))
        _note_speakers(session_id, [speaker])

        # 构建 messages
        messages = _build_messages(session_id, message, speaker)

        # 流式生成
        reply = _stream_chat(messages, max_tokens, avatar_session, datainfo)

        # 记录到上下文和记忆
        _append_context(session_id, "user", message, persona.get("context_rounds", 20),
                        speaker=speaker)
        if reply:
            _append_context(session_id, "assistant", reply, persona.get("context_rounds", 20))
        # 记忆存储（用户问题 + 数字人回答）
        try:
            add_memory(message, role="user", session_id=session_id)
            if reply:
                add_memory(reply, role="assistant", session_id=session_id)
        except Exception as e:
            logger.warning(f"[LLM-OpenAI] 记忆存储失败: {e}")

        # 写入对话会话记录（用户问题 + 数字人回答）
        try:
            if session_id and session_id != "default":
                # 语音入口会带 audio 路径（说话人分离的数据源）与实时说话人标签
                append_message(session_id, "user", message, build_message_extra(datainfo))
                append_message(session_id, "assistant", reply)
        except Exception as e:
            logger.warning(f"[LLM-OpenAI] 写入对话记录失败: {e}")

    except Exception as e:
        logger.exception('llm_openai exception:')
        return
