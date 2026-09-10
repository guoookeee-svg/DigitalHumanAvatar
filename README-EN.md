<p align="center">
    <img src="./assets/LiveTalking-logo.png" align="middle" width="600"/>
</p>

[中文版](./README.md) | English

<p align="center">
    <a href="./LICENSE"><img src="https://img.shields.io/badge/license-Apache%202-dfd.svg"></a>
    <a href=""><img src="https://img.shields.io/badge/python-3.10+-aff.svg"></a>
    <a href=""><img src="https://img.shields.io/badge/os-linux%2C%20win%2C%20mac-pink.svg"></a>
</p>

A real-time interactive streaming digital human engine with synchronized audio-video conversation.

**This repository is a heavily enhanced fork of [lipku/LiveTalking](https://github.com/lipku/LiveTalking)**. It adds **three interaction modes**, **real-time voiceprint speaker diarization**, **local SenseVoice ASR**, **TTS sentence-pipeline prefetch**, and **meeting response gating**, specifically optimized for **multi-person meeting attendance / recording** scenarios.

---

## ✨ Core Features

### 1. Three Interaction Modes (Chat / Meeting / Recording)

The system splits interaction into three modes, switchable via the sidebar or `/api/mode`:

| Mode | Description |
|------|-------------|
| **Chat (solo)** | Free-form one-on-one conversation; the digital human responds to anyone, no identity restriction |
| **Meeting (meeting)** | The digital human attends as the user's delegate; **responds only when addressed or asked directly**, otherwise only records |
| **Recording (recording)** | Pure meeting transcription + speaker labeling; the digital human stays silent, only records |

### 2. Real-time Voiceprint Speaker Diarization

- Uses **CAM++ (192-dim voiceprint vectors)** to identify who said each segment in real time
- In-session **temporary centers** automatically group the same person as "Speaker N", cross-session naming can be stored persistently (click the label next to a message to rename)
- Global voiceprint library (`data/speakers.json`) persists; once named, the same person is recognized on next appearance
- The digital human's own voice is **echo-filtered** (`is_avatar_voice`) to prevent self-interruption

> ⚠️ **Known limitations**: Real-time speaker identification is based on the CAM++ voiceprint model and **has limited accuracy**. It is prone to misjudgment in these cases:
> - **Short utterances / frequent interruptions**: the same person's short phrases may be split into multiple "Speaker N" labels (unstable vectors)
> - **Single-person conversation**: speaker differentiation is unnecessary and misjudgment pollutes the labels
> - **Ambient noise / digital-human echo**: the digital human's own voice may be mislabeled as a speaker
>
> Suggestions:
> - Use **solo mode** for **single-person conversations** (no speaker differentiation needed) to avoid mislabeling
> - For **multi-person meetings**, use longer, continuous speech; if recognition is off, **rename/register** temporary speakers, or run **offline clustering refinement** (`speaker_diarize.py`) on the full audio
> - To check echo handling, inspect `GAIN` in `web/asr/main.js` and `self_filter_threshold` in `speaker_store.py`

### 3. Local SenseVoice ASR

- Built-in **FunASR / SenseVoice** local recognition, no internet required
- **Silero VAD** auto end-of-turn detection + **SmartTurn** semantic verification, avoids splitting one sentence into two
- VAD detecting the user speaking triggers an **interrupt** (the digital human stops immediately)
- Supports 2-pass streaming / offline recognition; speaker judgment runs in parallel with ASR without blocking

### 4. TTS Sentence-Pipeline Prefetch

- Fixes "choppy speech": prefetches the next sentence's synthesis request in the gaps between pushing frames
- Eliminates silent gaps between sentences for smooth playback
- Checks if the user is speaking (`is_user_speaking`) before playback to avoid talking over the user

### 5. Meeting Response Gating

Inserts a `decide_response()` gate before the `/human` route calls the LLM:
- **Address detection**: messages containing `address_keywords` (e.g. "数字人", "王总") → must respond
- **Direct question patterns**: regex matches like "你怎么看", "总结一下" → must respond
- **Semantic interjection layer**: reserved interface (`proactive_level`), off by default
- Non-passing utterances are still written to the conversation log (with speaker labels) for review and offline refinement

### 6. Offline Speaker Clustering Refinement

After a meeting, run global clustering (`speaker_diarize.py`) on the entire audio using CAM++ embeddings + scipy hierarchical clustering to correct mis-merged/mis-split speakers from real-time recognition, and register temporary speakers into the global voiceprint library.

---

## 🖥 Usage Scenarios

| Scenario | Description |
|----------|-------------|
| **Virtual Streamer / Live Commerce** | 24/7 unmanned live streaming with LLM-generated scripts |
| **AI Digital Human Customer Service** | Knowledge base + real-time voice Q&A with interruption support |
| **Online Education / Training** | Teacher digital avatar for course recording or real-time lectures |
| **Intelligent Voice Assistant** | Call the `/human` API to drive digital human voice interactions |
| **Meeting Attendance / Recording** | Digital human represents the user in meetings, speaking only when addressed; or pure transcription/recording |

**Core Flow**: User voice/text → local ASR → voiceprint speaker judgment → interaction gating → LLM reply → TTS synthesis → real-time lip-sync → audio/video streaming output

---

## 📁 Project Structure

```
.
├── app.py                  # Service entry (aiohttp)
├── config.yaml             # Service config (fps/transport etc.)
├── config.py               # CLI arg + YAML config loading
├── registry.py             # Plugin registry (tts/avatar/output etc.)
├── start_new.sh            # One-click start / restart script
├── persona.json.example   # Persona config template (copy to persona.json)
├── llm_config.json.example # LLM / interaction mode / meeting config template (copy to llm_config.json)
├── agent/                  # LLM interaction subsystem ("dialogue brain")
│   ├── llm_router.py       # LLM mode dispatch (openai/agent) + interaction mode API
│   ├── llm_openai.py       # OpenAI-compatible LLM implementation (vLLM)
│   ├── llm.py              # Agent external service mode implementation
│   ├── meeting_gate.py     # Three-mode response gating (decide_response)
│   ├── speaker_store.py    # Real-time voiceprint recognition + voiceprint library
│   ├── speaker_diarize.py  # Offline speaker clustering refinement
│   ├── conversation_store.py # Conversation / message storage + active conversation pointer
│   └── memory_store.py     # RAG memory retrieval / storage
├── server/
│   ├── asr_server.py       # Local ASR WebSocket (VAD + end-of-turn + voiceprint)
│   ├── session_manager.py  # Session management
│   ├── routes.py           # HTTP/WS routes
│   └── ...
├── tts/
│   ├── base_tts.py         # TTS base + sentence-pipeline prefetch
│   ├── sovits.py           # GPT-SoVITS implementation (incl. play_stream)
│   └── ...                 # edge/azure/cosyvoice/xtts etc.
├── avatars/
│   ├── base_avatar.py      # Digital human base (flush_talk / is_speaking)
│   ├── wav2lip_avatar.py   # Wav2Lip lip-sync
│   └── ...
└── web/
    ├── console.html        # Three-mode console (chat/meeting/recording + PIP + ASR capture dock)
    ├── asr/                # ASR frontend capture page
    └── js/                 # app/chat/meeting/record/settings/speaker/webrtc
```

---

## 🚀 Quick Start

### Requirements

- **Python 3.10+**
- **Linux / Windows / macOS**
- An **NVIDIA GPU** (lip-sync inference + local ASR), ≥ 8GB VRAM recommended
- A local **vLLM** endpoint (serving `/v1/chat/completions`) or an external Agent service

### Optional GPT-SoVITS TTS

This repository uses **GPT-SoVITS** as the default TTS (see `tts: gpt-sovits` in `config.yaml`). It is a **separate external service** not bundled here; deploy it and place it in the **parent directory** (`../GPT-SoVITS`) so `start_new.sh` can find it.

- Once deployed, `start_new.sh` will start it automatically (port 9880).
- If GPT-SoVITS is not deployed, the script warns and skips TTS; the main service still starts, but **the digital human has no audio** (you can switch to `edge_tts` etc. by editing `config.yaml`'s `tts` entry).
- A voice reference audio is required (`REF_FILE` in `config.yaml`, 16kHz mono wav).

### Install

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment (copy template and fill in your config)
cp .env.example .env
# Edit .env, fill in LLM endpoint / API Key etc.
```

### Model Files

**Download links** (from the original lipku/LiveTalking project):

| Cloud drive | Link |
|-------------|------|
| Quark Cloud | <https://pan.quark.cn/s/83a750323ef0> |
| Google Drive | <https://drive.google.com/drive/folders/1FOC_MD6wdogyyX_7V1d4NDIO7P9NlSAJ?usp=sharing> |

The following models are NOT in this repo; download and place them yourself:

- `wav2lip256.pth` → copy to the project `models/` directory and **rename to `wav2lip.pth`** (required)
- `wav2lip256_avatar1.tar.gz` → extract and copy the whole folder to `data/avatars/`
- Avatar assets, place under `data/avatars/<avatar_id>/`; **three parts are required** (missing any of them will cause startup failure):
  - `full_imgs/` — raw face video frame images (named by sequence, e.g. `0.jpg`, `1.jpg`…)
  - `face_imgs/` — cropped face images (one-to-one with `full_imgs`)
  - `coords.pkl` — face coordinate file
  - Where `avatar_id` matches `avatar_id` in `config.yaml` (e.g. `wav2lip256_avatar1`)
- FunASR models (SenseVoice / CAM++ / fsmn-vad / paraformer), cached under `MODELSCOPE_CACHE`, downloaded automatically on first run
- TTS reference audio: `REF_FILE` in `config.yaml` (e.g. `ref_audio.wav`) is the digital human's voice reference (16kHz mono wav), **must be provided and placed at the corresponding path** for GPT-SoVITS voice cloning

### Start

```bash
./start_new.sh          # Start (auto-loads .env, stops old processes, health check)
./start_new.sh stop     # Stop
./start_new.sh status   # View status
./start_new.sh log      # Tail logs live
```

Or manually:

```bash
source .venv/bin/activate
python app.py
```

Then access: `http://<serverip>:8010/console.html`

---

## ⚙️ Configuration

### `.env` (environment variables)

| Variable | Description |
|----------|-------------|
| `LLM_API_KEY` | API key of the local vLLM |
| `LLM_BASE_URL` | Local vLLM endpoint, e.g. `http://<your-vllm-host>:8080/v1` |
| `LLM_MODEL` | Model name, e.g. `ds-v4-flash` |
| `QWENPAW_*` | External Agent service config (optional, agent mode) |
| `CUDA_VISIBLE_DEVICES` | GPU to use (avoid the cards occupied by vLLM) |
| `MODELSCOPE_CACHE` | FunASR model cache directory |

### `llm_config.json` (LLM & interaction mode)

- `mode`: `openai` (local vLLM) / `agent` (external Agent service)
- `interaction_mode`: `solo` / `meeting` / `recording`
- `meeting`: meeting config (`owner_name` / `owner_speaker` / `address_keywords` / `question_patterns` / `proactive_level`)
- `openai` / `agent`: endpoint & model config for the corresponding mode

### `persona.json` (persona)

- `system_prompt`: default persona
- `meeting_system_prompt`: meeting-specific persona (digital delegate, concise & colloquial, speaks as the owner)
- `reply_style` / `context_rounds` / `max_tokens`

---

## 🔌 Main APIs

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/offer` | POST | Establish WebRTC connection |
| `/human` | POST | Text/voice entry; triggers LLM + TTS (subject to interaction gating) |
| `/api/asr` | WS | Local ASR recognition WebSocket |
| `/api/mode` | GET/POST | Switch interaction mode (solo/meeting/recording) |
| `/api/llm/mode` | GET/POST | Switch LLM mode (openai/agent) |
| `/api/meeting/config` | GET/POST | Read/write meeting config |
| `/api/speakers` | GET/POST | Voiceprint library list / register |
| `/api/conversations/{id}/speakers/rename` | POST | Rename and register a temporary conversation speaker |
| `/api/conversations/{id}/diarize` | GET/POST | Trigger/query offline speaker clustering refinement |

---

## 🧪 FAQ

**Q: The digital human's speech is choppy?**
A: Fixed. TTS uses sentence-pipeline prefetch to synthesize the next sentence during playback gaps, eliminating silent gaps.

**Q: Cannot interrupt / the digital human interrupts itself?**
A: Interruption is triggered by ASR VAD detecting the user speaking (`_maybe_interrupt`); the digital human's own voice is filtered via voiceprint echo suppression. If still abnormal, check `GAIN` (`web/asr/main.js`) and `self_filter_threshold` (`speaker_store.py`).

**Q: Different speakers always labeled as the same person?**
A: Check `session_threshold` (default 0.65) in `llm_config.json` / `speaker_store.py`; use `/api/conversations/{id}/speakers/rename` to name and register temporary speakers.

**Q: In meeting mode the digital human does not answer?**
A: In meeting mode it only responds when addressed/asked. If you want anyone to trigger it, switch `interaction_mode` to `solo`, or add keywords to `meeting.address_keywords` / `question_patterns`.

---

## 📄 License

Licensed under [Apache License 2.0](./LICENSE), forked from [lipku/LiveTalking](https://github.com/lipku/LiveTalking).
