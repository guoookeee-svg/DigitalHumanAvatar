###############################################################################
#  服务器路由 — 统一异常处理的 API 路由
###############################################################################

import json
import asyncio
from aiohttp import web

from utils.logger import logger


# ─── 路由工具函数 ──────────────────────────────────────────────────────────

def json_ok(data=None):
    """返回成功 JSON 响应"""
    body = {"code": 0, "msg": "ok"}
    if data is not None:
        body["data"] = data
    return web.Response(
        content_type="application/json",
        text=json.dumps(body),
    )


def json_error(msg: str, code: int = -1):
    """返回错误 JSON 响应"""
    return web.Response(
        content_type="application/json",
        text=json.dumps({"code": code, "msg": str(msg)}),
    )


from server.session_manager import session_manager
from server.avatar_routes import setup_avatar_routes
from agent.conversation_store import append_message, get_active_conversation, set_active_conversation
from agent.meeting_gate import decide_response, gate_reason_label

def get_session(request, sessionid: str):
    """从 app 中获取 session 实例"""
    return session_manager.get_session(sessionid)


# ─── 路由处理函数 ──────────────────────────────────────────────────────────

async def human(request):
    """文本输入（echo/chat 模式），支持 voice/emotion 参数"""
    try:
        params: dict = await request.json()

        sessionid: str = params.get('sessionid', '')
        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")

        if params.get('interrupt'):
            avatar_session.flush_talk()

        datainfo = {}
        if params.get('tts'):  # tts 参数透传（voice, emotion 等）
            datainfo['tts'] = params.get('tts')
        # 语音入口附带的音频留存路径（相对 data/audio/），写进消息记录供说话人分离用
        if params.get('audio'):
            datainfo['audio'] = params.get('audio')
        # 实时说话人判定结果（姓名或「说话人N」），由前端从 ASR 响应透传过来
        if params.get('speaker'):
            datainfo['speaker'] = params.get('speaker')
            datainfo['speaker_conf'] = params.get('speaker_conf', 0)

        # 语音入口（前端 ASR）带 conversation_id 时优先使用（前端从 App.currentConvId
        # 同步到 hidden input #convid，确保选中哪个会话就落到哪个会话）。
        # 没带时回退到活动会话指针（create=False 不自动新建）。
        if params.get('type') == 'chat':
            conv_id = params.get('conversation_id') or get_active_conversation(sessionid, create=False)
            if conv_id:
                datainfo['conversation_id'] = conv_id

        if params['type'] == 'echo':
            avatar_session.put_msg_txt(params['text'], datainfo)
        elif params['type'] == 'chat':
            # ── 交互模式响应门控：决定这句话要不要让数字人开口 ──
            # solo：仅主用户驱动对话；meeting：点名/提问必答，其余沉默；
            # recording：一律只记录不回复。决策不通过时仍写入会话记录（带说话人标签）。
            # bypass=true 为手动插话（替会视图），用户明确要求发言，绕过门控。
            if params.get('bypass'):
                gate = None
            else:
                gate = decide_response(params['text'], params.get('speaker', ''),
                                       params.get('is_self', False))
            conv_id = datainfo.get('conversation_id', '')
            if gate is not None and not gate.respond:
                logger.info(f"[Gate] 不响应（{gate_reason_label(gate.reason)}）: "
                            f"speaker={params.get('speaker', '')!r} text={params['text'][:30]!r}")
                if conv_id:
                    # 只记录不回复（保存音频/说话人字段，供会话回看与离线精修）
                    extra = {k: datainfo[k] for k in ('audio', 'speaker', 'speaker_conf')
                             if datainfo.get(k) is not None}
                    extra['gate_reason'] = gate.reason
                    append_message(conv_id, "user", params['text'], extra)
                return json_ok({"gated": True, "reason": gate.reason})

            llm_response = request.app.get("llm_response")
            if llm_response:
                asyncio.get_event_loop().run_in_executor(
                    None, llm_response, params['text'], avatar_session, datainfo
                )

        return json_ok()
    except Exception as e:
        logger.exception('human route exception:')
        return json_error(str(e))


