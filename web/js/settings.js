/* settings.js — 设置页：LLM 配置 / 人设（双轨）/ 记忆管理（原内联迁移 + 替会人设） */
'use strict';

const Settings = {
    init() {
        document.getElementById('llmConfigMode').addEventListener('change', toggleConfigSections);
        loadLlmConfig();
        loadMemory();
        loadMeetingCfg();
    },
};

function toggleConfigSections() {
    const mode = document.getElementById('llmConfigMode').value;
    document.getElementById('openaiConfigSection').style.display = mode === 'openai' ? 'block' : 'none';
    document.getElementById('agentConfigSection').style.display = mode === 'agent' ? 'block' : 'none';
}

function loadLlmConfig() {
    fetch('/api/llm/config').then(r => r.json()).then(d => {
        if (d.code === 0 && d.data) {
            const cfg = d.data;
            document.getElementById('llmConfigMode').value = cfg.mode || 'openai';
            toggleConfigSections();
            const oa = cfg.openai || {};
            document.getElementById('openaiBaseUrl').value = oa.base_url || '';
            document.getElementById('openaiApiKey').value = oa.api_key || '';
            document.getElementById('openaiModel').value = oa.model || '';
            document.getElementById('openaiEmbedModel').value = oa.embed_model || '';
            document.getElementById('openaiRerankModel').value = oa.rerank_model || '';
            const ag = cfg.agent || {};
            document.getElementById('agentHost').value = ag.host || '';
            document.getElementById('agentPort').value = ag.port || '';
            document.getElementById('agentPath').value = ag.path || '';
            document.getElementById('agentId').value = ag.agent_id || '';
            document.getElementById('agentSession').value = ag.session_id || '';
            document.getElementById('agentResponseType').value = ag.response_type || 'sse';
            document.getElementById('agentResponsePath').value = ag.response_text_path || 'text';
            document.getElementById('agentHeaders').value = JSON.stringify(ag.headers || {}, null, 2);
            document.getElementById('agentBodyTemplate').value = JSON.stringify(ag.body_template || {}, null, 2);
        }
    }).catch(e => ConsoleToast.error('加载配置失败: ' + e.message));
    // 人设（独立接口，含替会人设）
    fetch('/api/persona').then(r => r.json()).then(d => {
        if (d.code === 0 && d.data) {
            document.getElementById('personaPrompt').value = d.data.system_prompt || '';
            document.getElementById('personaMeetingPrompt').value = d.data.meeting_system_prompt || '';
            document.getElementById('personaStyle').value = d.data.reply_style || '';
            document.getElementById('personaMaxTokens').value = d.data.max_tokens || 200;
            document.getElementById('personaContextRounds').value = d.data.context_rounds || 20;
        }
    }).catch(() => {});
}

function saveLlmConfig() {
    const mode = document.getElementById('llmConfigMode').value;
    let headers = {}, bodyTemplate = {};
    try {
        headers = JSON.parse(document.getElementById('agentHeaders').value || '{}');
    } catch (e) {
        ConsoleToast.error('请求头 JSON 格式错误');
        return;
    }
    try {
        bodyTemplate = JSON.parse(document.getElementById('agentBodyTemplate').value || '{}');
    } catch (e) {
        ConsoleToast.error('请求体模板 JSON 格式错误');
        return;
    }

    const data = {
        mode,
        openai: {
            base_url: document.getElementById('openaiBaseUrl').value,
            api_key: document.getElementById('openaiApiKey').value,
            model: document.getElementById('openaiModel').value,
            embed_model: document.getElementById('openaiEmbedModel').value,
            rerank_model: document.getElementById('openaiRerankModel').value,
        },
        agent: {
            host: document.getElementById('agentHost').value,
            port: document.getElementById('agentPort').value,
            path: document.getElementById('agentPath').value,
            agent_id: document.getElementById('agentId').value,
            session_id: document.getElementById('agentSession').value,
            response_type: document.getElementById('agentResponseType').value,
            response_text_path: document.getElementById('agentResponsePath').value,
            headers,
            body_template: bodyTemplate,
        },
    };

    fetch('/api/llm/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
    }).then(r => r.json()).then(d => {
        if (d.code === 0) {
            // 保存人设（含双轨 prompt）
            const persona = {
                system_prompt: document.getElementById('personaPrompt').value,
                meeting_system_prompt: document.getElementById('personaMeetingPrompt').value,
                reply_style: document.getElementById('personaStyle').value,
                max_tokens: parseInt(document.getElementById('personaMaxTokens').value) || 200,
                context_rounds: parseInt(document.getElementById('personaContextRounds').value) || 20,
            };
            return fetch('/api/persona', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(persona),
            });
        } else {
            throw new Error(d.msg || '保存配置失败');
        }
    }).then(r => r.json()).then(d => {
        if (d.code === 0) {
            ConsoleToast.success('配置已保存');
        } else {
            ConsoleToast.error(d.msg || '保存人设失败');
        }
    }).catch(e => ConsoleToast.error('保存失败: ' + e.message));
}

// ─── 记忆管理（原内联迁移） ───────────────────────────────

function loadMemory() {
    fetch('/api/memory').then(r => r.json()).then(d => {
        const list = document.getElementById('memoryList');
        if (d.code === 0 && d.data && d.data.entries && d.data.entries.length > 0) {
            list.innerHTML = d.data.entries.map(e =>
                `<div class="border-bottom py-2 small">
                    <span class="badge bg-secondary me-2">${e.role || 'user'}</span>
                    <span>${e.text || ''}</span>
                </div>`
            ).join('');
        } else {
            list.innerHTML = '<div class="text-muted small">暂无记忆</div>';
        }
    }).catch(e => console.log('load memory error:', e));
}

function clearMemory() {
    if (!confirm('确定清空所有记忆吗？')) return;
    fetch('/api/memory', { method: 'DELETE' }).then(r => r.json()).then(d => {
        if (d.code === 0) {
            ConsoleToast.success('记忆已清空');
            loadMemory();
        }
    }).catch(e => ConsoleToast.error('清空失败: ' + e.message));
}
