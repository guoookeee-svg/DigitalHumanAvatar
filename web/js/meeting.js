/* meeting.js — 替会视图：与会人列表 / 发言流（决策徽章）/ 手动插话 / 替会配置 */
'use strict';

// 决策原因 → 徽章文案与颜色
const GATE_BADGES = {
    owner:      { text: '主用户指令', cls: 'gate-owner' },
    addressed:  { text: '点名→回答', cls: 'gate-respond' },
    asked:      { text: '提问→回答', cls: 'gate-respond' },
    topic:      { text: '议题→插话', cls: 'gate-respond' },
    echo:       { text: '回声滤除', cls: 'gate-muted' },
    bystander:  { text: '闲聊→沉默', cls: 'gate-muted' },
    recording:  { text: '仅记录', cls: 'gate-muted' },
    passthrough:{ text: '直通', cls: 'gate-respond' },
};

const MeetingView = {
    _lastMsgCount: -1,

    init() {
        // 会话选择下拉框
        const sel = document.getElementById('meetingConvSelect');
        if (sel) {
            sel.addEventListener('change', () => {
                const id = sel.value;
                if (id) {
                    App.currentConvId = id;
                    bindActiveConversation(id);
                    this.onShow();
                }
            });
        }
    },

    // 视图激活：加载当前会话的发言流 + 状态
    onShow() {
        const sel = document.getElementById('meetingConvSelect');
        // 没有下拉框值但有 currentConvId 时同步下拉
        if (sel && App.currentConvId && (!sel.value || sel.value !== App.currentConvId)) {
            sel.value = App.currentConvId;
        }
        this.refresh();
        const el = document.getElementById('mtConvTitle');
        if (el && App.currentConvId) el.textContent = '会话 ' + App.currentConvId.slice(0, 8);
    },

    // 拉取当前会话消息 → 渲染发言流 + 与会人
    refresh() {
        if (!App.currentConvId) return;
        fetch('/api/conversations/' + encodeURIComponent(App.currentConvId) + '/messages')
            .then(r => r.json())
            .then(d => {
                if (d.code === 0 && d.data) this.render(d.data.entries || []);
            })
            .catch(() => {});
    },

    render(entries) {
        const box = document.getElementById('mtTranscript');
        if (!box) return;
        // 初始化滚动监听（仅一次）
        if (!box._scrollInited) {
            box._scrollInited = true;
            box.addEventListener('scroll', function() {
                const d = this.scrollHeight - this.scrollTop - this.clientHeight;
                this._userScrolledUp = d > 60;
            });
        }
        if (!entries.length) {
            box.innerHTML = '<div class="text-muted small text-center py-4">连接数字人后，会议发言将实时转写到这里</div>';
            return;
        }
        // 发言流：每条 = 说话人色标 + 时间 + 文本 + 决策徽章；assistant = 我方回复高亮
        box.innerHTML = entries.map(e => {
            const t = new Date((e.timestamp || 0) * 1000).toLocaleTimeString('zh-CN', { hour12: false });
            if (e.role === 'assistant') {
                return `<div class="tr-line tr-mine">
                    <span class="tr-time">${t}</span>
                    <span class="spk-tag spk-mine">我方（数字分身）</span>
                    <span class="tr-text">${escapeHtml(e.content || '')}</span>
                </div>`;
            }
            const spk = e.speaker || '未知';
            const isTemp = /^说话人\d+$/.test(spk);
            const spkCls = 'spk-tag spk-c' + speakerColorIndex(spk);
            const renameAttr = isTemp ? ` onclick="promptRenameSpeaker('${escapeHtml(spk)}')" title="点击命名"` : '';
            const owner = App.meetingCfg && App.meetingCfg.owner_speaker === spk;
            // 决策徽章：带 gate_reason 的显示原因；无徽章的 user 消息（走 LLM 的）显示为"已回复"
            let badge = '';
            if (e.gate_reason && GATE_BADGES[e.gate_reason]) {
                const b = GATE_BADGES[e.gate_reason];
                badge = `<span class="gate-badge ${b.cls}">${b.text}</span>`;
            }
            return `<div class="tr-line">
                <span class="tr-time">${t}</span>
                <span class="${spkCls}"${renameAttr}>${owner ? '👑 ' : ''}${escapeHtml(spk)}</span>
                <span class="tr-text">${escapeHtml(e.content || '')}</span>
                ${badge}
            </div>`;
        }).join('');
        if (box._userScrolledUp !== true) {
            box.scrollTop = box.scrollHeight;
        }

        // 与会人统计（user 消息按 speaker 分组）
        const counts = {};
        entries.filter(e => e.role === 'user').forEach(e => {
            const k = e.speaker || '未知';
            counts[k] = (counts[k] || 0) + 1;
        });
        this.renderAttendees(counts);
        // 我方发言数
        const mine = entries.filter(e => e.role === 'assistant').length;
        const rc = document.getElementById('mtReplyCount');
        if (rc) rc.textContent = mine;
    },

    renderAttendees(counts) {
        const box = document.getElementById('mtAttendees');
        if (!box) return;
        const names = Object.keys(counts);
        const sc = document.getElementById('mtSpeakerCount');
        if (sc) sc.textContent = names.length;
        if (!names.length) {
            box.innerHTML = '<div class="text-muted small text-center py-2">等待发言…</div>';
            return;
        }
        box.innerHTML = names.map(n => {
            const owner = App.meetingCfg && App.meetingCfg.owner_speaker === n;
            const cls = 'spk-tag spk-c' + speakerColorIndex(n);
            const renameAttr = /^说话人\d+$/.test(n) ? ` onclick="promptRenameSpeaker('${escapeHtml(n)}')" title="点击命名"` : '';
            return `<div class="attendee-item">
                <span class="attendee-avatar ${cls}">${owner ? '👑' : escapeHtml(n.slice(0, 1))}</span>
                <span class="attendee-name"${renameAttr}>${escapeHtml(n)}${owner ? '（主用户）' : ''}</span>
                <span class="attendee-count">${counts[n]} 条</span>
            </div>`;
        }).join('');
    },

    // 填充会话下拉框（由 chat.js loadConversations 调用）
    refreshConvSelect(convs) {
        const sel = document.getElementById('meetingConvSelect');
        if (!sel) return;
        const cur = sel.value;
        sel.innerHTML = convs.map(c =>
            `<option value="${c.id}">${escapeHtml(c.title || '新对话')}（${c.message_count || 0}条）</option>`).join('');
        if (cur && convs.some(c => c.id === cur)) sel.value = cur;
        else if (App.currentConvId && convs.some(c => c.id === App.currentConvId)) sel.value = App.currentConvId;
    },
};

