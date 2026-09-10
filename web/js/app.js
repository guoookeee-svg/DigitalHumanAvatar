/* app.js — 全局状态 + 交互模式切换（侧边栏=模式+视图）+ 视图路由
 * 交互逻辑：侧边栏前三项（对话/替会/记录）既是视图切换也是后端交互模式切换，
 * 单一入口不重复；顶栏只显示当前模式标签（只读）。
 */
'use strict';

// 全局应用状态（跨模块共享）
const App = {
    sessionid: null,          // WebRTC sessionid
    _currentConvId: null,     // 当前选中的会话（内部存储）
    mode: 'solo',             // 交互模式 solo | meeting | recording
    meetingCfg: null,         // 替会配置缓存

    // 当前选中的会话 ID。修改时自动同步 ASR iframe 可读的 hidden input (#convid)
    get currentConvId() { return this._currentConvId; },
    set currentConvId(val) {
        this._currentConvId = val;
        const el = document.getElementById('convid');
        if (el) el.value = val || '';
    },
};

// 后端模式 → 前端视图 id 映射
const MODE_VIEW = { solo: 'solo', meeting: 'meeting', recording: 'record' };
const MODE_NAMES = { solo: '对话', meeting: '替会', recording: '记录' };
const MODE_ICONS = { solo: 'bi-chat-dots', meeting: 'bi-people', recording: 'bi-journal-text' };

// 通用工具
function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

// 每个说话人一个稳定颜色（同一名字整场同色）
function speakerColorIndex(name) {
    let h = 0;
    for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) % 8;
    return h;
}

// 更新连接状态徽章（PIP 窗 overlay）
function updateStatus(connected) {
    ConsoleStatus.update('statusBadge', connected);
    const el = document.getElementById('videoOverlay');
    if (el) el.textContent = connected ? '已连接' : '未连接';
}

// 设置 sessionid（供 ASR iframe 读取 + 同步会话）
function setSessionId(sid) {
    App.sessionid = sid;
    document.getElementById('sessionIdDisplay').textContent = 'SID: ' + (sid || '-');
    document.getElementById('sessionid').value = sid || '';
    syncActiveConversation();
}

function showError(msg) {
    const el = document.getElementById('offerError');
    el.textContent = msg;
    el.style.display = 'block';
    ConsoleToast.error(msg);
    setTimeout(() => { el.style.display = 'none'; }, 8000);
}

// ─── 视图切换（纯前端，不动后端模式） ───────────────────────
function switchView(view) {
    document.querySelectorAll('.view-pane').forEach(p => p.classList.remove('active'));
    const pane = document.getElementById('view-' + view);
    if (pane) pane.classList.add('active');
    // 侧边栏高亮：视图对应模式项或功能项
    document.querySelectorAll('.side-item').forEach(b => {
        const m = b.dataset.mode, v = b.dataset.view;
        b.classList.toggle('active', (m && MODE_VIEW[m] === view) || v === view);
    });
    if (view === 'record') RecordView.onShow();
    if (view === 'meeting') MeetingView.onShow();
    if (view === 'speakers') loadSpeakers();
}

// ─── 模式切换（前端视图 + 后端模式一起切） ─────────────────
function setModeUI(mode) {
    App.mode = mode;
    // 顶栏模式标签（只读提示）
    const chip = document.getElementById('modeChip');
    if (chip) chip.innerHTML = '<i class="bi ' + (MODE_ICONS[mode] || 'bi-chat-dots') + '"></i> ' + (MODE_NAMES[mode] || mode);
    switchView(MODE_VIEW[mode] || 'solo');
}

function switchInteractionMode(mode) {
    fetch('/api/mode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode }),
    }).then(r => r.json()).then(d => {
        if (d.code === 0) {
            setModeUI(mode);
            ConsoleToast.success('已切换到「' + MODE_NAMES[mode] + '」模式');
        } else {
            ConsoleToast.error(d.msg || '切换失败');
        }
    }).catch(e => ConsoleToast.error('切换失败: ' + e.message));
}

