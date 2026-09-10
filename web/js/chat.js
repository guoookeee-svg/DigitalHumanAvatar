/* chat.js — solo 视图：会话管理 / 消息渲染 / 文本与语音输入 / 说话人精修入口 */
'use strict';

const Chat = {

    init() {
        // 输入框 Enter 发送，Shift+Enter 换行
        const chatInput = document.getElementById('chatInput');
        if (chatInput) {
            chatInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    sendChatMessage();
                }
            });
        }
        // 对话区 LLM 模式切换
        const chatMode = document.getElementById('chatLlmMode');
        if (chatMode) {
            fetch('/api/llm/mode').then(r => r.json()).then(d => {
                if (d.code === 0 && d.data && d.data.mode) chatMode.value = d.data.mode;
            }).catch(() => {});
            chatMode.addEventListener('change', () => switchLlmMode(chatMode.value));
        }
        loadConversations();
    },
};

// ─── 会话管理（原内联迁移） ───────────────────────────────

function bindActiveConversation(convId) {
    if (!convId || !App.sessionid) return;
    fetch('/api/conversations/active', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sessionid: String(App.sessionid), conversation_id: convId })
    }).catch(e => console.log('bind active conversation error:', e));
}

function syncActiveConversation() {
    if (!App.sessionid) return;
    fetch('/api/conversations/active?sessionid=' + encodeURIComponent(App.sessionid))
        .then(r => r.json())
        .then(d => {
            if (d.code === 0 && d.data && d.data.conversation_id) {
                App.currentConvId = d.data.conversation_id;
                loadConversations();
                refreshMessages();
                MeetingView.onShow();
            }
        })
        .catch(e => console.log('sync active conversation error:', e));
}

function newConversation() {
    fetch('/api/conversations', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: '新对话' }),
    }).then(r => r.json()).then(d => {
        if (d.code === 0 && d.data) {
            App.currentConvId = d.data.id;
            bindActiveConversation(d.data.id);
            loadConversations();
            renderMessages([]);
            ConsoleToast.success('已新建会话');
        }
    }).catch(e => ConsoleToast.error('新建会话失败: ' + e.message));
}

function loadConversations() {
    fetch('/api/conversations').then(r => r.json()).then(d => {
        if (d.code === 0 && d.data) {
            renderConversationList(d.data.conversations || []);
            RecordView.refreshConvSelect(d.data.conversations || []);
            MeetingView.refreshConvSelect(d.data.conversations || []);
        }
    }).catch(e => console.log('load conversations error:', e));
}

function renderConversationList(convs) {
    const list = document.getElementById('conversationList');
    if (!list) return;
    if (!convs || convs.length === 0) {
        list.innerHTML = '<div class="text-muted small text-center py-3">暂无会话</div>';
        return;
    }
    list.innerHTML = convs.map(c => {
        const active = c.id === App.currentConvId ? ' active' : '';
        const title = escapeHtml(c.title || '新对话');
        return `<div class="chat-conv-item${active}" onclick="selectConversation('${c.id}')">
            <div class="chat-conv-title">${title}</div>
            <div class="chat-conv-meta">${c.message_count || 0} 条消息</div>
            <button class="chat-conv-del" onclick="event.stopPropagation();deleteConversation('${c.id}')" title="删除会话"><i class="bi bi-x"></i></button>
        </div>`;
    }).join('');
}

function selectConversation(id) {
    App.currentConvId = id;
    bindActiveConversation(id);
    loadConversations();
    fetch('/api/conversations/' + encodeURIComponent(id) + '/messages').then(r => r.json()).then(d => {
        if (d.code === 0 && d.data) {
            renderMessages(d.data.entries || []);
        }
    }).catch(e => console.log('load messages error:', e));
}

function deleteConversation(id) {
    if (!confirm('确定删除该会话吗？')) return;
    fetch('/api/conversations/' + encodeURIComponent(id), { method: 'DELETE' }).then(r => r.json()).then(d => {
        if (d.code === 0) {
            if (App.currentConvId === id) {
                App.currentConvId = null;
                renderMessages([]);
            }
            loadConversations();
            ConsoleToast.success('会话已删除');
        }
    }).catch(e => ConsoleToast.error('删除失败: ' + e.message));
}

