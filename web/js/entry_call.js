/* entry_call.js — 数字人入口弹窗原型的真实 WebRTC 通话逻辑（独立模块）
 * 对接 LiveTalking 后端：/offer（WebRTC 协商）、/api/asr（本地语音识别）、/human（触发 LLM+TTS）
 * 不复用现有 console.html 的 webrtc.js / app.js，避免影响原系统。
 */
'use strict';

// ─── 全局状态 ─────────────────────────────────────────────
let entryPC = null;              // RTCPeerConnection
let entryMuted = false;
let entryTimers = [];
let entryAsrWs = null;           // ASR WebSocket
let entryRec = null;             // MediaRecorder（录音）
let entryRecChunks = [];
let entryAsrStream = null;       // 麦克风流
let entrySessionId = '';         // 数字人连接 sessionid
let entrySpeakingFlag = false;   // 是否正在识别
let entryConvId = '';            // 当前会话 id

// ─── 动态注入隐藏元素（供 ASR 读取）───────────────────────
function ensureHiddenInputs() {
    if (!document.getElementById('sessionid')) {
        const s = document.createElement('input');
        s.type = 'hidden'; s.id = 'sessionid'; s.value = '';
        document.body.appendChild(s);
    }
    if (!document.getElementById('convid')) {
        const c = document.createElement('input');
        c.type = 'hidden'; c.id = 'convid'; c.value = '';
        document.body.appendChild(c);
    }
    if (!document.getElementById('offerError')) {
        const e = document.createElement('div');
        e.id = 'offerError'; e.style.display = 'none';
        document.body.appendChild(e);
    }
}

// ─── 工具函数 ─────────────────────────────────────────────
function esc(s) {
    return String(s || '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

function entrySetPhase(txt, subHtml) {
    const ph = document.getElementById('cPhase');
    const sub = document.getElementById('cSub');
    if (ph) ph.textContent = txt;
    if (sub) sub.innerHTML = subHtml;
}

function entrySetState(txt, color) {
    const st = document.getElementById('cStateTxt');
    if (st) { st.textContent = txt; st.style.color = color || '#22c55e'; }
}

// ─── WebRTC 协商与连接 ────────────────────────────────────
function entryNegotiate() {
    entryPC.addTransceiver('video', { direction: 'recvonly' });
    entryPC.addTransceiver('audio', { direction: 'recvonly' });
    return entryPC.createOffer()
        .then(offer => entryPC.setLocalDescription(offer))
        .then(() => new Promise(resolve => {
            if (entryPC.iceGatheringState === 'complete') return resolve();
            const check = () => {
                if (entryPC.iceGatheringState === 'complete') {
                    entryPC.removeEventListener('icegatheringstatechange', check);
                    resolve();
                }
            };
            entryPC.addEventListener('icegatheringstatechange', check);
        }))
        .then(() => {
            const offer = entryPC.localDescription;
            const body = {
                sdp: offer.sdp,
                type: offer.type,
                avatar: (document.getElementById('offerAvatar') || {}).value || 'wav2lip256_avatar1',
            };
            return fetch('/offer', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
        })
        .then(res => res.json())
        .then(answer => {
            if (answer.code && answer.code !== 0) throw new Error(answer.msg || '服务端错误');
            if (!answer.sdp) throw new Error('服务端未返回 SDP');
            if (answer.sessionid) {
                entrySessionId = answer.sessionid;
                const si = document.getElementById('sessionid');
                if (si) si.value = answer.sessionid;
            }
            return entryPC.setRemoteDescription(new RTCSessionDescription({ type: 'answer', sdp: answer.sdp }));
        })
        .catch(e => {
            entrySetState('● 连接失败', '#ef4444');
            entrySetPhase('连接失败：' + (e.message || e));
            entryCleanup();
        });
}

function entryStartCall() {
    entrySetState('● 连接中…', '#f59e0b');
    entrySetPhase('正在建立实时语音通道…');
    ensureHiddenInputs();
    const config = { sdpSemantics: 'unified-plan' };
    entryPC = new RTCPeerConnection(config);
    entryPC.addEventListener('track', evt => {
        if (evt.track.kind === 'video') {
            const v = document.getElementById('cVideo');
            if (v) { v.srcObject = evt.streams[0]; v.classList.add('on'); }
        } else {
            // audio 自动播放
            const a = document.createElement('audio');
            a.autoplay = true; a.srcObject = evt.streams[0];
            document.body.appendChild(a);
        }
    });
    entryPC.addEventListener('connectionstatechange', () => {
        if (entryPC && entryPC.connectionState === 'connected') {
            entrySetState('● 通话中', '#22c55e');
            entrySetPhase('正在聆听，请说话…');
            entryStartAsr();
        } else if (entryPC && (entryPC.connectionState === 'disconnected' || entryPC.connectionState === 'failed')) {
            entrySetState('● 已断开', '#8a94a6');
        }
    });
    entryNegotiate();
    entrySetState('● 连接中…', '#f59e0b');
}

function entryCleanup() {
    if (entryPC) { entryPC.close(); entryPC = null; }
    entryStopAsr();
    entryTimers.forEach(clearTimeout); entryTimers = [];
    const v = document.getElementById('cVideo');
    if (v) { v.srcObject = null; v.classList.remove('on'); }
    const si = document.getElementById('sessionid');
    if (si) si.value = '';
    entrySessionId = '';
}

function entryEndCall() {
    entryCleanup();
    const win = document.getElementById('callWin');
    if (win) win.classList.remove('show');
}

function entryToggleMute() {
    entryMuted = !entryMuted;
    const b = document.getElementById('muteBtn');
    if (b) { b.classList.toggle('muted', entryMuted); b.textContent = entryMuted ? '🔇' : '🎤'; }
    // 静音本地麦克风采集
    if (entryAsrStream) entryAsrStream.getAudioTracks().forEach(t => { t.enabled = !entryMuted; });
}

// ─── 本地 ASR（对接 /api/asr）──────────────────────────────
function entryStartAsr() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        entrySetPhase('当前浏览器不支持麦克风，无法语音对话（可改用管理页文本输入）');
        return;
    }
    navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } })
        .then(stream => {
            entryAsrStream = stream;
            const protocol = location.protocol === 'https:' ? 'wss://' : 'ws://';
            const host = location.host;
            entryAsrWs = new WebSocket(protocol + host + '/api/asr');
            entryAsrWs.onopen = () => {
                entryAsrWs.send(JSON.stringify({
                    chunk_size: [5, 10, 5], wav_name: 'h5', is_speaking: true,
                    chunk_interval: 10, mode: '2pass', itn: false, sessionid: entrySessionId,
                }));
                entryStartRecord(stream);
            };
            entryAsrWs.onmessage = e => {
                try {
                    const d = JSON.parse(e.data);
                    const txt = (d.text || '').replace(/ +/g, '');
                    if (txt) {
                        entrySetPhase('识别中…', '<span class="who">你：</span>' + esc(txt));
                        if (d.is_final && !d.is_self) {
                            entrySendHuman(txt, d.audio, d.speaker, d.speaker_conf);
                        }
                    }
                } catch (err) { /* ignore */ }
            };
            entryAsrWs.onclose = () => {};
            entryAsrWs.onerror = () => {};
        })
        .catch(() => entrySetPhase('无法访问麦克风'));
}

