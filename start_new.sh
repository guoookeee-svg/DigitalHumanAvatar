#!/usr/bin/env bash
# LiveTalking 数字人 - 一键启动/重启脚本（管理 GPT-SoVITS + 主服务）
#   ./start_new.sh          # 启动/重启全部
#   ./start_new.sh stop     # 停止全部
#   ./start_new.sh status   # 查看状态
#   ./start_new.sh log      # 实时看主服务日志
#   ./start_new.sh debug    # 前台运行主服务（Ctrl+C 停止）

set -euo pipefail

# ─── 路径与配置 ─────────────────────────────────────────────
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="$APP_DIR/.venv/bin/python"
ENTRY="$APP_DIR/app.py"
PORT=8010
LOG_FILE="/tmp/livetalking.log"

# GPT-SoVITS 相关
SOVITS_DIR="${SOVITS_DIR:-$(dirname "$APP_DIR")/GPT-SoVITS}"
SOVITS_PY="$SOVITS_DIR/.venv/bin/python"
SOVITS_API="$SOVITS_DIR/api.py"
SOVITS_PORT=9880
SOVITS_LOG="/tmp/gpt_sovits.log"

cd "$APP_DIR"

# ─── 辅助函数 ────────────────────────────────────────────────
color()    { [ -t 1 ] && printf "\033[%sm%s\033[0m" "$1" "$2" || printf "%s" "$2"; }
green()    { color "0;32" "$1"; }
yellow()   { color "0;33" "$1"; }
red()      { color "0;31" "$1"; }
info()     { echo "$(green '[INFO]') $*"; }
warn()     { echo "$(yellow '[WARN]') $*"; }
error()    { echo "$(red '[ERROR]') $*" >&2; }

# 找出占用某端口的 PID
get_pid_on() {
    local p="$1"
    ss -tlnp 2>/dev/null | grep ":$p " | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2 || true
}

stop_port() {
    local p="$1" pid
    pid="$(get_pid_on "$p")"
    if [ -n "$pid" ]; then
        info "端口 $p 占用中（PID=$pid），停止..."
        kill "$pid" 2>/dev/null || true
        for _ in $(seq 1 12); do
            if ! kill -0 "$pid" 2>/dev/null; then break; fi
            sleep 0.5
        done
        if kill -0 "$pid" 2>/dev/null; then
            warn "PID=$pid 未响应，强制结束 (kill -9)"
            kill -9 "$pid" 2>/dev/null || true
            sleep 1
        fi
        info "端口 $p 已释放"
    else
        info "端口 $p 当前无进程"
    fi
}

check_env() {
    if [ ! -f "$VENV_PY" ]; then
        error "未找到主服务虚拟环境 Python：$VENV_PY"; exit 1
    fi
    if [ ! -f "$ENTRY" ]; then
        error "未找到主服务入口文件：$ENTRY"; exit 1
    fi
    if [ ! -f "$SOVITS_PY" ]; then
        warn "未找到 GPT-SoVITS 虚拟环境 Python：$SOVITS_PY（将跳过 TTS 启动）"
    fi
    if [ ! -f "$SOVITS_API" ]; then
        warn "未找到 GPT-SoVITS api.py：$SOVITS_API（将跳过 TTS 启动）"
    fi

    # ── 模型 / 形象素材检查（缺失会启动失败，提前给出明确提示）──
    if [ ! -f "$APP_DIR/models/wav2lip.pth" ]; then
        warn "未找到嘴型模型 models/wav2lip.pth，请下载后放到 models/ 目录（否则数字人无法驱动嘴型）"
    fi
    # 从 config.yaml 读取 avatar_id（不存在则用默认值）
    local av_id
    av_id="$(grep -E '^avatar_id:' "$APP_DIR/config.yaml" 2>/dev/null | awk '{print $2}' | tr -d ' \r\n')"
    av_id="${av_id:-wav2lip256_avatar1}"
    if [ ! -f "$APP_DIR/data/avatars/$av_id/coords.pkl" ]; then
        warn "未找到形象素材 data/avatars/$av_id/coords.pkl，请放置完整形象素材（含 full_imgs/、face_imgs/、coords.pkl），否则启动会失败"
    fi
}

