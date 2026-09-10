/* webrtc.js — WebRTC 连接（negotiate / start / stop）
 * 媒体元素全局单例：一份 video（PIP 悬浮窗）+ 一份 audio，全视图共用。
 * 修复：此前两套 video/audio 同流双播导致声音叠加断续。
 */
'use strict';

let pc = null;

function negotiate() {
    pc.addTransceiver('video', { direction: 'recvonly' });
    pc.addTransceiver('audio', { direction: 'recvonly' });
    return pc.createOffer().then(offer => pc.setLocalDescription(offer)).then(() => {
        return new Promise(resolve => {
            if (pc.iceGatheringState === 'complete') resolve();
            else {
                const check = () => { if (pc.iceGatheringState === 'complete') { pc.removeEventListener('icegatheringstatechange', check); resolve(); } };
                pc.addEventListener('icegatheringstatechange', check);
            }
        });
    }).then(() => {
        const offer = pc.localDescription;
        const body = {
            sdp: offer.sdp,
            type: offer.type,
            avatar: document.getElementById('offerAvatar').value || undefined,
            refaudio: document.getElementById('offerRefAudio').value || undefined,
            reftext: document.getElementById('offerRefText').value || undefined,
        };
        return fetch('/offer', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    }).then(res => res.json()).then(answer => {
        if (answer.code && answer.code !== 0) {
            throw new Error(answer.msg || 'Unknown server error');
        }
        if (!answer.sdp) {
            throw new Error('Server returned no SDP');
        }
        if (answer.sessionid) setSessionId(answer.sessionid);
        return pc.setRemoteDescription(new RTCSessionDescription({ type: 'answer', sdp: answer.sdp }));
    }).catch(e => {
        const msg = e.message || String(e);
        showError(msg);
        if (pc) { pc.close(); pc = null; }
        updateStatus(false);
        setSessionId(null);
        document.getElementById('btnStart').style.display = 'inline-block';
        document.getElementById('btnStop').style.display = 'none';
    });
}

function start() {
    document.getElementById('offerError').style.display = 'none';
    const config = { sdpSemantics: 'unified-plan' };
    pc = new RTCPeerConnection(config);
    // 单一 video/audio（PIP 悬浮窗内），流只绑一次
    pc.addEventListener('track', evt => {
        if (evt.track.kind === 'video') {
            document.getElementById('video').srcObject = evt.streams[0];
        } else {
            document.getElementById('audio').srcObject = evt.streams[0];
        }
    });
    pc.addEventListener('connectionstatechange', () => {
        if (pc.connectionState === 'connected') {
            updateStatus(true);
            ConsoleToast.success('WebRTC 连接成功');
        }
        else if (pc.connectionState === 'disconnected' || pc.connectionState === 'failed') { updateStatus(false); setSessionId(null); }
    });
    negotiate();
    document.getElementById('btnStart').style.display = 'none';
    document.getElementById('btnStop').style.display = 'inline-block';
    // 显示全局悬浮窗（视频 PIP + ASR 采集坞，全视图共用）
    document.getElementById('pipDock').style.display = 'flex';
    document.getElementById('asrDock').style.display = 'flex';
}

function stop() {
    if (pc) { pc.close(); pc = null; }
    updateStatus(false);
    setSessionId(null);
    document.getElementById('btnStart').style.display = 'inline-block';
    document.getElementById('btnStop').style.display = 'none';
    document.getElementById('video').srcObject = null;
    document.getElementById('audio').srcObject = null;
    // 收起悬浮窗
    document.getElementById('pipDock').style.display = 'none';
    document.getElementById('asrDock').style.display = 'none';
    ConsoleToast.info('已断开连接');
}

// 悬浮窗折叠交互
document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('pipToggle').addEventListener('click', () => {
        const body = document.getElementById('pipBody');
        const collapsed = body.style.display === 'none';
        body.style.display = collapsed ? 'block' : 'none';
        document.getElementById('pipToggle').innerHTML =
            collapsed ? '<i class="bi bi-chevron-down"></i>' : '<i class="bi bi-chevron-up"></i>';
    });
    document.getElementById('pipExpand').addEventListener('click', () => {
        document.getElementById('pipDock').classList.toggle('pip-large');
    });
    document.getElementById('asrToggle').addEventListener('click', () => {
        const body = document.getElementById('asrDockBody');
        const collapsed = body.style.display === 'none';
        body.style.display = collapsed ? 'block' : 'none';
        document.getElementById('asrToggle').innerHTML =
            collapsed ? '<i class="bi bi-chevron-down"></i>' : '<i class="bi bi-chevron-up"></i>';
    });
});
