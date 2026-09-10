/* ============================================================
   LiveTalking 数字人控制台 - 共享 JS 工具
   ============================================================ */

// ─── Toast 通知 ────────────────────────────────────────────
const ConsoleToast = {
    container: null,

    _ensureContainer() {
        if (!this.container) {
            this.container = document.createElement('div');
            this.container.className = 'toast-container';
            document.body.appendChild(this.container);
        }
        return this.container;
    },

    _show(type, message, duration = 3000) {
        const container = this._ensureContainer();
        const toast = document.createElement('div');
        toast.className = `console-toast ${type}`;

        const icons = {
            success: 'bi-check-circle-fill',
            error: 'bi-x-circle-fill',
            info: 'bi-info-circle-fill'
        };

        toast.innerHTML = `
            <i class="bi ${icons[type] || icons.info}"></i>
            <span class="toast-msg"></span>
            <button class="toast-close bi bi-x-lg"></button>
        `;
        toast.querySelector('.toast-msg').textContent = message;
        toast.querySelector('.toast-close').addEventListener('click', () => this._remove(toast));

        container.appendChild(toast);

        if (duration > 0) {
            setTimeout(() => this._remove(toast), duration);
        }
    },

    _remove(toast) {
        toast.style.animation = 'slideOut 0.3s ease forwards';
        setTimeout(() => toast.remove(), 300);
    },

    success(msg, duration) { this._show('success', msg, duration); },
    error(msg, duration) { this._show('error', msg, duration); },
    info(msg, duration) { this._show('info', msg, duration); }
};

// ─── 加载指示 ──────────────────────────────────────────────
const ConsoleLoading = {
    overlay: null,
    count: 0,

    show(text = '处理中...') {
        this.count++;
        if (!this.overlay) {
            this.overlay = document.createElement('div');
            this.overlay.className = 'spinner-overlay';
            this.overlay.innerHTML = `
                <div class="spinner-box">
                    <div class="spinner"></div>
                    <div class="spinner-text"></div>
                </div>
            `;
            document.body.appendChild(this.overlay);
        }
        this.overlay.querySelector('.spinner-text').textContent = text;
        this.overlay.style.display = 'flex';
    },

    hide() {
        this.count = Math.max(0, this.count - 1);
        if (this.count === 0 && this.overlay) {
            this.overlay.style.display = 'none';
        }
    }
};

// ─── 状态徽章 ──────────────────────────────────────────────
const ConsoleStatus = {
    update(badgeId, connected) {
        const badge = document.getElementById(badgeId);
        if (!badge) return;
        if (connected) {
            badge.className = 'session-badge connected';
            badge.innerHTML = '<span class="status-dot on"></span>已连接';
        } else {
            badge.className = 'session-badge disconnected';
            badge.innerHTML = '<span class="status-dot off"></span>未连接';
        }
    }
};

// ─── 通用工具 ──────────────────────────────────────────────
const ConsoleUtil = {
    // fetch 封装，自动处理 JSON 和错误
    async post(url, body) {
        const res = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        const data = await res.json();
        if (data.code && data.code !== 0) {
            throw new Error(data.msg || '请求失败');
        }
        return data;
    },

    async get(url) {
        const res = await fetch(url);
        const data = await res.json();
        if (data.code && data.code !== 0) {
            throw new Error(data.msg || '请求失败');
        }
        return data;
    },

    // 防抖
    debounce(fn, delay = 300) {
        let timer;
        return function (...args) {
            clearTimeout(timer);
            timer = setTimeout(() => fn.apply(this, args), delay);
        };
    },

    // 格式化时间
    formatTime(ts) {
        const d = new Date(ts);
        return d.toLocaleTimeString('zh-CN', { hour12: false });
    }
};
