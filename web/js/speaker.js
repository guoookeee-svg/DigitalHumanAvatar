/* speaker.js — 声纹库：注册 / 列表 / 删除 / 设主用户（从原内联迁移 + owner） */
'use strict';

let spkRecorder = null, spkChunks = [];

// 兼容旧入口：打开声纹视图（新布局为独立视图，不再是 modal）
function openSpeakerPanel() {
    switchView('speakers');
}

function loadSpeakers() {
    fetch('/api/speakers').then(r => r.json()).then(d => {
        if (d.code !== 0) return;
        renderSpeakerList(d.data.speakers || []);
    }).catch(() => {});
}

function renderSpeakerList(speakers) {
    const box = document.getElementById('speakerList');
    if (!box) return;
    if (!speakers.length) {
        box.innerHTML = '<div class="text-muted small py-2">暂无声纹。说话后点消息旁的「说话人N」标签即可命名并自动注册；或在此手动录入。</div>';
        return;
    }
    const ownerName = App.meetingCfg ? App.meetingCfg.owner_speaker : '';
    box.innerHTML = speakers.map(s => {
        const avatar = s.role === 'avatar';
        const isOwner = !avatar && ownerName && s.name === ownerName;
        return `<div class="d-flex align-items-center justify-content-between py-2 border-bottom">
            <div>
                <span class="fw-semibold">${isOwner ? '👑 ' : ''}${escapeHtml(s.name)}</span>
                <span class="text-muted small ms-2">${avatar ? '数字人自身（回声滤除用）' : (isOwner ? '主用户' : '参会者')}</span>
            </div>
            ${avatar ? '' : `<div class="d-flex gap-1">
                <button class="btn-outline-custom btn-sm" onclick="setOwnerSpeaker('${escapeHtml(s.name)}')" title="设为主用户：对话模式下唯一能驱动数字人；替会模式下其发言视为指令">${isOwner ? '★ 已是主用户' : '设为主用户'}</button>
                <button class="btn-outline-custom btn-sm" onclick="delSpeaker('${s.id}')" title="删除声纹"><i class="bi bi-x-lg"></i></button>
            </div>`}
        </div>`;
    }).join('');
}

// 设为主用户：写入替会配置的 owner_speaker
function setOwnerSpeaker(name) {
    fetch('/api/meeting/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ owner_speaker: name }),
    }).then(r => r.json()).then(d => {
        if (d.code === 0) {
            ConsoleToast.success('已将「' + name + '」设为主用户');
            loadMeetingCfgToState();
            loadSpeakers();
        } else {
            ConsoleToast.error(d.msg || '设置失败');
        }
    }).catch(e => ConsoleToast.error('设置失败: ' + e.message));
}

// 给会话中的临时说话人命名并注册声纹（由界面点击 "说话人N" 标签触发）
function promptRenameSpeaker(label) {
    const name = prompt('为「' + label + '」命名（将自动注册声纹，下次可自动识别）：', '');
    if (!name || !name.trim()) return;
    const convId = App.currentConvId;
    if (!convId) { ConsoleToast.error('未选中任何会话'); return; }
    fetch('/api/conversations/' + encodeURIComponent(convId) + '/speakers/rename', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label: label, name: name.trim() }),
    }).then(r => r.json()).then(d => {
        if (d.code !== 0) throw new Error(d.msg || '命名失败');
        ConsoleToast.success('已命名并注册声纹：' + name.trim());
        // 刷新当前视图
        if (typeof MeetingView !== 'undefined' && MeetingView.refresh) MeetingView.refresh();
        if (typeof RecordView !== 'undefined' && RecordView.loadBoard) RecordView.loadBoard();
        loadSpeakers();
    }).catch(e => ConsoleToast.error('命名失败: ' + e.message));
}

function delSpeaker(id) {
    if (!confirm('删除这条声纹？')) return;
    fetch('/api/speakers/' + encodeURIComponent(id), { method: 'DELETE' })
        .then(r => r.json()).then(d => {
            if (d.code !== 0) throw new Error(d.msg);
            ConsoleToast.success('已删除');
            loadSpeakers();
        }).catch(e => ConsoleToast.error(e.message));
}

// 录 3-6 秒声纹样本（原内联迁移）
function toggleSpkRecord() {
    const btn = document.getElementById('spkRecordBtn');
    if (spkRecorder && spkRecorder.state === 'recording') {
        spkRecorder.stop();
        return;
    }
    navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } })
        .then(stream => {
            spkChunks = [];
            spkRecorder = new MediaRecorder(stream);
            spkRecorder.ondataavailable = ev => spkChunks.push(ev.data);
            spkRecorder.onstop = () => {
                stream.getTracks().forEach(t => t.stop());
                btn.innerHTML = '<i class="bi bi-mic"></i> 录声纹';
                btn.classList.remove('recording');
                const name = document.getElementById('spkNameInput').value.trim();
                if (!name) {
                    ConsoleToast.error('请先输入姓名再录音');
                    return;
                }
                const blob = new Blob(spkChunks, { type: 'audio/webm' });
                uploadVoiceprint(name, blob);
            };
            spkRecorder.start();
            btn.innerHTML = '<i class="bi bi-stop-circle"></i> 停止录音';
            btn.classList.add('recording');
            ConsoleToast.info('请对着麦克风自然说话 3~6 秒');
        })
        .catch(e => ConsoleToast.error('无法访问麦克风: ' + e.message));
}

// webm 需转成 wav 16k 才能上传（后端只收 wav）
function uploadVoiceprint(name, blob) {
    ConsoleLoading.show('正在注册声纹…');
    const ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
    blob.arrayBuffer().then(ab => ctx.decodeAudioData(ab)).then(buf => {
        const ch = buf.numberOfChannels > 1 ? mixToMono(buf) : buf.getChannelData(0);
        const wav = encodeWav16(ch, 16000);
        const form = new FormData();
        form.append('name', name);
        form.append('audio', new Blob([wav], { type: 'audio/wav' }), 'vp.wav');
        return fetch('/api/speakers', { method: 'POST', body: form });
    }).then(r => r.json()).then(d => {
        ConsoleLoading.hide();
        ctx.close();
        if (d.code !== 0) throw new Error(d.msg || '注册失败');
        ConsoleToast.success('声纹「' + name + '」注册成功');
        document.getElementById('spkNameInput').value = '';
        loadSpeakers();
    }).catch(e => {
        ConsoleLoading.hide();
        ConsoleToast.error('注册声纹失败: ' + e.message);
    });
}

function mixToMono(buf) {
    const out = new Float32Array(buf.length);
    for (let c = 0; c < buf.numberOfChannels; c++) {
        const d = buf.getChannelData(c);
        for (let i = 0; i < d.length; i++) out[i] += d[i] / buf.numberOfChannels;
    }
    return out;
}

// 最小 WAV（16k 16bit PCM）编码器
function encodeWav16(samples, sampleRate) {
    const buf = new ArrayBuffer(44 + samples.length * 2);
    const v = new DataView(buf);
    const wstr = (off, s) => { for (let i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i)); };
    wstr(0, 'RIFF'); v.setUint32(4, 36 + samples.length * 2, true); wstr(8, 'WAVE');
    wstr(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, sampleRate, true); v.setUint32(28, sampleRate * 2, true);
    v.setUint16(32, 2, true); v.setUint16(34, 16, true);
    wstr(36, 'data'); v.setUint32(40, samples.length * 2, true);
    let o = 44;
    for (let i = 0; i < samples.length; i++, o += 2) {
        let s = Math.max(-1, Math.min(1, samples[i]));
        v.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
    }
    return buf;
}
