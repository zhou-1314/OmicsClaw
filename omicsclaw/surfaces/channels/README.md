# OmicsClaw Channel Surface

The Channel Surface owns OmicsClaw's messaging adapters. Its production scope
is the shared runner and `ControlRuntime`: Owner-only Telegram text plus one
ordinary photo, and Owner-only Feishu text-only. Start either authoritative
Adapter with the runner:

```bash
python -m omicsclaw.surfaces.channels --channels telegram
python -m omicsclaw.surfaces.channels --channels feishu
```

Telegram photos enter the immutable Attachment Store only after duplicate and
admission checks. Both Channels send terminal text only through the persistent
Delivery Outbox. `FEISHU_ALLOWED_SENDERS` and `FEISHU_BOT_OPEN_ID` are
mandatory; the Bot open ID proves group mention identity. The other Channel
Adapters remain gated and their sections below are migration reference only.
Outbound media remains incomplete and fail-closed; this is not full ADR or
media completion.

## Interactive Terminal Chat

Terminal chat is provided by the main OmicsClaw CLI rather than the `bot` package:

```bash
# Start the interactive conversational UI
oc interactive
# or
omicsclaw interactive
```

## Supported Channels
- [Architecture](#architecture)
- [Capability Matrix](#capability-matrix)
- [Installation](#installation)
- [Configuration](#configuration)
- [Channel Deployment Guides](#channel-deployment-guides)
- [Usage](#usage)
- [Security and Access Control](#security-and-access-control)
- [Troubleshooting](#troubleshooting)

## Architecture

```text
Telegram update / Feishu text event
  -> authenticity + configured-Owner gate
  -> RawInboundV1 (stable provider message id; optional Telegram photo descriptor)
  -> duplicate/capacity checks
  -> Telegram only: process-local photo source -> attachments.db + Blob
  -> ControlRuntime / durable Turn / per-Conversation FIFO
  -> canonical Transcript keeps structured Attachment References only
  -> bounded ephemeral image rendering immediately before each model call
  -> Agent runtime + shared Skill tools
  -> canonical terminal Transcript
  -> atomic Outbound Delivery plan in control.db
  -> account-scoped Delivery Pump
  -> one Telegram or Feishu provider attempt

DingTalk / Discord / Slack / WeChat / QQ / Email / iMessage
  -> startup rejected until the same ingress + Delivery cutover exists
```

### Core Modules

| Module | Purpose |
|--------|--------|
| `telegram.py` / `feishu.py` | Authenticated Owner ingress, RawInbound construction, lifecycle |
| `telegram_delivery.py` / `feishu_delivery.py` | Single-attempt text Delivery Adapters |
| `omicsclaw/attachments/` | Immutable Records, content-addressed Blobs, reconciliation and bounded model rendering |
| `manager.py` | Fail-closed lifecycle + `/healthz` |
| `base.py` | Shared Adapter interface; legacy direct startup gate |
| `omicsclaw/control/runtime.py` | Authoritative Turn + Transcript composition |
| `omicsclaw/control/delivery.py` | Persistent Outbox Pump and retry/unknown policy |
| `__main__.py` | Production runner — `python -m omicsclaw.surfaces.channels` |

## Registered Adapter capability declarations

Only the first two rows are enabled production behavior. Other declarations
remain source material and do not grant startup authority.

| Channel | Inbound | Outbound | Production status |
|:--------|:--------|:---------|:------------------|
| Telegram | Owner text; one ordinary photo with optional caption | Persistent Outbox text | Enabled |
| Feishu | Owner text; groups require a proved Bot mention | Persistent Outbox text | Enabled, text-only |
| DingTalk, Discord, Slack, WeChat, QQ, Email, iMessage | None | None | Gated at startup |

Outbound media is not enabled for any Channel.

### Connection Types

| Channel | Transport | Connection Mode | Default Port |
|---------|-----------|-----------------|:------------:|
| Telegram | HTTPS | Long polling (`getUpdates`) | — |
| Feishu | WebSocket | lark-oapi WebSocket client | — |
| DingTalk | WebSocket | Stream Mode (DingTalk gateway) | — |
| Discord | WebSocket | Gateway events (`discord.py`) | — |
| Slack | WebSocket | Socket Mode (`slack-sdk`) | — |
| WeChat | HTTP | Webhook (`POST /wechat/callback`) | 9001 |
| QQ | WebSocket | Bot Gateway (`qq-botpy`) | — |
| Email | TCP | IMAP polling + SMTP send | 993/587 |
| iMessage | stdio | JSON-RPC (`imsg` CLI) | — |

> **"—"** means no listening port required — no public IP or port forwarding needed.

---

## Installation

### Step 1: Core Dependencies

```bash
# Install OmicsClaw plus the declared Channel extras
pip install -e ".[channels]"
```

OmicsClaw bot entrypoints automatically read the project-root `.env`. If `python-dotenv` is unavailable, OmicsClaw falls back to an internal `.env` parser, so normal `KEY=value` configuration still works.

### Step 2: Channel Dependencies

Choose **one** of the following approaches. The declared `channels` extra
installs both production SDKs; packages below it are listed only for developers
working on gated Adapter migrations.

#### Option A: Install declared Channel extras

```bash
pip install -e ".[channels]"
```

#### Option B: Install only the channels you need

```bash
# Telegram
pip install "python-telegram-bot>=21.0"

# Feishu (Lark)
pip install "lark-oapi>=1.3.0"

# DingTalk
pip install "websockets>=12.0"
# httpx is already in core requirements

# Discord
pip install "discord.py>=2.3.0"

# Slack
pip install "slack-sdk>=3.27.0" "aiohttp>=3.9.0"

# WeChat
# httpx + aiohttp already covered above — no additional install needed

# QQ
pip install qq-botpy

# Email
# No extra dependencies — uses Python standard library (imaplib, smtplib)

# iMessage (macOS only)
# No Python dependencies — requires imsg CLI:
brew install anthropics/tap/imsg
```

### Dependency Summary

| Channel | Python Package | External Tool | Notes |
|---------|---------------|---------------|-------|
| Telegram | `python-telegram-bot>=21.0` | — | |
| Feishu | `lark-oapi>=1.3.0` | — | |
| DingTalk | `websockets>=12.0` | — | `httpx` already in core |
| Discord | `discord.py>=2.3.0` | — | |
| Slack | `slack-sdk>=3.27.0`, `aiohttp>=3.9.0` | — | |
| WeChat | *(covered by core + Slack deps)* | — | Needs `httpx` + `aiohttp` |
| QQ | `qq-botpy` | — | |
| Email | *(none — stdlib only)* | — | Uses `imaplib`, `smtplib` |
| iMessage | *(none)* | `imsg` CLI (macOS) | `brew install anthropics/tap/imsg` |

---

## Configuration

### 1. Create `.env` (project root)

```bash
cp .env.example .env
# edit with your values
```

### 2. Environment Variables

For a guided setup, run `oc onboard`. The wizard writes `.env` using the same variables documented below.

| Variable | Purpose | Required by |
|---|---|---|
| `LLM_PROVIDER` | Provider preset (see table below) — *optional if provider-specific key is set* | All |
| `LLM_API_KEY` | API key (generic) — *optional if provider-specific key is set* | All |
| `LLM_BASE_URL` | Override endpoint URL (optional, auto-set by provider) | All |
| `OMICSCLAW_MODEL` | Override model name (optional, auto-set by provider) | All |
| `OMICSCLAW_LLM_TIMEOUT_SECONDS` | Total timeout for shared LLM requests in seconds (default: `120`) | All |
| `OMICSCLAW_LLM_CONNECT_TIMEOUT_SECONDS` | Connection timeout for shared LLM requests in seconds (default: `10`) | All |
| `TELEGRAM_BOT_TOKEN` | From @BotFather on Telegram | Telegram |
| `TELEGRAM_ALLOWED_SENDERS` | Comma-separated Telegram user ids that identify the configured Owner | Telegram |
| `TELEGRAM_CHAT_ID` | Optional legacy additional Owner user id | Telegram |
| `TELEGRAM_ACCOUNT_NAMESPACE` | Optional assertion; when set it must equal `bot-<authenticated-bot-id>` | Telegram |
| `FEISHU_APP_ID` | From Feishu developer console | Feishu |
| `FEISHU_APP_SECRET` | From Feishu developer console | Feishu |
| `FEISHU_ALLOWED_SENDERS` | **Required.** Comma-separated Owner `open_id` values | Feishu |
| `FEISHU_BOT_OPEN_ID` | **Required.** This Bot's `open_id`, used to prove group @mentions | Feishu |
| `DINGTALK_CLIENT_ID` | Robot App Key | DingTalk |
| `DINGTALK_CLIENT_SECRET` | Robot App Secret | DingTalk |
| `DISCORD_BOT_TOKEN` | Discord bot token | Discord |
| `SLACK_BOT_TOKEN` | Bot User OAuth Token (xoxb-...) | Slack |
| `SLACK_APP_TOKEN` | App-Level Token (xapp-...) for Socket Mode | Slack |
| `WECOM_CORP_ID` | Corp ID (企业微信) | WeChat (WeCom) |
| `WECOM_AGENT_ID` | Agent ID (企业微信) | WeChat (WeCom) |
| `WECOM_SECRET` | App Secret (企业微信) | WeChat (WeCom) |
| `WECHAT_APP_ID` | App ID (公众号, alternative to WeCom) | WeChat (MP) |
| `WECHAT_APP_SECRET` | App Secret (公众号) | WeChat (MP) |
| `QQ_APP_ID` | QQ Bot App ID | QQ |
| `QQ_APP_SECRET` | QQ Bot App Secret | QQ |
| `EMAIL_IMAP_HOST` | IMAP server hostname (e.g. `imap.gmail.com`) | Email |
| `EMAIL_IMAP_USERNAME` | IMAP login username | Email |
| `EMAIL_IMAP_PASSWORD` | IMAP login password / app password | Email |
| `EMAIL_SMTP_HOST` | SMTP server hostname (e.g. `smtp.gmail.com`) | Email |
| `EMAIL_SMTP_USERNAME` | SMTP login username | Email |
| `EMAIL_SMTP_PASSWORD` | SMTP login password / app password | Email |
| `EMAIL_FROM_ADDRESS` | Sender address for outbound emails | Email |
| `IMESSAGE_CLI_PATH` | Path to `imsg` CLI binary (macOS only) | iMessage |
| `IMESSAGE_ALLOWED_SENDERS` | Comma-separated allow-list (phones/emails) | iMessage |
| `OMICSCLAW_DATA_DIRS` | Extra trusted data directories (comma-separated absolute paths) | All |
| `OMICSCLAW_MEMORY_DB_URL` | Persistent graph memory database URL | All |
| `OMICSCLAW_MAX_HISTORY` | Max messages kept in transcript history (default: 50) | All |
| `OMICSCLAW_MAX_HISTORY_CHARS` | Optional transcript character cap (default: 0 = disabled) | All |
| `OMICSCLAW_MAX_TOOL_ITERATIONS` | Max tool iterations per request (default: 20) | All |

The shared provider runtime applies its configured HTTP timeouts; the Delivery
Pump separately bounds each Telegram provider Attempt and classifies ambiguity
as `unknown` rather than replaying the Turn.

### LLM Provider Quick Start

OmicsClaw supports **12 LLM providers** through a unified OpenAI-compatible interface:

| Provider | `LLM_PROVIDER` | Default Model | API Key Env Var |
|---|---|---|---|
| DeepSeek | `deepseek` | `deepseek-v4-flash` | `DEEPSEEK_API_KEY` |
| OpenAI | `openai` | `gpt-5.5` | `OPENAI_API_KEY` |
| Anthropic | `anthropic` | `claude-sonnet-4-6` | `ANTHROPIC_API_KEY` |
| Google Gemini | `gemini` | `gemini-3-flash-preview` | `GOOGLE_API_KEY` |
| NVIDIA NIM | `nvidia` | `nvidia/nemotron-3-super-120b-a12b` | `NVIDIA_API_KEY` |
| SiliconFlow | `siliconflow` | `Pro/zai-org/GLM-5` | `SILICONFLOW_API_KEY` |
| OpenRouter | `openrouter` | `anthropic/claude-sonnet-4.6` | `OPENROUTER_API_KEY` |
| Volcengine 火山引擎 | `volcengine` | `doubao-seed-2-0-pro-260215` | `VOLCENGINE_API_KEY` |
| DashScope 阿里云 | `dashscope` | `qwen3.6-plus` | `DASHSCOPE_API_KEY` |
| Moonshot | `moonshot` | `kimi-k2.6` | `MOONSHOT_API_KEY` |
| Zhipu AI 智谱 | `zhipu` | `glm-5.1` | `ZHIPU_API_KEY` |
| Ollama (local) | `ollama` | `qwen2.5:7b` | *(none)* |
| Custom endpoint | `custom` | *(set `OMICSCLAW_MODEL`)* | *(set `LLM_API_KEY`)* |

**Two ways to configure:**

**Option 1 — Explicit** (set LLM_PROVIDER + LLM_API_KEY):

```bash
LLM_PROVIDER=deepseek
LLM_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**Option 2 — Auto-detect** (set only the provider-specific API key):

```bash
# Just set this — the system auto-detects DeepSeek
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Provider-specific examples:

```bash
# Anthropic Claude
ANTHROPIC_API_KEY=sk-ant-xxxxxxxx

# Google Gemini
GOOGLE_API_KEY=AIzaSyxxxxxxxx

# NVIDIA NIM
NVIDIA_API_KEY=nvapi-xxxxxxxx

# Alibaba DashScope (Qwen)
DASHSCOPE_API_KEY=sk-xxxxxxxx
OMICSCLAW_MODEL=qwen3.6-plus

# Local Ollama
LLM_PROVIDER=ollama
OMICSCLAW_MODEL=qwen2.5:7b

# Custom OpenAI-compatible endpoint
LLM_PROVIDER=custom
LLM_BASE_URL=https://your-endpoint.example.com/v1
OMICSCLAW_MODEL=your-model-name
LLM_API_KEY=sk-xxxxxxxxxxxxxxxx
```

You can also override the auto-configured defaults:

```bash
LLM_PROVIDER=deepseek
LLM_API_KEY=sk-xxxxx
OMICSCLAW_MODEL=deepseek-v4-pro   # Use Pro instead of default Flash
```

---

## Channel Deployment Guides

### Telegram

**Install:** `pip install "python-telegram-bot>=21.0"`

**Prerequisites:**

1. Search for [@BotFather](https://t.me/BotFather) in Telegram, send `/newbot`, and follow the prompts
2. BotFather will return a Bot Token (format: `123456789:ABCdefGHI...`) — save it securely
3. Get your user ID: send any message to [@userinfobot](https://t.me/userinfobot)
4. (Optional) For group use: in BotFather send `/setprivacy` → `Disable`

**Configuration:**

```bash
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrSTUvwxYZ
TELEGRAM_ALLOWED_SENDERS=123456789       # One Owner may have multiple ids
TELEGRAM_CHAT_ID=                        # Optional legacy additional Owner id
```

**Technical details:** Long polling preserves pending updates
(`drop_pending_updates=False`). Owner text is normalized into `RawInboundV1`,
processed by `ControlRuntime`, and the canonical terminal Transcript is rendered
into deterministic Delivery Items. The Telegram Adapter performs exactly one
plain-text `send_message` call per durable Attempt; the Delivery Pump owns
ordering and reliable retry classification. One ordinary photo follows the
Attachment Store path; albums, documents, audio and video are rejected before
download. Group text from a configured Owner follows the same path and
preserves the Telegram topic/thread id when present.

---

### Feishu (Lark)

**Install:** `pip install "lark-oapi>=1.3.0"`

**Prerequisites:**

1. Create an app at [Feishu Open Platform](https://open.feishu.cn/app) (international: [Lark Developer](https://open.larksuite.com/app))
2. Copy the **App ID** and **App Secret**
3. **CRITICAL**: See [bot/CHANNELS_SETUP.md](./CHANNELS_SETUP.md#2-feishu-lark) for detailed instructions on configuring permissions (`im:message`, `im:resource`), subscribing to events, enabling Long Connection, and **Publishing a Version** (strictly required for receiving messages)

**Configuration:**

```bash
FEISHU_APP_ID=cli_xxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxx
FEISHU_ALLOWED_SENDERS=ou_owner_open_id
FEISHU_BOT_OPEN_ID=ou_bot_open_id
```

Both identity variables are mandatory. `FEISHU_ALLOWED_SENDERS` establishes
Owner admission; `FEISHU_BOT_OPEN_ID` proves that a group @mention names this
Bot rather than another member. Unknown chat types, non-text messages,
attachments, rich posts and cards fail closed before Turn submission.

**Technical details:** WebSocket long connection via `lark_oapi.ws.Client`.
Owner text is normalized into `RawInboundV1` and processed by the shared
`ControlRuntime`. The Feishu Adapter makes one text-only provider call per
durable Attempt; the Delivery Pump owns ordering and retry classification.

---

### DingTalk 钉钉

**Install:** `pip install "websockets>=12.0"`

**Prerequisites:**

1. Create a robot at [DingTalk Open Platform](https://open-dev.dingtalk.com)
2. Enable **Stream Mode** (WebSocket, no public IP needed)
3. Publish the app and add the bot to a group, or test via direct message

**Configuration:**

```bash
DINGTALK_CLIENT_ID=ding...
DINGTALK_CLIENT_SECRET=xxxxxxxxxxxxxxxxxx
```

**Technical details:** Stream Mode via WebSocket — connects to DingTalk gateway. Automatic ticket-based auth. Sends via Markdown format. Group @mention detection via `isInAtList`. `accessToken` auto-refresh. Text chunk limit: 4096 chars.

---

### Discord

**Install:** `pip install "discord.py>=2.3.0"`

**Prerequisites:**

1. Go to [Discord Developer Portal](https://discord.com/developers/applications) → New Application
2. Bot → Reset Token → copy the Bot Token
3. Under **Privileged Gateway Intents**, enable **Message Content Intent**
4. OAuth2 → URL Generator: Scopes `bot`, Permissions: `Send Messages`, `Read Message History`, `Attach Files`, `Add Reactions`
5. Open the generated URL in browser, select a server to invite the bot

**Configuration:**

```bash
DISCORD_BOT_TOKEN=MTIzNDU2Nzg5.xxxx.xxxxx
```

**Technical details:** WebSocket Gateway (`discord.py`). In server channels, only responds when @mentioned; DMs respond directly. Thread-aware replies via `MessageReference`. Media sent via `discord.File`. Typing indicator refreshes every 8s. Text chunk limit: 2000 chars.

---

### Slack

**Install:** `pip install "slack-sdk>=3.27.0" "aiohttp>=3.9.0"`

**Prerequisites:**

1. Go to [Slack API](https://api.slack.com/apps) → Create New App → From scratch
2. **Socket Mode** → enable → Generate App-Level Token (scope `connections:write`) → copy `xapp-...`
3. **OAuth & Permissions** → add Bot Token Scopes: `chat:write`, `channels:history`, `groups:history`, `im:history`, `files:read`, `files:write`, `reactions:write`
4. Click **Install to Workspace** → copy Bot Token `xoxb-...`
5. **Event Subscriptions** → enable → Subscribe: `message.channels`, `message.groups`, `message.im`, `app_mention`

**Configuration:**

```bash
SLACK_BOT_TOKEN=xoxb-xxxx-xxxx-xxxx
SLACK_APP_TOKEN=xapp-1-xxxx-xxxx
```

**Technical details:** Socket Mode (no public URL needed). Markdown to mrkdwn conversion. Thread replies via `thread_ts`. Attachments via `files_upload_v2`. Runs `auth_test()` on startup to verify credentials. Text chunk limit: 4000 chars.

---

### WeChat 企业微信 / 公众号

**Install:** `pip install httpx aiohttp` *(both already in core/Slack deps)*

Two backends supported: **WeCom** (recommended, free) and **WeChat Official Account** (requires verified service account).

#### WeCom

1. Log in to [WeCom Admin Console](https://work.weixin.qq.com/) → App Management → create a custom app
2. Copy the **Corp ID**, **AgentId**, and **Secret**
3. In app details → Receive Messages → Set API Receive → URL: `http://your-host:9001/wechat/callback`
4. Copy **Token** and **EncodingAESKey**
5. In app details → **Trusted IP** → add your server's public IP address

```bash
WECOM_CORP_ID=ww...
WECOM_AGENT_ID=1000002
WECOM_SECRET=xxxxxxxxxxxxxxxxxx
WECOM_TOKEN=xxxxxxxxxxxxxxxxxx          # Optional
WECOM_ENCODING_AES_KEY=xxxxxxxxxxxxxxxxxx  # Optional
```

#### Official Account

1. Log in to [WeChat Official Account Platform](https://mp.weixin.qq.com/)
2. Settings & Development → Basic Configuration
3. Copy **AppID** and **AppSecret**

```bash
WECHAT_APP_ID=wx...
WECHAT_APP_SECRET=xxxxxxxxxxxxxxxxxx
```

> ⚠️ Requires a public IP for webhook callbacks (port 9001 by default). For local development, use `ngrok http 9001`.

**Technical details:** Webhook HTTP server for inbound XML message parsing. `access_token` auto-refresh (2h TTL). WeCom supports Markdown; Official Account uses plain text only. Supports text, image, voice, video message types. Text chunk limit: 4096 chars.

---

### QQ

**Install:** `pip install qq-botpy`

**Prerequisites:**

1. Register as a developer at [QQ Open Platform](https://q.qq.com/)
2. Create a bot → complete developer verification
3. Copy the **App ID** and **App Secret**
4. Enable the "C2C消息" and "群聊消息" intents in the bot dashboard
5. Search for and add the bot as a friend in QQ, or add it to a group

**Configuration:**

```bash
QQ_APP_ID=xxxxxxxxxx
QQ_APP_SECRET=xxxxxxxxxxxxxxxxxx
```

> **Note**: QQ bots require intent declarations. The bot uses WebSocket Gateway mode — no public IP required.

**Technical details:** Uses `qq-botpy` SDK via WebSocket to connect to QQ Bot Gateway. Supports C2C (direct) and group messages. Message deduplication (1000-entry LRU cache). Group @mention filtering. Intents: `public_messages=True`, `direct_message=True`. Text chunk limit: 4096 chars.

---

### Email

**Install:** No extra Python dependencies — uses only standard library (`imaplib`, `smtplib`, `email`).

**Prerequisites:**

1. Prepare an email account with IMAP + SMTP support (Gmail, Outlook, self-hosted, etc.)
2. **Gmail**: Enable 2FA → generate an [App Password](https://myaccount.google.com/apppasswords)
3. **Outlook/Office 365**: IMAP: `outlook.office365.com:993`, SMTP: `smtp.office365.com:587`
4. Ensure IMAP access is enabled in your email settings

**Configuration:**

```bash
EMAIL_IMAP_HOST=imap.gmail.com
EMAIL_IMAP_PORT=993
EMAIL_IMAP_USERNAME=your-email@gmail.com
EMAIL_IMAP_PASSWORD=your-app-password
EMAIL_SMTP_HOST=smtp.gmail.com
EMAIL_SMTP_PORT=587
EMAIL_SMTP_USERNAME=your-email@gmail.com
EMAIL_SMTP_PASSWORD=your-app-password
EMAIL_FROM_ADDRESS=your-email@gmail.com
EMAIL_POLL_INTERVAL=30                    # Optional (default: 30s)
EMAIL_MARK_SEEN=1                         # Optional (default: 1)
EMAIL_ALLOWED_SENDERS=                    # Optional: comma-separated
```

> **Common providers:**
> | Provider | IMAP Host | SMTP Host | SMTP Port |
> |---|---|---|---|
> | Gmail | `imap.gmail.com` | `smtp.gmail.com` | 587 |
> | Outlook/365 | `outlook.office365.com` | `smtp.office365.com` | 587 |
> | QQ Mail | `imap.qq.com` | `smtp.qq.com` | 587 |
> | 163 Mail | `imap.163.com` | `smtp.163.com` | 25 |

**Technical details:** IMAP polling mode, checks for UNSEEN emails periodically (max 20 per cycle). Auto-parses multipart emails (prefers text/plain, falls back text/html). Replies set `In-Reply-To` and `References` headers to maintain threads. Sends HTML + plain text dual format. IMAP auto-reconnects. No public IP needed.

---

### iMessage (macOS only)

**Install:** No Python dependencies. Requires the [imsg](https://github.com/anthropics/imsg) CLI tool.

**Requirements:** macOS only — iMessage is Apple-proprietary. Requires a signed-in Apple ID with Messages.app.

**Prerequisites:**

1. Install imsg CLI:
   ```bash
   brew install anthropics/tap/imsg
   ```
2. Verify: `imsg --version`
3. Grant Terminal/IDE **Full Disk Access** in System Settings → Privacy & Security
4. Ensure Messages.app is signed in and working

**Configuration:**

```bash
IMESSAGE_CLI_PATH=/opt/homebrew/bin/imsg    # or $(which imsg)
IMESSAGE_ALLOWED_SENDERS=+1234567890,user@icloud.com
IMESSAGE_SERVICE=auto                       # Optional: imessage | sms | auto
IMESSAGE_REGION=US                          # Optional: phone number region
```

**Allowlist formats:** phone (`+1234567890`), email (`user@icloud.com`), `chat_id:123`, `chat_guid:iMessage;-;+1234567890`, wildcard `*`.

> ⚠️ The iMessage channel performs a runtime macOS check. On non-macOS systems it will raise a clear error at startup.

**Technical details:** JSON-RPC over stdio with imsg CLI. Creates `watch.subscribe` on startup for real-time message streaming (not polling). Supports iMessage + SMS dual channel. Attachments read from local paths. No public IP needed.

---

## Usage

```bash
# Authoritative production runner
python -m omicsclaw.surfaces.channels --channels telegram
python -m omicsclaw.surfaces.channels --channels feishu
python -m omicsclaw.surfaces.channels --channels telegram --health-port 8080
python -m omicsclaw.surfaces.channels --list  # marks remaining Adapters disabled

# Makefile shortcuts
make bot-telegram
make bot-list
```

## Bot Commands (Telegram)

| Command | Description |
|---|---|
| `/start` | Welcome message with instructions |
| `/skills` | List all available OmicsClaw analysis skills |
| `/demo <skill>` | Run a skill demo (e.g. `/demo preprocess`) |
| `/status` | Bot uptime and configuration |
| `/health` | System health check |

## Data Input

### One Telegram photo (declared size up to 20 MiB)

The configured Owner may send one ordinary Telegram photo, with or without a
caption. The Adapter rejects a missing, zero or oversized provider-declared
size before calling Telegram's file API. A stable `file_unique_id` participates
in the ingress fingerprint; duplicate delivery therefore returns the original
Turn before another download. Novel input is downloaded only through a
process-local byte source, verified as immutable image content, and published
as a per-Turn Attachment Record backed by a content-addressed Blob.

Photo albums and documents—including image documents—are separate input shapes
and remain rejected before download. This path does not accept arbitrary small
files merely because they fit under the size limit.

### Large files: Server-side path mode (recommended)

Spatial transcriptomics data files are typically hundreds of MB to several GB, far exceeding messaging upload limits. The recommended workflow:

1. **Place files** in the `data/` directory on the server (or any trusted directory)
2. **Tell the bot** the filename or path in the chat

```
User: 对 data/brain_visium.h5ad 做预处理
Bot:  (runs preprocess on data/brain_visium.h5ad)

User: analyze my_experiment.h5ad
Bot:  (auto-discovers my_experiment.h5ad in data/)

User: run de on /mnt/nas/spatial/sample01.h5ad
Bot:  (reads from NAS if OMICSCLAW_DATA_DIRS includes /mnt/nas/spatial)
```

The bot automatically searches these directories:
- `data/` — primary user data folder
- `examples/` — demo datasets
- `output/` — previous analysis outputs
- Any additional paths in `OMICSCLAW_DATA_DIRS`

To add external data directories (NAS, shared storage, other projects), set in `.env`:

```bash
OMICSCLAW_DATA_DIRS=/mnt/nas/spatial_data,/home/user/experiments
```

Files are only readable from trusted directories. Paths outside these directories are rejected.

### Other attachments

Telegram media groups, documents, audio and video are not accepted. Desktop
uploads, CLI File References and every non-Telegram Adapter also remain outside
this production slice. None falls back to temporary paths or a latest-file
registry.

---

## Security and Access Control

### Telegram Owner boundary

Telegram requires at least one explicit identity for the single configured
Owner. Multiple ids may represent that same Owner:

```bash
TELEGRAM_ALLOWED_SENDERS=<owner-user-id>[,<second-owner-identity>]
# Optional compatibility identity:
TELEGRAM_CHAT_ID=<additional-owner-user-id>
```

Startup fails only when neither source supplies an Owner identity. Every update
and command is checked against the merged identity set before durable ingress.
These ids are aliases of one Owner, not tenants or independent user partitions.
Other adapters are disabled regardless of their legacy allowlist variables.

### Data Security

- Telegram accepts only one ordinary photo whose declared size is at most
  20 MiB; albums, documents, audio/video and outbound media fail closed.
- Attachment bytes are stored owner-private, digest-verified, and referenced by
  opaque per-Turn identity; provider handles and Base64 never become durable
  Transcript content.
- Text enters only as a typed `RawInboundV1`; provider update objects are not a
  durable contract.
- The authenticated bot id fixes the Delivery account namespace, so one process
  cannot claim another bot's queued work.
- Bot tokens and provider error bodies are excluded from surfaced error evidence
  and logging filters redact the configured token.

---

## Troubleshooting

### Bot not responding to messages

1. Check `TELEGRAM_BOT_TOKEN` and that `TELEGRAM_ALLOWED_SENDERS` contains
   exactly the configured Owner id.
2. Confirm the authenticated bot id matches any explicit account namespace.
3. Check sanitized logs from
   `python -m omicsclaw.surfaces.channels --channels telegram --verbose`.

### "channel X not found" or import errors

Install the channel-specific dependencies:
```bash
# Install the declared Channel extras:
pip install -e ".[channels]"
```

### Webhook channels (WeChat) not receiving messages

1. Ensure the webhook URL is publicly reachable (not behind NAT without port forwarding)
2. For local development, use a tunnel: `ngrok http 9001`
3. Verify the callback URL matches exactly (including path)
4. For WeCom: add your server's public IP to the app's **Trusted IP** list

### WeChat API error `60020`

Add your server's public IP to the WeCom app's **Trusted IP** list in the admin console.

### Feishu "event loop already running" error

This concerns `lark_oapi`, which caches a module-level event loop variable at
import time. `FeishuChannel.start()` runs the listener in its own thread and
patches that variable to a fresh loop, so the error should not appear on the
cut-over path. If it does, check that nothing imported `lark_oapi.ws.client`
from inside a running loop before `start()`.

Feishu is an authoritative Channel (ADR 0060/0063 text-only slice): Owner text
and group @-mentions enter the shared `ControlRuntime`, and terminal replies
leave only through the persistent Delivery Outbox. Inbound attachments,
outbound media, rich post/cards and placeholder editing remain fail-closed.

### Token refresh failures

- Feishu/WeChat/DingTalk tokens auto-refresh with a safety margin before expiry
- If the refresh endpoint is unreachable (network issues), messages will fail until the next successful refresh
- Check proxy settings if your server requires a proxy

### Debug Logging

Enable verbose mode for detailed channel logs:

```bash
python -m omicsclaw.surfaces.channels --channels telegram --verbose
# or set environment variable:
export LOG_LEVEL=DEBUG
```

## Logging

The runner emits sanitized lifecycle and Delivery diagnostics through Python
logging. `control.db` is the durable authority for Turn and Delivery state; log
files are not lifecycle evidence.