start_sovits() {
    if [ ! -f "$SOVITS_PY" ] || [ ! -f "$SOVITS_API" ]; then
        warn "GPT-SoVITS 未安装，跳过 TTS 服务启动"
        return 0
    fi
    if get_pid_on "$SOVITS_PORT" | grep -q .; then
        info "GPT-SoVITS 已在运行（端口 $SOVITS_PORT），跳过"
        return 0
    fi
    info "启动 GPT-SoVITS TTS 服务（端口 $SOVITS_PORT）..."
    # 必须 cd 到 GPT-SoVITS 根目录，否则 api.py 里 os.getcwd() 得到错误路径，
    # 导致 from text.LangSegmenter import LangSegmenter 报 ModuleNotFoundError
    (cd "$SOVITS_DIR" && nohup "$SOVITS_PY" "$SOVITS_API" -p "$SOVITS_PORT" > "$SOVITS_LOG" 2>&1 &)
    info "GPT-SoVITS 已后台启动，日志：$SOVITS_LOG"
    info "GPT-SoVITS 加载模型需约 20~60 秒，请在主服务就绪后再测试对话"
}

start_bg() {
    info "加载 .env 环境变量..."
    if [ -f "$APP_DIR/.env" ]; then
        set -a; . "$APP_DIR/.env"; set +a
    else
        warn "未找到 .env，跳过（LLM/GPU 配置可能不生效）"
    fi
    info "启动数字人主服务：$ENTRY (端口 $PORT)"
    nohup "$VENV_PY" "$ENTRY" > "$LOG_FILE" 2>&1 &
    info "主服务已启动，PID=$!，日志：$LOG_FILE"
}

wait_ready() {
    for _ in $(seq 1 30); do
        if get_pid_on "$PORT" | grep -q .; then return 0; fi
        sleep 0.5
    done
    return 1
}

health_check() {
    local pid
    pid="$(get_pid_on "$PORT")"
    if [ -n "$pid" ]; then
        info "主服务已启动并监听端口 $PORT (PID=$pid)"
        info "访问地址：http://<服务器IP>:$PORT/console.html"
        echo "----- 主服务日志 -----"
        tail -n 15 "$LOG_FILE" 2>/dev/null || echo "(日志暂空)"
        return 0
    else
        warn "端口 $PORT 未监听，主服务可能启动失败"
        echo "----- 主服务日志末尾 -----"
        tail -n 30 "$LOG_FILE" 2>/dev/null || echo "(无日志)"
        error "请检查上方日志排查启动失败原因"
        return 1
    fi
}

do_start() {
    check_env
    # 停止旧进程
    info "── 检查旧进程 ──"
    stop_port "$PORT"
    stop_port "$SOVITS_PORT"
    # 启动
    info "── 启动服务 ──"
    start_sovits
    start_bg
    info "等待主服务就绪..."
    if ! wait_ready; then
        error "主服务未在预期时间内就绪(15s)"
    fi
    health_check || exit 1
    echo ""
    info "说明：GPT-SoVITS 在后台加载模型，需等待 20~60 秒后才可对话。"
}

case "${1:-start}" in
    stop)
        check_env
        info "── 停止全部服务 ──"
        stop_port "$PORT"
        stop_port "$SOVITS_PORT"
        ;;
    status)
        local_pid="$(get_pid_on "$PORT")"
        sovits_pid="$(get_pid_on "$SOVITS_PORT")"
        if [ -n "$local_pid" ]; then
            info "数字人主服务：运行中 (PID=$local_pid, 端口 $PORT)"
        else
            info "数字人主服务：未运行"
        fi
        if [ -n "$sovits_pid" ]; then
            info "GPT-SoVITS：运行中 (PID=$sovits_pid, 端口 $SOVITS_PORT)"
        else
            info "GPT-SoVITS：未运行"
        fi
        ;;
    log)
        if [ -f "$LOG_FILE" ]; then exec tail -f "$LOG_FILE"; else error "日志不存在：$LOG_FILE"; exit 1; fi
        ;;
    sovits-log)
        if [ -f "$SOVITS_LOG" ]; then exec tail -f "$SOVITS_LOG"; else error "日志不存在：$SOVITS_LOG"; exit 1; fi
        ;;
    debug)
        check_env
        info "前台运行主服务（Ctrl+C 停止）..."
        "$VENV_PY" "$ENTRY"
        ;;
    start|restart)
        do_start
        ;;
    *)
        error "未知命令：$1"
        echo "可用命令：start | stop | restart | status | log | sovits-log | debug"
        exit 1
        ;;
esac