// ─── 消息渲染（原内联迁移） ───────────────────────────────

function renderMessages(entries) {
    const box = document.getElementById('chatMessages');
    if (!box) return;
    // 初始化滚动监听（仅一次）：用户主动向上滚时标记，滚到底部时取消标记
    if (!box._scrollInited) {
        box._scrollInited = true;
        box.addEventListener('scroll', function() {
            const distFromBottom = this.scrollHeight - this.scrollTop - this.clientHeight;
            if (distFromBottom > 60) {
                this._userScrolledUp = true;
            } else {
                this._userScrolledUp = false;
            }
        });
    }
    if (!entries || entries.length === 0) {
        box.innerHTML = '<div class="text-muted small text-center py-4">开始对话吧</div>';
        return;
    }
    box.innerHTML = entries.map(e => {
        const isUser = e.role === 'user';
        const cls = isUser ? 'msg-user' : 'msg-assistant';
        const spk = (isUser && e.speaker) ? String(e.speaker) : null;
        const label = spk || (isUser ? '我' : '数字人');
        const spkCls = spk ? ' spk-tag spk-c' + speakerColorIndex(spk) : '';
        const renameAttr = (spk && /^说话人\d+$/.test(spk))
            ? ` onclick="promptRenameSpeaker('${escapeHtml(spk)}')" title="点击给这位说话人命名"` : '';
        return `<div class="chat-msg ${cls}">
            <div class="chat-msg-label${spkCls}"${renameAttr}>${escapeHtml(label)}</div>
            <div class="chat-msg-content">${escapeHtml(e.content || '')}</div>
        </div>`;
    }).join('');
    // 自动滚到底部：仅当用户没有主动向上滚动时
    if (box._userScrolledUp !== true) {
        box.scrollTop = box.scrollHeight;
    }
}

function sendChatMessage() {
    const input = document.getElementById('chatInput');
    const text = input.value.trim();
    if (!text) return;
    if (!App.currentConvId) {
        ConsoleToast.error('请先新建或选择一个会话');
        return;
    }
    if (!App.sessionid) {
        ConsoleToast.error('请先连接数字人（WebRTC）');
        return;
    }
    const box = document.getElementById('chatMessages');
    if (box && box.querySelector('.text-muted')) box.innerHTML = '';
    box.insertAdjacentHTML('beforeend',
        `<div class="chat-msg msg-user"><div class="chat-msg-label">我</div><div class="chat-msg-content">${escapeHtml(text)}</div></div>`);
    if (box._userScrolledUp !== true) {
        box.scrollTop = box.scrollHeight;
    }
    input.value = '';

    fetch('/api/conversations/' + encodeURIComponent(App.currentConvId) + '/messages', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, sessionid: String(App.sessionid), interrupt: true }),
    }).then(r => r.json()).then(d => {
        if (d.code !== 0) {
            ConsoleToast.error(d.msg || '发送失败');
        }
        setTimeout(() => refreshMessages(), 800);
    }).catch(e => ConsoleToast.error('发送失败: ' + e.message));
}

function refreshMessages() {
    if (!App.currentConvId) return;
    fetch('/api/conversations/' + encodeURIComponent(App.currentConvId) + '/messages').then(r => r.json()).then(d => {
        if (d.code === 0 && d.data) {
            renderMessages(d.data.entries || []);
        }
    }).catch(() => {});
}

function clearConversation() {
    if (!App.currentConvId) {
        ConsoleToast.error('请先选择会话');
        return;
    }
    if (!confirm('确定清空当前会话的消息吗？')) return;
    fetch('/api/conversations/' + encodeURIComponent(App.currentConvId) + '/messages', { method: 'DELETE' }).then(r => r.json()).then(d => {
        if (d.code === 0) {
            renderMessages([]);
            ConsoleToast.success('会话已清空');
        }
    }).catch(e => ConsoleToast.error('清空失败: ' + e.message));
}