function entryStartRecord(stream) {
    entryRecChunks = [];
    try {
        entryRec = new MediaRecorder(stream);
        entryRec.ondataavailable = ev => { entryRecChunks.push(ev.data); };
        entryRec.onstop = () => {};
        entryRec.start();
    } catch (e) { /* MediaRecorder 不可用时跳过 */ }
}

function entryStopAsr() {
    if (entryRec && entryRec.state !== 'inactive') { try { entryRec.stop(); } catch (e) {} entryRec = null; }
    if (entryAsrWs) { try { entryAsrWs.close(); } catch (e) {} entryAsrWs = null; }
    if (entryAsrStream) { entryAsrStream.getTracks().forEach(t => t.stop()); entryAsrStream = null; }
}

// 识别出一句 → 发送给数字人（触发 LLM + TTS）
function entrySendHuman(text, audio, speaker, speakerConf) {
    if (!entrySessionId || !text) return;
    fetch('/human', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            text: text,
            type: 'chat',
            interrupt: true,
            sessionid: entrySessionId,
            conversation_id: (document.getElementById('convid') || {}).value || '',
            audio: audio || '',
            speaker: speaker || '',
            speaker_conf: speakerConf || 0,
        }),
    }).catch(() => {});
}

// ─── 暴露给页面 onclick ───────────────────────────────────
// 覆盖原型里的 startCall/endCall/toggleMute
window.startCall = function (name, word) {
    const win = document.getElementById('callWin');
    if (win) {
        const cn = document.getElementById('cName'); if (cn) cn.textContent = name;
        const cf = document.getElementById('cFace'); if (cf) cf.textContent = word;
        win.classList.add('show');
    }
    entryStartCall();
};
window.endCall = entryEndCall;
window.toggleMute = entryToggleMute;

document.addEventListener('DOMContentLoaded', () => {
    ensureHiddenInputs();
});