async def send_conversation_message(request):
    """发送对话消息到指定会话，触发 LLM 并通过数字人播放。

    请求体：{ "text": "...", "sessionid": "数字人连接sessionid" }
    - 写入 user 消息到对话会话
    - 触发 LLM（通过数字人 sessionid 播放），datainfo 携带 conversation_id
    - LLM 回复由 llm.py / llm_openai.py 写入 assistant 消息
    """
    try:
        conv_id = request.match_info.get('id', '')
        params = await request.json()
        text = params.get('text', '')
        sessionid = params.get('sessionid', '')
        if not text:
            return json_error("text is required")

        # 先校验数字人连接：校验不通过时直接返回，不写消息也不改指针，
        # 否则会留下「有 user 无 assistant」的孤儿记录（LLM 根本没跑）
        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")

        # 写入 user 消息到对话会话
        append_message(conv_id, 'user', text)

        # 交互模式门控：记录（recording）模式下文本输入同样不出声，只记录
        gate = decide_response(text, '', False)
        if not gate.respond:
            logger.info(f"[Gate] 文本入口不响应（{gate_reason_label(gate.reason)}）: {text[:30]!r}")
            return json_ok({"gated": True, "reason": gate.reason})

        # 把该数字人连接绑定到这个会话，后续语音对话会接着聊同一个会话
        set_active_conversation(sessionid, conv_id)

        if params.get('interrupt'):
            avatar_session.flush_talk()

        datainfo = {'conversation_id': conv_id}
        llm_response = request.app.get("llm_response")
        if llm_response:
            asyncio.get_event_loop().run_in_executor(
                None, llm_response, text, avatar_session, datainfo
            )
        return json_ok()
    except Exception as e:
        logger.exception('send_conversation_message exception:')
        return json_error(str(e))


async def interrupt_talk(request):
    """打断当前说话"""
    try:
        params = await request.json()
        sessionid = params.get('sessionid', '')
        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")
        avatar_session.flush_talk()
        return json_ok()
    except Exception as e:
        logger.exception('interrupt_talk exception:')
        return json_error(str(e))


async def humanaudio(request):
    """上传音频文件"""
    try:
        form = await request.post()
        sessionid = str(form.get('sessionid', ''))
        fileobj = form["file"]
        filebytes = fileobj.file.read()

        datainfo = {}

        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")
        avatar_session.put_audio_file(filebytes, datainfo)
        return json_ok()
    except Exception as e:
        logger.exception('humanaudio exception:')
        return json_error(str(e))


async def set_audiotype(request):
    """设置自定义状态（动作编排）"""
    try:
        params = await request.json()
        sessionid = params.get('sessionid', '')
        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")
        avatar_session.set_custom_state(params['audiotype'])
        return json_ok()
    except Exception as e:
        logger.exception('set_audiotype exception:')
        return json_error(str(e))


async def record(request):
    """录制控制"""
    try:
        params = await request.json()
        sessionid = params.get('sessionid', '')
        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")
        if params['type'] == 'start_record':
            avatar_session.start_recording()
        elif params['type'] == 'end_record':
            avatar_session.stop_recording()
        return json_ok()
    except Exception as e:
        logger.exception('record exception:')
        return json_error(str(e))


async def is_speaking(request):
    """查询是否正在说话"""
    params = await request.json()
    sessionid = params.get('sessionid', '')
    avatar_session = get_session(request, sessionid)
    if avatar_session is None:
        return json_error("session not found")
    return json_ok(data=avatar_session.is_speaking())