function switchLlmMode(mode) {
    fetch('/api/llm/mode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode }),
    }).then(r => r.json()).then(d => {
        if (d.code === 0) {
            ConsoleToast.success('LLM 模式已切换为 ' + (mode === 'agent' ? 'Agent 服务' : 'OpenAI LLM'));
        } else {
            ConsoleToast.error(d.msg || '切换失败');
        }
    }).catch(e => ConsoleToast.error('切换失败: ' + e.message));
}

// ─── 语音输入（Web Speech API，原内联迁移） ─────────────────

let voiceRecognition = null;
let voiceListening = false;
function toggleVoiceInput() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
        ConsoleToast.error('当前浏览器不支持语音识别');
        return;
    }
    if (voiceListening) {
        voiceRecognition.stop();
        return;
    }
    if (!voiceRecognition) {
        voiceRecognition = new SR();
        voiceRecognition.lang = 'zh-CN';
        voiceRecognition.continuous = false;
        voiceRecognition.interimResults = false;
        voiceRecognition.onresult = (event) => {
            const text = event.results[0][0].transcript;
            const input = document.getElementById('chatInput');
            input.value = (input.value ? input.value + ' ' : '') + text;
            voiceListening = false;
            document.getElementById('voiceBtn').classList.remove('listening');
            ConsoleToast.success('已识别语音');
        };
        voiceRecognition.onerror = (e) => {
            voiceListening = false;
            document.getElementById('voiceBtn').classList.remove('listening');
            ConsoleToast.error('语音识别错误: ' + e.error);
        };
        voiceRecognition.onend = () => {
            voiceListening = false;
            document.getElementById('voiceBtn').classList.remove('listening');
        };
    }
    voiceListening = true;
    document.getElementById('voiceBtn').classList.add('listening');
    voiceRecognition.start();
}

// ─── 说话人分离（会后精修 + 改名，原内联迁移） ───────────────

function runDiarize() {
    if (!App.currentConvId) {
        ConsoleToast.error('请先选择一个会话');
        return;
    }
    ConsoleLoading.show('正在分析说话人（会后全局聚类）…');
    fetch('/api/conversations/' + encodeURIComponent(App.currentConvId) + '/diarize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({})
    }).then(r => r.json()).then(d => {
        if (d.code !== 0) throw new Error(d.msg || '触发失败');
        return pollDiarize();
    }).then(d => {
        ConsoleLoading.hide();
        if (d.status === 'done') {
            ConsoleToast.success('分析完成：' + (d.msg || ''));
            if (App.currentConvId) refreshMessages();
        } else if (d.status === 'skipped') {
            ConsoleToast.info(d.msg || '语音段不足，跳过');
        } else {
            ConsoleToast.error(d.msg || '分析失败');
        }
    }).catch(e => {
        ConsoleLoading.hide();
        ConsoleToast.error('识别说话人失败: ' + e.message);
    });
}

function pollDiarize() {
    return new Promise((resolve, reject) => {
        let tries = 0;
        const tick = () => {
            if (++tries > 180) return reject(new Error('分析超时'));
            fetch('/api/conversations/' + encodeURIComponent(App.currentConvId) + '/diarize')
                .then(r => r.json())
                .then(d => {
                    if (d.code !== 0) return reject(new Error(d.msg));
                    const st = d.data || {};
                    if (['done', 'error', 'skipped'].includes(st.status)) return resolve(st);
                    setTimeout(tick, 1000);
                })
                .catch(reject);
        };
        tick();
    });
}

function promptRenameSpeaker(label) {
    if (!App.currentConvId) return;
    const name = prompt('给「' + label + '」起个名字（将注册声纹，下次自动识别）：', '');
    if (!name || !name.trim()) return;
    fetch('/api/conversations/' + encodeURIComponent(App.currentConvId) + '/speakers/rename', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label: label, name: name.trim() })
    }).then(r => r.json()).then(d => {
        if (d.code !== 0) throw new Error(d.msg || '命名失败');
        ConsoleToast.success('已命名为「' + name.trim() + '」，下次自动识别');
        if (App.currentConvId) refreshMessages();
    }).catch(e => ConsoleToast.error(e.message));
}
