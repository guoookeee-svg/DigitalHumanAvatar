<p align="center">
    <img src="./assets/LiveTalking-logo.png" align="middle" width="600"/>
</p>

中文版 ｜ [English](./README-EN.md)

<p align="center">
    <a href="./LICENSE"><img src="https://img.shields.io/badge/license-Apache%202-dfd.svg"></a>
    <a href=""><img src="https://img.shields.io/badge/python-3.10+-aff.svg"></a>
    <a href=""><img src="https://img.shields.io/badge/os-linux%2C%20win%2C%20mac-pink.svg"></a>
</p>


实时交互流式数字人引擎，实现音视频同步对话，已在业内获得广泛商用。

**本仓库在 [lipku/LiveTalking](https://github.com/lipku/LiveTalking) 基础上做了深度改造**，新增了**三模式交互**、**实时声纹说话人分离**、**本地 SenseVoice ASR**、**TTS 句间流水线预取**、**会议响应门控**等能力，面向**多人会议替会 / 记录**场景做了专门优化。

---

## ✨ 核心特性

### 1. 三模式交互（对话 / 替会 / 记录）

系统把交互拆成三种模式，通过侧边栏或 `/api/mode` 切换：

| 模式 | 说明 |
|------|------|
| **对话（solo）** | 单人自由对话，任何人说话数字人都能响应，无身份限制 |
| **替会（meeting）** | 数字人作为用户分身参会；**被点名 / 被直接提问才回答**，其余发言只记录不插嘴 |
| **记录（recording）** | 纯会议转写 + 说话人标注，数字人不出声，只负责记录 |

### 2. 实时声纹说话人分离

- 基于 **CAM++（192 维声纹向量）** 实时识别每段语音是谁说的
- 会话内**临时中心**自动把同一个人归为「说话人N」，跨会话可命名入库（点击消息旁标签即可重命名）
- 全局声纹库（`data/speakers.json`）持久化，命名后下次同一个人说话能直接认出
- 数字人自身声音做**回声滤除**（`is_avatar_voice`），避免数字人自己打断自己

### 3. 本地 SenseVoice ASR

- 内置 **FunASR / SenseVoice** 本地识别，无需外网
- **Silero VAD** 自动判停 + **SmartTurn** 语义复核，避免把人一句话切成两段
- VAD 检测到用户开口即触发**打断**（数字人立即停止说话）
- 支持 2-pass 流式 / 离线识别，说话人判定与 ASR 并行执行互不阻塞

### 4. TTS 句间流水线预取

- 修复了「说话断断续续」问题：在播上一句的推帧间隙**后台预取下一句的合成请求**
- 消除句与句之间的静音间隙，保持连贯
- 播放前检查用户是否正在说话（`is_user_speaking`），避免人机同时说话

### 5. 会议响应门控

在 `/human` 路由调 LLM 前插入 `decide_response()` 门控：
- **点名检测**：消息包含 `address_keywords`（如「数字人」「王总」）→ 必答
- **直接提问句式**：正则匹配「你怎么看」「总结一下」等 → 必答
- **语义插话层**：预留接口（`proactive_level`），默认关闭
- 不通过的发言仍写入会话记录（带说话人标签），供回看与离线精修

### 6. 离线说话人聚类精修

会议结束后可对整段音频做**全局聚类修正**（`speaker_diarize.py`），用 CAM++ embedding + scipy 层次聚类，把实时识别中误合并/误拆分的说话人重新校正，可对临时说话人命名并注册进全局声纹库。

---

## 🖥 使用场景

| 场景 | 说明 |
|------|------|
| **虚拟主播 / 直播带货** | 24 小时无人直播，LLM 自动生成话术 |
| **AI 数字人客服** | 接入知识库，语音提问实时回答，支持打断 |
| **在线教育 / 培训** | 教师数字分身录制课程或实时授课 |
| **智能语音助手** | 结合 APP 调用 `/human` 接口驱动对话 |
| **会议替会 / 记录** | 数字人代表用户参会，被点名才发言；或纯转写记录多人会议 |

**核心流程**：用户语音/文字 → 本地 ASR 识别 → 声纹判定说话人 → 交互门控 → LLM 生成回复 → TTS 合成语音 → 数字人实时口型同步 → 音视频推流输出

---

## 📁 目录结构

```
.
├── app.py                  # 服务主入口（aiohttp）
├── config.yaml             # 服务配置（fps/transport 等）
├── config.py               # CLI 参数解析 + YAML 配置加载
├── registry.py             # 插件注册表（tts/avatar/output 等）
├── start_new.sh            # 一键启动 / 重启脚本
├── persona.json.example   # 人设配置模板（实际使用复制为 persona.json）
├── llm_config.json.example # LLM / 交互模式 / 替会配置模板（实际使用复制为 llm_config.json）
├── agent/                  # LLM 交互子系统（数字人"对话大脑"）
│   ├── llm_router.py       # LLM 模式分发（openai / agent）+ 交互模式 API
│   ├── llm_openai.py       # OpenAI 兼容 LLM 实现（vLLM）
│   ├── llm.py              # Agent 外部服务模式实现
│   ├── meeting_gate.py     # 三模式响应门控（decide_response）
│   ├── speaker_store.py    # 实时声纹识别 + 声纹库
│   ├── speaker_diarize.py  # 离线说话人聚类精修
│   ├── conversation_store.py # 会话 / 消息存储 + 活动会话指针
│   └── memory_store.py     # RAG 记忆检索 / 存储
├── server/
│   ├── asr_server.py       # 本地 ASR WebSocket（VAD + 判停 + 声纹）
│   ├── session_manager.py  # 会话管理
│   ├── routes.py           # HTTP/WS 路由
│   └── ...
├── tts/
│   ├── base_tts.py         # TTS 基类 + 句间流水线预取
│   ├── sovits.py           # GPT-SoVITS 实现（含 play_stream）
│   └── ...                 # edge/azure/cosyvoice/xtts 等
├── avatars/
│   ├── base_avatar.py      # 数字人基类（flush_talk / is_speaking）
│   ├── wav2lip_avatar.py   # Wav2Lip 嘴型驱动
│   └── ...
└── web/
    ├── console.html        # 三模式控制台（对话/替会/记录 + PIP 悬浮窗 + ASR 采集坞）
    ├── asr/                # ASR 前端采集页
    └── js/                 # app/chat/meeting/record/settings/speaker/webrtc
```

---

## 🚀 快速开始

### 环境要求

- **Python 3.10+**
- **Linux / Windows / macOS**
- 一张 **NVIDIA GPU**（嘴型推理 + 本地 ASR），建议显存 ≥ 8GB
- 本地 **vLLM** 端点（提供 `/v1/chat/completions`）或外部 Agent 服务

### 可选的 GPT-SoVITS TTS

本仓库默认 TTS 用 **GPT-SoVITS**（见 `config.yaml` 的 `tts: gpt-sovits`）。它是**独立的外部服务**，不包含在本仓库内，需要单独部署，并放在本仓库的**上一级目录**（`../GPT-SoVITS`），这样 `start_new.sh` 才能找到它。

- 部署 GPT-SoVITS 后，`start_new.sh` 会自动启动它（端口 9880）。
- 若未部署 GPT-SoVITS，脚本会提示并跳过 TTS，主服务仍能启动，但**数字人没有声音**（可改用 `edge_tts` 等，修改 `config.yaml` 的 `tts` 项即可）。
- 需提供音色参考音频（`config.yaml` 的 `REF_FILE`，16kHz 单声道 wav）。

### 安装

```bash
# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt

# 配置环境变量（复制模板并填入你的配置）
cp .env.example .env
# 编辑 .env，填入 LLM 端点 / API Key 等
```

### 模型文件

需要以下模型（不在仓库内，需自行下载放置）：

- `models/wav2lip.pth` — Wav2Lip 嘴型模型权重（必装）
- 嘴型/形象素材，放在 `data/avatars/` 下
- FunASR 模型（SenseVoice / CAM++ / fsmn-vad / paraformer），由 `MODELSCOPE_CACHE` 指定缓存目录，首次运行自动下载
- TTS 参考音频：`config.yaml` 中 `REF_FILE`（如 `ref_audio.wav`）指向数字人的音色参考音频（16kHz 单声道 wav），**需自行准备并放到对应路径**，用于 GPT-SoVITS 音色克隆

### 启动

```bash
./start_new.sh          # 启动（自动加载 .env、停旧进程、健康检查）
./start_new.sh stop     # 停止
./start_new.sh status   # 查看状态
./start_new.sh log      # 实时看日志
```

或手动：

```bash
source .venv/bin/activate
python app.py
```

启动后访问：`http://<服务器IP>:8010/console.html`

---

## 🧭 从零部署完整步骤

以下是从一台干净机器到可对话数字人的完整流程（GPU 服务器）：

### 1. 拉取代码 / 准备目录

```bash
# 克隆本仓库
git clone <你的仓库地址> LiveTalking
cd LiveTalking

# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

> **GPU 说明**：若 `torch` 安装的版本不带 CUDA 或装错版本，请按你的 CUDA 版本重新安装，例如：
> ```bash
> pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
> ```
> （`nvidia-smi` 查看 CUDA 版本，选择对应的 cu 版本）

### 3. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入：
#   LLM_BASE_URL   → 你的 vLLM 服务地址（提供 /v1/chat/completions）
#   LLM_API_KEY    → 对应 API Key
#   LLM_MODEL      → 模型名（如 ds-v4-flash）
#   CUDA_VISIBLE_DEVICES → 用哪张 GPU
```

> **务必**从模板复制 `llm_config.json` 和 `persona.json`（直接使用模板即可，首次运行可再调整）：
>
> ```bash
> cp llm_config.json.example llm_config.json
> cp persona.json.example persona.json
> ```
>
> 否则 LLM 模式 / 人设 / 会议替会配置会缺失，数字人可能无法正常对话。
> 另外可在 `llm_config.json` 里配 `interaction_mode`（solo/meeting/recording）与 `speaker`/`memory` 段参数。

### 4. 放置模型与素材

- 把 `wav2lip.pth` 放到 `models/wav2lip.pth`
- 把形象素材（如 `wav2lip256_avatar1/`）放到 `data/avatars/`
- FunASR 模型不用手动放，首次运行会自动下载到 `MODELSCOPE_CACHE`
- TTS 参考音频 `ref_audio.wav`（16kHz 单声道）放到 `REF_FILE` 指定路径

### 5. 准备外部服务（可选）

- **GPT-SoVITS TTS**：单独部署，放在本仓库上一级目录 `../GPT-SoVITS`（`start_new.sh` 会自动拉起，端口 9880）。
- **vLLM LLM 服务**：需已可用，地址填进 `.env` 的 `LLM_BASE_URL`。

### 6. 启动

```bash
./start_new.sh          # 一键启动（自动加载 .env、拉起 GPT-SoVITS、健康检查）
```

等待 GPT-SoVITS 加载完成（约 20~60s），然后浏览器打开：

```
http://<服务器IP>:8010/console.html
```

> **注意**：若不是用显卡跑推理（如用 CPU 或云 CPU 机器），ASR/声纹/嘴型推理会非常慢或无法运行，建议使用 NVIDIA GPU。

### 部署检查清单

- [ ] `.venv` 已创建并 `pip install -r requirements.txt`
- [ ] `.env` 已配置 `LLM_BASE_URL` / `LLM_API_KEY` / `CUDA_VISIBLE_DEVICES`
- [ ] `models/wav2lip.pth` 已放置
- [ ] 形象素材在 `data/avatars/`
- [ ] TTS 参考音频 `ref_audio.wav` 已放置（使用 GPT-SoVITS 时）
- [ ] GPT-SoVITS 已部署到 `../GPT-SoVITS`（使用 GPT-SoVITS 时）
- [ ] `nvidia-smi` 能看到 GPU，且 CUDA 可用（`python -c "import torch;print(torch.cuda.is_available())"` 为 True）

---

## ⚙️ 配置说明

### `.env`（环境变量）

| 变量 | 说明 |
|------|------|
| `LLM_API_KEY` | 本地 vLLM 的 API Key |
| `LLM_BASE_URL` | 本地 vLLM 端点，如 `http://<你的vLLM主机>:8080/v1` |
| `LLM_MODEL` | 模型名，如 `ds-v4-flash` |
| `QWENPAW_*` | Agent 外部服务配置（agent 模式可选）|
| `CUDA_VISIBLE_DEVICES` | 指定使用的 GPU（避免占用被 vLLM 占用的卡）|
| `MODELSCOPE_CACHE` | FunASR 模型缓存目录 |

### `llm_config.json`（LLM 与交互模式）

- `mode`：`openai`（本地 vLLM）/ `agent`（外部 Agent 服务）
- `interaction_mode`：`solo` / `meeting` / `recording`
- `meeting`：替会配置（`owner_name` / `owner_speaker` / `address_keywords` / `question_patterns` / `proactive_level`）
- `openai` / `agent`：对应模式的端点与模型配置

### `persona.json`（人设）

- `system_prompt`：默认人设
- `meeting_system_prompt`：替会模式专属人设（数字分身、简短口语化、以主人立场发言）
- `reply_style` / `context_rounds` / `max_tokens`

---

## 🔌 主要 API

| 接口 | 方法 | 说明 |
|------|------|------|
| `/offer` | POST | WebRTC 建立连接 |
| `/human` | POST | 文本/语音入口，触发 LLM + TTS（受交互门控约束）|
| `/api/asr` | WS | 本地 ASR 识别 WebSocket |
| `/api/mode` | GET/POST | 切换交互模式（solo/meeting/recording）|
| `/api/llm/mode` | GET/POST | 切换 LLM 模式（openai/agent）|
| `/api/meeting/config` | GET/POST | 读写替会配置 |
| `/api/speakers` | GET/POST | 声纹库列表 / 注册 |
| `/api/conversations/{id}/speakers/rename` | POST | 给会话临时说话人命名并注册声纹 |
| `/api/conversations/{id}/diarize` | GET/POST | 触发/查询离线说话人聚类精修 |

---

## 🧪 常见问题

**Q: 数字人说话「断断续续」？**
A: 已修复。TTS 采用句间流水线预取，在推帧间隙提前合成下一句，消除静音间隙。

**Q: 说话不能打断 / 数字人自打断？**
A: 打断由 ASR 的 VAD 检测用户开口触发（`_maybe_interrupt`），数字人自身声音通过声纹回声滤除。若仍异常，检查 GAIN（`web/asr/main.js`）与 `self_filter_threshold`（`speaker_store.py`）。

**Q: 说话人总是被识别成同一个人？**
A: 检查 `llm_config.json` / `speaker_store.py` 的 `session_threshold`（默认 0.65）是否合理；用 `/api/conversations/{id}/speakers/rename` 命名并注册临时说话人。

**Q: 会议模式下数字人不回答？**
A: 替会模式只在被点名/被提问时响应。若希望所有人都能触发，切换 `interaction_mode` 为 `solo`，或在 `meeting.address_keywords` / `question_patterns` 里补充关键词。

---

## 📄 License

本项目基于 [Apache License 2.0](./LICENSE)，参考 [lipku/LiveTalking](https://github.com/lipku/LiveTalking) 改造。