// 加载替会配置（状态条 + 提示用）
function loadMeetingCfgToState() {
    fetch('/api/meeting/config').then(r => r.json()).then(d => {
        if (d.code === 0 && d.data) {
            App.meetingCfg = d.data;
            const el = document.getElementById('mtOwnerName');
            if (el) el.textContent = d.data.owner_name || '-';
            const pv = document.getElementById('mtProactive');
            if (pv) pv.textContent = { off: '关', low: '低', mid: '中', high: '高' }[d.data.proactive_level] || d.data.proactive_level;
            const hint = document.getElementById('soloOwnerHint');
            if (hint) {
                hint.innerHTML = '<i class="bi bi-person-check"></i> 声纹识别已开启';
            }
            if (document.getElementById('view-speakers').classList.contains('active')) loadSpeakers();
        }
    }).catch(() => {});
}

// 打断数字人
function interrupt() {
    if (!App.sessionid) return;
    fetch('/interrupt_talk', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sessionid: String(App.sessionid) }),
    }).then(r => r.json()).then(d => {
        ConsoleToast.info('已打断');
    });
}

// ─── 初始化 ─────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    // 侧边栏：模式项（data-mode）→ 切后端模式 + 视图；功能项（data-view）→ 仅切视图
    document.querySelectorAll('.side-item').forEach(btn => {
        btn.addEventListener('click', () => {
            if (btn.dataset.mode) switchInteractionMode(btn.dataset.mode);
            else switchView(btn.dataset.view);
        });
    });
    // 侧边栏收起/展开
    document.getElementById('sidebarToggle').addEventListener('click', () => {
        document.body.classList.toggle('sidebar-collapsed');
    });

    // 顶栏 LLM 模式切换（同步对话区下拉、回调后端 /api/llm/mode）
    function initHeaderLlmMode() {
        const sel = document.getElementById('headerLlmMode');
        if (!sel) return;
        // 初始化：加载当前模式
        fetch('/api/llm/mode').then(r => r.json()).then(d => {
            if (d.code === 0 && d.data && d.data.mode) {
                sel.value = d.data.mode;
                // 同步对话区下拉（如存在）
                const chatSel = document.getElementById('chatLlmMode');
                if (chatSel) chatSel.value = d.data.mode;
            }
        }).catch(() => {});
        // 顶栏下拉变更 → 写入后端 + 同步对话区
        sel.addEventListener('change', () => {
            const mode = sel.value;
            fetch('/api/llm/mode', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ mode }),
            }).then(r => r.json()).then(d => {
                if (d.code === 0) {
                    const chatSel = document.getElementById('chatLlmMode');
                    if (chatSel) chatSel.value = mode;
                    ConsoleToast.success('LLM 模式已切换为 ' + (mode === 'agent' ? 'Agent 服务' : 'OpenAI LLM'));
                } else {
                    ConsoleToast.error(d.msg || '切换失败');
                }
            }).catch(e => ConsoleToast.error('切换失败: ' + e.message));
        });
        // 对话区下拉变更时反向同步顶栏
        const chatSel = document.getElementById('chatLlmMode');
        if (chatSel) {
            chatSel.addEventListener('change', () => {
                sel.value = chatSel.value;
            });
        }
    }

    // 加载当前交互模式（持久化的）
    fetch('/api/mode').then(r => r.json()).then(d => {
        if (d.code === 0 && d.data && d.data.mode) setModeUI(d.data.mode);
    }).catch(() => {});
    loadMeetingCfgToState();
    // 模块初始化
    Chat.init();
    MeetingView.init();
    RecordView.init();
    Settings.init();
    initHeaderLlmMode();
    // 定时刷新（按当前视图分流）
    setInterval(() => {
        if (App.currentConvId && document.getElementById('view-solo').classList.contains('active')) refreshMessages();
        if (App.currentConvId && document.getElementById('view-meeting').classList.contains('active')) MeetingView.refresh();
        if (document.getElementById('view-record').classList.contains('active')) RecordView.tick();
    }, 3000);
});
