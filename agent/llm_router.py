###############################################################################
#  LLM 模式分发器 — 根据当前模式调用 QwenPaw 或 OpenAI 实现
#
#  支持两种模式：
#    - qwenpaw: 使用 QwenPaw Agent（自带上下文/记忆），调用 llm.llm_response
#    - openai:  使用 OpenAI 兼容 LLM API（本地管理上下文/记忆/人设/RAG），调用 llm_openai.llm_response_openai
#
#  模式通过前端 /api/llm/mode 切换（运行时生效，无需重启）。
###############################################################################

import os
import json
from aiohttp import web

from utils.logger import logger

# 项目根目录（agent 包的上一层）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 人设配置文件路径
PERSONA_FILE = os.path.join(PROJECT_ROOT, "persona.json")
# LLM 配置（OpenAI + 通用 Agent 服务）
LLM_CONFIG_FILE = os.path.join(PROJECT_ROOT, "llm_config.json")

# 当前 LLM 模式（内存变量，默认从配置读取）
_current_mode = "openai"


def _load_llm_config() -> dict:
    """读取 LLM 配置。"""
    try:
        with open(LLM_CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"[LLM] 读取配置失败: {e}")
        return {"mode": "openai", "openai": {}, "agent": {}}


def _save_llm_config(data: dict) -> bool:
    """保存 LLM 配置。"""
    try:
        with open(LLM_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.exception(f"[LLM] 保存配置失败: {e}")
        return False


def _init_mode():
    """初始化模式（从配置读取）。"""
    global _current_mode
    cfg = _load_llm_config()
    mode = cfg.get("mode", "openai")
    if mode in ("openai", "agent"):
        _current_mode = mode
    else:
        _current_mode = "openai"


_init_mode()


def get_mode() -> str:
    """返回当前 LLM 模式。"""
    return _current_mode


def set_mode(mode: str) -> bool:
    """设置当前 LLM 模式。返回是否设置成功。"""
    global _current_mode
    mode = (mode or "").strip().lower()
    if mode not in ("openai", "agent"):
        return False
    _current_mode = mode
    logger.info(f"[LLM] 模式切换为: {mode}")
    return True


def llm_response(message, avatar_session, datainfo: dict = {}):
    """LLM 响应入口，根据当前模式分发到对应实现。"""
    if _current_mode == "openai":
        from agent.llm_openai import llm_response_openai
        return llm_response_openai(message, avatar_session, datainfo)
    else:
        from agent.llm import llm_response as _agent_response
        return _agent_response(message, avatar_session, datainfo)


# ─── 人设配置 ───────────────────────────────────────────────

def load_persona() -> dict:
    """读取人设配置。"""
    try:
        with open(PERSONA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"[LLM] 读取人设失败: {e}")
        return {"system_prompt": "", "reply_style": "", "max_tokens": 200, "context_rounds": 20}


def save_persona(data: dict) -> bool:
    """保存人设配置。"""
    try:
        with open(PERSONA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.exception(f"[LLM] 保存人设失败: {e}")
        return False


async def get_persona(request):
    """获取人设配置。"""
    return web.json_response({"code": 0, "msg": "ok", "data": load_persona()})


async def set_persona(request):
    """保存人设配置。"""
    try:
        params = await request.json()
        if save_persona(params):
            return web.json_response({"code": 0, "msg": "ok", "data": load_persona()})
        return web.json_response({"code": -1, "msg": "保存人设失败"})
    except Exception as e:
        logger.exception("set_persona error:")
        return web.json_response({"code": -1, "msg": str(e)})


# ─── 路由处理 ───────────────────────────────────────────────

async def get_llm_mode(request):
    """获取当前 LLM 模式。"""
    return web.json_response({"code": 0, "msg": "ok", "data": {"mode": get_mode()}})


async def set_llm_mode(request):
    """设置当前 LLM 模式。"""
    try:
        params = await request.json()
        mode = params.get("mode", "")
        if set_mode(mode):
            return web.json_response({"code": 0, "msg": "ok", "data": {"mode": get_mode()}})
        return web.json_response({"code": -1, "msg": f"无效的模式: {mode}（可选 qwenpaw / openai）"})
    except Exception as e:
        logger.exception("set_llm_mode error:")
        return web.json_response({"code": -1, "msg": str(e)})


async def get_llm_config(request):
    """获取 LLM 配置（OpenAI + Agent）。"""
    cfg = _load_llm_config()
    cfg["mode"] = get_mode()
    return web.json_response({"code": 0, "msg": "ok", "data": cfg})


async def set_llm_config(request):
    """保存 LLM 配置。"""
    try:
        params = await request.json()
        # 保存配置
        if _save_llm_config(params):
            # 同步模式
            mode = params.get("mode", "")
            if mode in ("openai", "agent"):
                set_mode(mode)
            return web.json_response({"code": 0, "msg": "ok", "data": _load_llm_config()})
        return web.json_response({"code": -1, "msg": "保存配置失败"})
    except Exception as e:
        logger.exception("set_llm_config error:")
        return web.json_response({"code": -1, "msg": str(e)})


# ─── 说话人标注（openai / agent 两种模式共用） ─────────────────

def get_speaker_annotate_cfg() -> dict:
    """说话人标注配置（llm_config.json 的 speaker 段）。"""
    cfg = {"annotate_in_llm": True, "min_speakers_for_annotate": 2}
    raw = _load_llm_config().get("speaker") or {}
    if isinstance(raw, dict):
        for k in list(cfg):
            if raw.get(k) is not None:
                cfg[k] = raw[k]
    return cfg


def should_annotate_speakers(names, current_speaker: str = "") -> bool:
    """是否需要给消息加说话人前缀 —— 仅在多人会话时。

    单人会话加前缀纯属浪费 token，还可能让模型无谓地称呼对方。
    """
    cfg = get_speaker_annotate_cfg()
    if not cfg.get("annotate_in_llm"):
        return False
    try:
        need = int(cfg.get("min_speakers_for_annotate", 2))
    except (TypeError, ValueError):
        need = 2
    unique = {n for n in (names or []) if n}
    if current_speaker:
        unique.add(current_speaker)
    return len(unique) >= need


def format_speaker_prefix(content: str, speaker: str, annotate: bool) -> str:
    """给用户消息加说话人前缀，形如「[张三]: 我觉得可以」。"""
    if not annotate or not speaker or not content:
        return content
    return f"[{speaker}]: {content}"


# ─── 交互模式（solo / meeting / recording）与替会配置 ─────────

from agent.meeting_gate import (get_interaction_mode, set_interaction_mode,
                                get_meeting_cfg, save_meeting_cfg, MODES as _INTERACTION_MODES)


async def get_interaction_mode_handler(request):
    """获取当前交互模式。"""
    return web.json_response({"code": 0, "msg": "ok",
                              "data": {"mode": get_interaction_mode(),
                                       "modes": list(_INTERACTION_MODES)}})


async def set_interaction_mode_handler(request):
    """切换交互模式（运行时生效 + 持久化）。"""
    try:
        params = await request.json()
        mode = params.get("mode", "")
        if set_interaction_mode(mode):
            return web.json_response({"code": 0, "msg": "ok",
                                      "data": {"mode": get_interaction_mode()}})
        return web.json_response({"code": -1,
                                  "msg": f"无效的模式: {mode}（可选 {' / '.join(_INTERACTION_MODES)}）"})
    except Exception as e:
        logger.exception("set_interaction_mode error:")
        return web.json_response({"code": -1, "msg": str(e)})


async def get_meeting_config_handler(request):
    """获取替会配置。"""
    return web.json_response({"code": 0, "msg": "ok", "data": get_meeting_cfg()})


async def set_meeting_config_handler(request):
    """保存替会配置。"""
    try:
        params = await request.json()
        if save_meeting_cfg(params):
            return web.json_response({"code": 0, "msg": "ok", "data": get_meeting_cfg()})
        return web.json_response({"code": -1, "msg": "保存替会配置失败"})
    except Exception as e:
        logger.exception("set_meeting_config error:")
        return web.json_response({"code": -1, "msg": str(e)})


def setup_llm_routes(app):
    """注册 LLM 相关路由。"""
    app.router.add_get("/api/llm/mode", get_llm_mode)
    app.router.add_post("/api/llm/mode", set_llm_mode)
    app.router.add_get("/api/llm/config", get_llm_config)
    app.router.add_post("/api/llm/config", set_llm_config)
    app.router.add_get("/api/persona", get_persona)
    app.router.add_post("/api/persona", set_persona)
    # 交互模式与替会配置
    app.router.add_get("/api/mode", get_interaction_mode_handler)
    app.router.add_post("/api/mode", set_interaction_mode_handler)
    app.router.add_get("/api/meeting/config", get_meeting_config_handler)
    app.router.add_post("/api/meeting/config", set_meeting_config_handler)
