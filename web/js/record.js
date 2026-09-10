/* record.js — 会议记录视图：会话选择 + 转写板 + 导出 */
'use strict';

const RecordView = {
    _lastEntries: [],

    init() {
        const sel = document.getElementById('recordConvSelect');
        if (sel) {
            sel.addEventListener('change', () => this.loadBoard());
        }
    },

    onShow() {
        this.loadBoard();
    },

    tick() {
        // 轮询：仅当选中的会话是当前活动会话时刷新（其余会话是静态历史）
        if (App.currentConvId && this._selected() === App.currentConvId) this.loadBoard();
    },

    _selected() {
        const sel = document.getElementById('recordConvSelect');
        return sel ? sel.value : '';
    },

    // 填充会话下拉（chat.js 的 loadConversations 调用）
    refreshConvSelect(convs) {
        const sel = document.getElementById('recordConvSelect');
        if (!sel) return;
        const cur = sel.value;
        sel.innerHTML = convs.map(c =>
            `<option value="${c.id}">${escapeHtml(c.title || '新对话')}（${c.message_count || 0}条）</option>`).join('');
        if (cur && convs.some(c => c.id === cur)) sel.value = cur;
        else if (App.currentConvId && convs.some(c => c.id === App.currentConvId)) sel.value = App.currentConvId;
        if (!sel._loadedOnce) { sel._loadedOnce = true; this.loadBoard(); }
    },

    loadBoard() {
        const convId = this._selected() || App.currentConvId;
        if (!convId) return;
        fetch('/api/conversations/' + encodeURIComponent(convId) + '/messages')
            .then(r => r.json())
            .then(d => {
                if (d.code === 0 && d.data) {
                    this._lastEntries = d.data.entries || [];
                    this.renderBoard(this._lastEntries);
                }
            })
            .catch(() => {});
    },

    renderBoard(entries) {
        const box = document.getElementById('recordBoard');
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
            box.innerHTML = '<div class="text-muted small text-center py-4">选择会话查看转写记录</div>';
            return;
        }
        box.innerHTML = entries.map(e => {
            const t = new Date((e.timestamp || 0) * 1000).toLocaleTimeString('zh-CN', { hour12: false });
            if (e.role === 'assistant') {
                return `<div class="tr-line tr-mine">
                    <span class="tr-time">${t}</span>
                    <span class="spk-tag spk-mine">数字分身</span>
                    <span class="tr-text">${escapeHtml(e.content || '')}</span>
                </div>`;
            }
            const spk = e.speaker || '未知';
            const cls = 'spk-tag spk-c' + speakerColorIndex(spk);
            const renameAttr = /^说话人\d+$/.test(spk) ? ` onclick="promptRenameSpeaker('${escapeHtml(spk)}')" title="点击命名"` : '';
            return `<div class="tr-line">
                <span class="tr-time">${t}</span>
                <span class="${cls}"${renameAttr}>${escapeHtml(spk)}</span>
                <span class="tr-text">${escapeHtml(e.content || '')}</span>
            </div>`;
        }).join('');
        if (box._userScrolledUp !== true) {
            box.scrollTop = box.scrollHeight;
        }
    },
};

// 导出当前选中会话（md / txt）
function exportRecord(fmt) {
    const entries = RecordView._lastEntries;
    if (!entries || !entries.length) {
        ConsoleToast.error('当前没有可导出的记录');
        return;
    }
    const convId = RecordView._selected() || App.currentConvId;
    let content;
    if (fmt === 'md') {
        const lines = ['# 会议记录', '', '> 导出时间：' + new Date().toLocaleString('zh-CN'), ''];
        let lastSpeaker = null;
        entries.forEach(e => {
            const t = new Date((e.timestamp || 0) * 1000).toLocaleTimeString('zh-CN', { hour12: false });
            const spk = e.role === 'assistant' ? '数字分身' : (e.speaker || '未知');
            if (spk !== lastSpeaker) { lines.push('## ' + spk, ''); lastSpeaker = spk; }
            lines.push('- `[' + t + ']` ' + (e.content || ''));
        });
        content = lines.join('\n');
    } else {
        const lines = ['会议记录 ' + new Date().toLocaleString('zh-CN'), ''];
        entries.forEach(e => {
            const t = new Date((e.timestamp || 0) * 1000).toLocaleTimeString('zh-CN', { hour12: false });
            const spk = e.role === 'assistant' ? '数字分身' : (e.speaker || '未知');
            lines.push('[' + t + '] ' + spk + '：' + (e.content || ''));
        });
        content = lines.join('\n');
    }
    // 下载
    const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'meeting_' + (convId ? convId.slice(0, 8) : 'record') + '.' + fmt;
    a.click();
    URL.revokeObjectURL(a.href);
    ConsoleToast.success('已导出 ' + fmt.toUpperCase());
}