async def sse_handler(request):
    """SSE 事件流，推送服务器状态更新到客户端"""
    sessionid = request.query.get('sessionid', '')
    avatar_session = session_manager.get_session(sessionid)
    if avatar_session is None:
        return json_error("session not found")

    response = web.StreamResponse(
        status=200,
        reason='OK',
        headers={
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'Access-Control-Allow-Origin': '*',
        }
    )
    await response.prepare(request)

    import queue
    msgqueue = queue.Queue()
    avatar_session.add_msgqueue(msgqueue)

    try:
        while True:
            try:
                msg = msgqueue.get_nowait()
                await response.write(f"data: {msg}\n\n".encode('utf-8'))
            except queue.Empty:
                await asyncio.sleep(0.01)
    except (asyncio.CancelledError, ConnectionResetError):
        logger.info('SSE connection closed for session: %s', sessionid)
    finally:
        if msgqueue in avatar_session.msgqueues:
            avatar_session.msgqueues.remove(msgqueue)

    return response


async def admin_config(request):
    """Admin: 获取全局配置参数"""
    try:
        opt = request.app.get("opt")
        if opt:
            return json_ok(data={"config": vars(opt)})
        return json_error("Config not found")
    except Exception as e:
        logger.exception('admin_config exception:')
        return json_error(str(e))


async def admin_sessions(request):
    """Admin: 获取活跃的会话及其配置"""
    try:
        sessions_info = []
        for sid, avatar_session in session_manager.sessions.items():
            if avatar_session:
                s_opt = getattr(avatar_session, 'opt', None)
                s_data = {
                    "sessionid": sid,
                    "speaking": avatar_session.is_speaking() if hasattr(avatar_session, 'is_speaking') else False,
                    "recording": getattr(avatar_session, 'recording', False),
                }
                if s_opt:
                    s_data.update({
                        "model": getattr(s_opt, "model", ""),
                        "avatar_id": getattr(s_opt, "avatar_id", ""),
                        "REF_FILE": getattr(s_opt, "REF_FILE", ""),
                        "transport": getattr(s_opt, "transport", ""),
                        "batch_size": getattr(s_opt, "batch_size", 0),
                        "customopt": getattr(s_opt, "customopt", []),
                    })
                sessions_info.append(s_data)
        return json_ok(data={"sessions": sessions_info})
    except Exception as e:
        logger.exception('admin_sessions exception:')
        return json_error(str(e))


# ─── 路由注册 ──────────────────────────────────────────────────────────────

async def index(request):
    """默认首页重定向"""
    opt = request.app.get("opt")
    pagename = 'index.html'
    if opt and opt.transport == 'rtmp':
        pagename = 'rtmpapi.html'
    elif opt and opt.transport == 'rtcpush':
        pagename = 'rtcpushapi.html'
    raise web.HTTPFound(f'/{pagename}')


def setup_routes(app):
    """注册所有路由到 aiohttp app"""
    app.router.add_get("/", index)
    app.router.add_post("/human", human)
    app.router.add_post("/api/conversations/{id}/messages", send_conversation_message)
    app.router.add_post("/humanaudio", humanaudio)
    app.router.add_post("/set_audiotype", set_audiotype)
    app.router.add_post("/record", record)
    app.router.add_post("/interrupt_talk", interrupt_talk)
    app.router.add_post("/is_speaking", is_speaking)
    app.router.add_get("/api/admin/config", admin_config)
    app.router.add_get("/api/admin/sessions", admin_sessions)
    app.router.add_get('/sse', sse_handler)

    # ── Local ASR endpoint (SenseVoice/FunASR) ── Issue #604 ──
    try:
        from server.asr_server import asr_websocket_handler, is_funasr_available
        if is_funasr_available():
            app.router.add_get("/api/asr", asr_websocket_handler)
            logger.info("[ASR] Local SenseVoice ASR endpoint enabled at /api/asr")
        else:
            logger.info("[ASR] funasr not installed — local ASR endpoint disabled "
                        "(pip install funasr modelscope)")
    except Exception as e:
        logger.warning(f"[ASR] Failed to register ASR endpoint: {e}")

    # 注册 avatar 生成相关的路由
    setup_avatar_routes(app)

    app.router.add_static('/', path='web')
