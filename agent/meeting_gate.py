###############################################################################
#  交互模式响应门控 — 决定"这句话要不要让数字人开口"
#
#  三种交互模式（llm_config.json.interaction_mode）：
#    - solo:      单人对话，所有人都能驱动数字人回答，无身份限制
#    - meeting:   替会模式，听会议所有人发言并记录；
#                 被点名 / 被直接提问必答；议题相关可插话（灵敏度可调）；
#                 其余沉默。主用户发言视为给数字人下指令，正常响应
#    - recording: 会议记录，纯转写+说话人标注，永不调 LLM，数字人不出声
#
#  决策入口：decide_response()，插入在 /human 路由调 LLM 之前。
#  第一版策略：纯规则（关键词 + 句式），语义插话层留接口默认关闭。
###############################################################################

import re
from dataclasses import dataclass

from utils.logger import logger

# 交互模式集合
MODES = ("solo", "meeting", "recording")

# 默认替会配置
DEFAULT_MEETING_CFG = {
    "owner_name": "王总",              # 用户称呼（点名检测用）
    "owner_speaker": "spk1",           # 主用户声纹名（身份判定用）
    # 点名关键词：消息包含任一 → 视为被点名，必答
    "address_keywords": ["数字人", "机器人", "助手"],
    # 直接提问句式（正则）：命中 → 必答
    "question_patterns": [
        "你怎么看", "怎么看", "你说呢", "怎么想", "什么意见", "什么看法",
        "请.{0,6}发言", "请.{0,6}说说", "请.{0,6}讲讲", "请.{0,6}回应",
        "麻烦.{0,8}回复", "麻烦.{0,8}说", "问一下.{0,10}",
        "有没有.{0,6}意见", "有没有.{0,6}问题", "有问题吗", "同意吗", "行不行",
        "总结.{0,6}一下", "帮.{0,4}总结", "帮我.{0,6}总结",
    ],
    # 语义插话灵敏度：off=纯规则；low/mid/high=开语义判断（第一版仅留开关）
    "proactive_level": "off",
}


@dataclass
class GateResult:
    """门控决策结果"""
    respond: bool
    reason: str  # owner / addressed / asked / topic / echo / bystander / recording / passthrough


# ─── 配置读取 ───────────────────────────────────────────────

def _cfg_path() -> str:
    """llm_config.json 绝对路径（位于项目根，agent 包的上一层）。"""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "llm_config.json")


def _load_cfg() -> dict:
    """读取 llm_config.json（轻量；调用频率 = 每句话一次，开销可忽略）。"""
    try:
        import json
        with open(_cfg_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def get_interaction_mode() -> str:
    """当前交互模式（solo / meeting / recording）。"""
    mode = (_load_cfg().get("interaction_mode") or "solo").strip().lower()
    return mode if mode in MODES else "solo"


def get_meeting_cfg() -> dict:
    """替会配置（带默认值兜底）。"""
    cfg = dict(DEFAULT_MEETING_CFG)
    raw = _load_cfg().get("meeting") or {}
    if isinstance(raw, dict):
        for k in list(cfg):
            if raw.get(k) is not None:
                cfg[k] = raw[k]
    return cfg


def set_interaction_mode(mode: str) -> bool:
    """切换交互模式（内存 + 持久化到 llm_config.json）。"""
    mode = (mode or "").strip().lower()
    if mode not in MODES:
        return False
    try:
        import json
        with open(_cfg_path(), "r", encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["interaction_mode"] = mode
        with open(_cfg_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        logger.info(f"[Gate] 交互模式切换为: {mode}")
        return True
    except Exception as e:
        logger.exception(f"[Gate] 切换交互模式失败: {e}")
        return False


def save_meeting_cfg(params: dict) -> bool:
    """保存替会配置。"""
    try:
        import json
        with open(_cfg_path(), "r", encoding="utf-8") as f:
            cfg = json.load(f)
        merged = dict(cfg.get("meeting") or DEFAULT_MEETING_CFG)
        if isinstance(params, dict):
            for k in list(DEFAULT_MEETING_CFG):
                if params.get(k) is not None:
                    merged[k] = params[k]
        cfg["meeting"] = merged
        with open(_cfg_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.exception(f"[Gate] 保存替会配置失败: {e}")
        return False


# ─── 规则检测 ───────────────────────────────────────────────

def _is_addressed(text: str, cfg: dict) -> bool:
    """点名检测：包含用户称呼关键词。"""
    keywords = cfg.get("address_keywords") or []
    return any(kw and kw in text for kw in keywords)


def _is_asked(text: str, cfg: dict) -> bool:
    """直接提问句式检测（正则）。"""
    for pat in cfg.get("question_patterns") or []:
        try:
            if re.search(pat, text):
                return True
        except re.error:
            continue
    return False


# ─── 决策入口 ───────────────────────────────────────────────

def decide_response(text: str, speaker: str = "", is_self: bool = False,
                    mode: str = None) -> GateResult:
    """决定数字人是否要响应这句话。

    参数：
        text:     ASR 识别文本
        speaker:  实时说话人判定结果（姓名或「说话人N」；文本入口为空）
        is_self:  是否数字人自身的回声（前端已在透传前滤除，此处兜底）
        mode:     交互模式（None 则读配置）
    """
    mode = mode or get_interaction_mode()
    cfg = get_meeting_cfg()
    owner = (cfg.get("owner_speaker") or "").strip()
    text = (text or "").strip()

    # 数字人自身回声：任何模式都不响应
    if is_self:
        return GateResult(False, "echo")

    # 记录模式：一律只记录
    if mode == "recording":
        return GateResult(False, "recording")

    # 单人对话：所有人都能对话，不区分主用户
    if mode == "solo":
        return GateResult(True, "passthrough")

    # 替会模式
    if mode == "meeting":
        # 主用户发言：视为给数字人下指令，正常响应
        if owner and (speaker or "").strip() == owner:
            return GateResult(True, "owner")
        # 被点名（含用户称呼关键词）→ 必答
        if _is_addressed(text, cfg):
            return GateResult(True, "addressed")
        # 被直接提问 → 必答
        if _is_asked(text, cfg):
            return GateResult(True, "asked")
        # 语义插话层（第一版默认 off，留接口）
        if cfg.get("proactive_level") in ("low", "mid", "high"):
            # TODO: 语义层——把最近 N 条会议上下文+当前发言交 LLM 快速判断；
            # 第一版纯规则，语义层稳定后启用
            pass
        # 其余：他人闲聊，沉默只记录
        return GateResult(False, "bystander")

    # 未知模式：放行（保持旧行为）
    return GateResult(True, "passthrough")


def gate_reason_label(reason: str) -> str:
    """决策原因的中文标签（前端徽章 / 日志用）。"""
    return {
        "owner": "主用户",
        "addressed": "被点名",
        "asked": "被提问",
        "topic": "议题相关",
        "echo": "回声",
        "bystander": "旁人闲聊",
        "recording": "记录模式",
        "passthrough": "直通",
    }.get(reason, reason)