// 手动插话：随时让数字人代表用户发言（echo 直通，绕过门控）
function meetingManualSpeak() {
    if (!App.sessionid) {
        ConsoleToast.error('请先连接数字人（WebRTC）');
        return;
    }
    const text = prompt('让数字人代表你说什么？', '');
    if (!text || !text.trim()) return;
    fetch('/human', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            text: text.trim(),
            type: 'chat',
            interrupt: true,
            sessionid: String(App.sessionid),
            bypass: true,  // 手动插话：用户明确要求发言，绕过模式门控
        }),
    }).then(r => r.json()).then(d => {
        if (d.code === 0) ConsoleToast.success('数字人即将发言');
        else ConsoleToast.error(d.msg || '发送失败');
    }).catch(e => ConsoleToast.error('发送失败: ' + e.message));
}

// 跳转到设置的替会配置页
function openMeetingCfg() {
    switchView('settings');
    // 激活替会配置 tab
    const tab = document.querySelector('[data-bs-target="#set-meeting"]');
    if (tab) bootstrap.Tab.getOrCreateInstance(tab).show();
    loadMeetingCfg();
}

// 替会配置读写（settings 页 #set-meeting）
function loadMeetingCfg() {
    fetch('/api/meeting/config').then(r => r.json()).then(d => {
        if (d.code === 0 && d.data) {
            const c = d.data;
            document.getElementById('mtCfgOwnerName').value = c.owner_name || '';
            document.getElementById('mtCfgOwnerSpeaker').value = c.owner_speaker || '';
            document.getElementById('mtCfgKeywords').value = (c.address_keywords || []).join(',');
            document.getElementById('mtCfgProactive').value = c.proactive_level || 'off';
        }
    }).catch(e => ConsoleToast.error('加载替会配置失败: ' + e.message));
}

function saveMeetingCfg() {
    const keywords = document.getElementById('mtCfgKeywords').value
        .split(/[,，]/).map(s => s.trim()).filter(Boolean);
    const data = {
        owner_name: document.getElementById('mtCfgOwnerName').value.trim(),
        owner_speaker: document.getElementById('mtCfgOwnerSpeaker').value.trim(),
        address_keywords: keywords,
        proactive_level: document.getElementById('mtCfgProactive').value,
    };
    fetch('/api/meeting/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
    }).then(r => r.json()).then(d => {
        if (d.code === 0) {
            ConsoleToast.success('替会配置已保存');
            loadMeetingCfgToState();
        } else {
            ConsoleToast.error(d.msg || '保存失败');
        }
    }).catch(e => ConsoleToast.error('保存失败: ' + e.message));
}
