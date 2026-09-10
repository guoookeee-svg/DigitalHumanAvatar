###############################################################################
#  通用 Agent 服务模式 — 调用带上下文/记忆的 Agent 服务（类似 QwenPaw）
#
#  配置从 llm_config.json 读取（agent 段），支持自定义：
#    - host / port / path：服务地址与调用路径
#    - headers：请求头（可自定义多个）
#    - body_template：请求体模板（支持 {message}、{session_id}、{agent_id} 变量）
#    - response_type：响应类型（sse / json）
#    - response_text_path：从响应中提取文本的路径
###############################################################################

import time
import os
import json
import ssl
import urllib.request
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from avatars.base_avatar import BaseAvatar
from utils.logger import logger
from agent.llm_router import _load_llm_config
from agent.conversation_store import append_message, build_message_extra


def _get_agent_config() -> dict:
    """从 llm_config.json 读取 Agent 服务配置。"""
    cfg = _load_llm_config()
    return cfg.get("agent", {})


def _qwenpaw_ssl_context():
    """创建宽松验证的 SSL 上下文，处理自签名/内部 CA 证书。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _render_template(template, variables: dict):
    """递归替换模板中的 {var} 变量。"""
    if isinstance(template, str):
        result = template
        for key, value in variables.items():
            result = result.replace("{" + key + "}", str(value))
        return result
    elif isinstance(template, dict):
        return {k: _render_template(v, variables) for k, v in template.items()}
    elif isinstance(template, list):
        return [_render_template(item, variables) for item in template]
    else:
        return template


def _extract_text_from_response(data, path: str) -> str:
    """从响应数据中按路径提取文本。路径支持点号（如 a.b.c）。"""
    if not path:
        return ""
    parts = path.split(".")
    current = data
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            idx = int(part)
            current = current[idx] if idx < len(current) else None
        else:
            return ""
        if current is None:
            return ""
    return str(current) if current is not None else ""


def llm_response(message, avatar_session: 'BaseAvatar', datainfo: dict = {}):
    try:
        start = time.perf_counter()
        # 记录当前 LLM 代次：flush_talk 递增代次后，本线程检测到代次变化即停止
        my_generation = avatar_session.llm_generation

        # 从 llm_config.json 读取 Agent 配置
        agent = _get_agent_config()
        host = agent.get("host", "")
        port = agent.get("port", "30027")
        path = agent.get("path", "/api/console/chat")
        headers = agent.get("headers", {})
        body_template = agent.get("body_template", {})
        response_type = agent.get("response_type", "sse")
        response_text_path = agent.get("response_text_path", "text")

        # 构建请求变量
        # 每个对话会话（conversation_id）用独立的 agent session_id，
        # 让不同会话的对话上下文互不干扰。若配置显式指定了 session_id 则优先使用。
        agent_id = agent.get("agent_id", "")
        configured_session = agent.get("session_id", "")
        conv_id = datainfo.get('conversation_id', '')
        conn_sessionid = getattr(avatar_session, "sessionid", "")
        if configured_session:
            session_id = configured_session
        elif conv_id:
            session_id = f"lt_{conv_id}"
        elif conn_sessionid:
            session_id = f"lt_{conn_sessionid}"
        else:
            session_id = ""
        # 说话人前缀：多人会话时让 agent 知道这句话是谁说的。
        # agent 服务的上下文在远端，本地只能标注当前这条消息。
        speaker = datainfo.get('speaker', '') or ''
        if speaker:
            try:
                from agent.conversation_store import get_speaker_names
                from agent.llm_router import should_annotate_speakers, format_speaker_prefix
                if should_annotate_speakers(get_speaker_names(conv_id), speaker):
                    message = format_speaker_prefix(message, speaker, True)
            except Exception as e:
                logger.warning(f"[LLM] 说话人标注失败: {e}")

        # 替会模式：agent 的 prompt 在远端，本地在消息前注入分身场景说明，
        # 让回复贴合"代表主人参会"的口吻（简短、口语化、以主人立场发言）
        try:
            from agent.meeting_gate import get_interaction_mode
            if get_interaction_mode() == "meeting":
                message = ("（你此刻正以数字分身身份代表你的主人参会，请以主人身份"
                           "简短口语化发言，不超过三句话，不要条目和Markdown。）" + message)
        except Exception as e:
            logger.warning(f"[LLM] 替会模式标注失败: {e}")

        variables = {
            "message": message,
            "session_id": session_id,
            "agent_id": agent_id,
        }

        url = f"{host}:{port}{path}"
        payload = _render_template(body_template, variables)

        # 构建请求头（替换模板变量）
        req_headers = {}
        for k, v in headers.items():
            req_headers[k] = _render_template(v, variables)

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers=req_headers,
        )
        end = time.perf_counter()
        logger.info(f"llm Time init: {end-start}s,{message}")

        resp = urllib.request.urlopen(req, timeout=120, context=_qwenpaw_ssl_context())

        result = ""
        full_text = ""
        first = True

        if response_type == "sse":
            # SSE 流式解析
            for raw in resp:
                line = raw.decode(errors="replace").strip()
                payload_data = None
                for prefix in ("data: ", "data "):
                    if line.startswith(prefix):
                        payload_data = line[len(prefix):]
                        break
                if payload_data is None:
                    continue
                try:
                    data = json.loads(payload_data)
                except json.JSONDecodeError:
                    continue

                # 检查代次变化（打断）
                if avatar_session.llm_generation != my_generation:
                    logger.info("llm interrupted, stop generating")
                    break

                # 跳过非增量消息（delta=false 的完整文本，避免与增量重复）
                if "delta" in data and not data.get("delta", False):
                    continue

                # 提取文本
                t = _extract_text_from_response(data, response_text_path)
                if t:
                    full_text += t
                    if first:
                        end = time.perf_counter()
                        logger.info(f"llm Time to first chunk: {end-start}s")
                        first = False
                    # 按标点分句送 TTS
                    lastpos = 0
                    for i, char in enumerate(t):
                        if char in ",.!;:，。！？：；":
                            result = result + t[lastpos:i+1]
                            lastpos = i+1
                            if len(result) > 4:
                                logger.info(result)
                                avatar_session.put_msg_txt(result, datainfo)
                                result = ""
                    result = result + t[lastpos:]
        else:
            # JSON 响应
            data = json.loads(resp.read().decode(errors="replace"))
            t = _extract_text_from_response(data, response_text_path)
            if t:
                avatar_session.put_msg_txt(t, datainfo)
                # 非流式已整段播放，不再赋给 result，避免末尾重复播放一次
                full_text = t

        end = time.perf_counter()
        logger.info(f"llm Time to last chunk: {end-start}s")
        if result:
            avatar_session.put_msg_txt(result, datainfo)

        # 写入对话会话记录（用户问题 + 数字人回答）
        try:
            conv_id = datainfo.get('conversation_id', '')
            target = conv_id or getattr(avatar_session, "sessionid", "")
            if target:
                # 语音入口会带 audio 路径（说话人分离的数据源）与实时说话人标签
                append_message(target, "user", message, build_message_extra(datainfo))
                append_message(target, "assistant", full_text)
        except Exception as e:
            logger.warning(f"[LLM] 写入对话记录失败: {e}")

    except Exception as e:
        logger.exception('llm exception:')
        return
