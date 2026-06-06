import { useMemo, useState } from 'react'
import { Link, Navigate, useParams } from 'react-router-dom'
import AppShell from '../components/AppShell'
import { useAuth } from '../hooks/useAuth'
import { usePublicModels } from '../hooks/usePublicModels'
import './GuideDetail.css'

const SITE_ROOT = typeof window !== 'undefined' ? window.location.origin : ''
const OPENAI_BASE_URL = SITE_ROOT ? `${SITE_ROOT}/v1` : '/v1'
const CODEX_MODEL_ID = 'gpt-5.4'

function CopyButton({ text, idleLabel = '复制', doneLabel = '已复制' }) {
    const [copied, setCopied] = useState(false)

    const handleCopy = async () => {
        await navigator.clipboard.writeText(text)
        setCopied(true)
        window.setTimeout(() => setCopied(false), 2000)
    }

    return (
        <button className="btn btn-primary btn-sm" onClick={handleCopy}>
            {copied ? `\u2713 ${doneLabel}` : idleLabel}
        </button>
    )
}

function GuideCommand({ title, summary, code }) {
    return (
        <section className="guide-command glass-card">
            <div className="guide-command-header">
                <div>
                    <span className="guide-kicker">Terminal</span>
                    <h2>{title}</h2>
                    <p>{summary}</p>
                </div>
                <CopyButton text={code} idleLabel="复制命令" />
            </div>
            <pre className="guide-code-block">{code}</pre>
        </section>
    )
}

function GuideCodeGrid({ items }) {
    return (
        <section className="guide-code-grid">
            {items.map((item) => (
                <article className="guide-code-card glass-card" key={item.title}>
                    <div className="guide-code-card-head">
                        <div>
                            <span className="guide-kicker">{item.label || 'Example'}</span>
                            <h2>{item.title}</h2>
                            <p>{item.summary}</p>
                        </div>
                        <CopyButton text={item.code} idleLabel="复制" />
                    </div>
                    <pre className="guide-code-block guide-code-block-compact">{item.code}</pre>
                </article>
            ))}
        </section>
    )
}

function GuideCommandGroup({ items }) {
    return (
        <div className="guide-command-group">
            {items.map((item) => (
                <GuideCommand
                    key={item.title}
                    title={item.title}
                    summary={item.summary}
                    code={item.code}
                />
            ))}
        </div>
    )
}

function OtherGuideCard({ item }) {
    return (
        <Link to={`/guides/${item.id}`} className="guide-integration-card glass-card">
            <span className="guide-integration-icon">{item.icon}</span>
            <span className="guide-integration-copy">
                <strong>{item.title}</strong>
                <span>{item.summary}</span>
            </span>
            <span className="guide-integration-arrow">查看</span>
        </Link>
    )
}

function OtherGuideGrid({ items }) {
    return (
        <section className="guide-integration-grid">
            {items.map((item) => (
                <OtherGuideCard key={item.id} item={item} />
            ))}
        </section>
    )
}

export default function GuideDetail() {
    const { guideId } = useParams()
    const { effectiveApiKey, hasDeveloperKey, hasLocalDeveloperKey, latestDeveloperKey } = useAuth()
    const { models, textModels, imageModels, defaultTextModel, defaultImageModel } = usePublicModels()

    const key = effectiveApiKey || ''
    const codingModel = textModels.find((model) => model.id === 'opus')
        || textModels.find((model) => model.id === 'claude-opus-4-8')
        || textModels.find((model) => model.id === 'claude-opus-4-7')
        || defaultTextModel
        || models[0]
    const defaultClaudeModel = 'claude-sonnet-4-6'
    const snippetKey = key || 'YOUR_DEVELOPER_API_KEY'
    const maskedKey = effectiveApiKey
        ? `${effectiveApiKey.slice(0, 8)}\u2022\u2022\u2022\u2022${effectiveApiKey.slice(-4)}`
        : latestDeveloperKey?.masked_key || '还没有本地可用开发者 Key'

    const guides = useMemo(() => {
        const apiQuickstartCommand = `curl ${OPENAI_BASE_URL}/chat/completions \\
  -H "Authorization: Bearer ${snippetKey}" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${codingModel?.id || 'opus'}",
    "messages": [{"role": "user", "content": "Reply with only: OK"}],
    "stream": false
  }'`

        const apiPythonCommand = `from openai import OpenAI

client = OpenAI(
    api_key="${snippetKey}",
    base_url="${OPENAI_BASE_URL}",
)

response = client.chat.completions.create(
    model="${codingModel?.id || 'opus'}",
    messages=[{"role": "user", "content": "Reply with only: OK"}],
)

print(response.choices[0].message.content)`

        const apiJavaScriptCommand = `import OpenAI from "openai";

const client = new OpenAI({
  apiKey: "${snippetKey}",
  baseURL: "${OPENAI_BASE_URL}",
});

const response = await client.chat.completions.create({
  model: "${codingModel?.id || 'opus'}",
  messages: [{ role: "user", content: "Reply with only: OK" }],
});

console.log(response.choices[0].message.content);`

        const apiGoCommand = `package main

import (
  "context"
  "fmt"

  "github.com/openai/openai-go"
  "github.com/openai/openai-go/option"
)

func main() {
  client := openai.NewClient(
    option.WithAPIKey("${snippetKey}"),
    option.WithBaseURL("${OPENAI_BASE_URL}"),
  )

  resp, err := client.Chat.Completions.New(context.Background(), openai.ChatCompletionNewParams{
    Model: "${codingModel?.id || 'opus'}",
    Messages: []openai.ChatCompletionMessageParamUnion{
      openai.UserMessage("Reply with only: OK"),
    },
  })
  if err != nil {
    panic(err)
  }

  fmt.Println(resp.Choices[0].Message.Content)
}`

        const apiPhpCommand = `<?php

$payload = [
    "model" => "${codingModel?.id || 'opus'}",
    "messages" => [
        ["role" => "user", "content" => "Reply with only: OK"],
    ],
];

$ch = curl_init("${OPENAI_BASE_URL}/chat/completions");
curl_setopt_array($ch, [
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_HTTPHEADER => [
        "Authorization: Bearer ${snippetKey}",
        "Content-Type: application/json",
    ],
    CURLOPT_POST => true,
    CURLOPT_POSTFIELDS => json_encode($payload),
]);

echo curl_exec($ch);`

        const apiExamples = [
            {
                label: 'HTTP',
                title: 'cURL',
                summary: '最快确认 Base URL、Key、model 都可用。',
                code: apiQuickstartCommand,
            },
            {
                label: 'Python',
                title: 'Python SDK',
                summary: '适合后端脚本、批处理和服务端调用。',
                code: apiPythonCommand,
            },
            {
                label: 'Node.js',
                title: 'JavaScript SDK',
                summary: '适合 Node 服务、CLI 工具和前端构建脚本。',
                code: apiJavaScriptCommand,
            },
            {
                label: 'Go',
                title: 'Go SDK',
                summary: '适合网关、后端服务和长期运行任务。',
                code: apiGoCommand,
            },
            {
                label: 'PHP',
                title: 'PHP cURL',
                summary: '适合传统 Web 后端和快速集成。',
                code: apiPhpCommand,
            },
        ]

        const codexCommand = `mkdir -p ~/.codex
if [ -f ~/.codex/config.toml ]; then
  cp ~/.codex/config.toml ~/.codex/config.toml.bak.$(date +%Y%m%d%H%M%S)
fi
cat > ~/.codex/config.toml <<'EOF'
model_provider = "coincoin"
model = "${CODEX_MODEL_ID}"
disable_response_storage = true
model_reasoning_effort = "high"
web_search = "live"
personality = "pragmatic"

[model_providers.coincoin]
name = "CoinCoin"
base_url = "${OPENAI_BASE_URL}"
experimental_bearer_token = "${snippetKey}"
wire_api = "responses"
EOF

codex`

        const codexWindowsCommand = `New-Item -ItemType Directory -Force "$HOME\\.codex" | Out-Null
if (Test-Path "$HOME\\.codex\\config.toml") {
  $stamp = Get-Date -Format "yyyyMMddHHmmss"
  Copy-Item "$HOME\\.codex\\config.toml" "$HOME\\.codex\\config.toml.bak.$stamp"
}
@"
model_provider = "coincoin"
model = "${CODEX_MODEL_ID}"
disable_response_storage = true
model_reasoning_effort = "high"
web_search = "live"
personality = "pragmatic"

[model_providers.coincoin]
name = "CoinCoin"
base_url = "${OPENAI_BASE_URL}"
experimental_bearer_token = "${snippetKey}"
wire_api = "responses"
"@ | Set-Content "$HOME\\.codex\\config.toml" -Encoding UTF8

codex`

        const claudeCommand = `COINCOIN_CLAUDE_ENV="$HOME/.coincoin-claude-code.env"
if [ -f "$COINCOIN_CLAUDE_ENV" ]; then
  cp "$COINCOIN_CLAUDE_ENV" "$COINCOIN_CLAUDE_ENV.bak.$(date +%Y%m%d%H%M%S)"
fi
cat > "$COINCOIN_CLAUDE_ENV" <<'EOF'
export ANTHROPIC_BASE_URL="${SITE_ROOT}"
export ANTHROPIC_AUTH_TOKEN="${snippetKey}"
export ANTHROPIC_MODEL="${defaultClaudeModel}"
export ANTHROPIC_DEFAULT_SONNET_MODEL="${defaultClaudeModel}"
export ANTHROPIC_DEFAULT_OPUS_MODEL="claude-opus-4-8"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="claude-haiku-4-5"
EOF

. "$COINCOIN_CLAUDE_ENV"
claude --model "${defaultClaudeModel}"`

        const opencodeCommand = `mkdir -p ~/.config/opencode && cat > ~/.config/opencode/opencode.json <<'EOF'
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "coincoin": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "CoinCoin",
      "options": {
        "baseURL": "${OPENAI_BASE_URL}",
        "apiKey": "${snippetKey}"
      },
      "models": {
        "${codingModel?.id || 'gpt-5.3-codex'}": {}
      }
    }
  },
  "model": "coincoin/${codingModel?.id || 'gpt-5.3-codex'}"
}
EOF

opencode`

        const continueCommand = `mkdir -p ~/.continue && cat > ~/.continue/config.yaml <<'EOF'
name: CoinCoin
version: 0.0.1
schema: v1
models:
  - name: CoinCoin Codex
    provider: openai
    model: ${codingModel?.id || 'gpt-5.3-codex'}
    apiKey: ${snippetKey}
    apiBase: ${OPENAI_BASE_URL}
    roles:
      - chat
      - edit
EOF`

        const aiderCommand = `export OPENAI_API_KEY="${snippetKey}"
export OPENAI_API_BASE="${OPENAI_BASE_URL}"

aider --model openai/${codingModel?.id || 'gpt-5.3-codex'}`

        const openclawCommand = `{
  "models": {
    "providers": {
      "coincoin": {
        "baseUrl": "${OPENAI_BASE_URL}",
        "apiKey": "${snippetKey}",
        "api": "openai-completions",
        "models": [{"id": "${codingModel?.id || 'gpt-5.3-codex'}", "contextWindow": 131072}]
      }
    },
    "defaults": {
      "provider": "coincoin",
      "model": "${codingModel?.id || 'gpt-5.3-codex'}"
    }
  }
}`

        const imageModelId = defaultImageModel?.id || imageModels[0]?.id || 'gpt-image-2'

        const imageGenerationCommand = `curl ${OPENAI_BASE_URL}/images/generations \\
  -H "Authorization: Bearer ${snippetKey}" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${imageModelId}",
    "prompt": "A clean product poster for an AI gateway",
    "size": "1024x1024"
  }'`

        const geminiImageGenerationCommand = `curl ${OPENAI_BASE_URL}/images/generations \\
  -H "Authorization: Bearer ${snippetKey}" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "gemini-image",
    "prompt": "A clean product poster in Gemini image style",
    "size": "1024x1024"
  }'`

        const usageCommand = `curl ${OPENAI_BASE_URL}/usage?limit=5 \\
  -H "Authorization: Bearer ${snippetKey}"`

        const otherGuides = [
            {
                id: 'opencode',
                icon: 'OC',
                title: 'OpenCode 接入',
                summary: 'OpenAI 兼容 provider，适合本地 coding agent。',
            },
            {
                id: 'continue',
                icon: 'CT',
                title: 'Continue 接入',
                summary: 'VS Code / JetBrains 配置，先开 chat 和 edit。',
            },
            {
                id: 'aider',
                icon: 'AI',
                title: 'Aider 接入',
                summary: '命令行和项目级配置，模型用 openai/<alias>。',
            },
            {
                id: 'openclaw',
                icon: 'CL',
                title: 'OpenClaw 接入',
                summary: '推荐 openai-completions 模式。',
            },
            {
                id: 'images',
                icon: 'IMG',
                title: '图片接口',
                summary: '文生图、图编辑和 usage 计费检查。',
            },
        ]

        return {
            'api-quickstart': {
                title: 'API 快速接入',
                description: '用统一 `/v1` 地址接 OpenAI 兼容 SDK。先跑通最小请求，再接入你的业务代码。',
                examples: apiExamples,
            },
            codex: {
                title: 'Codex 配置',
                description: '直接把 token 写进 `~/.codex/config.toml`，不再要求额外改 `~/.zshrc`。',
                commandGroup: [
                    {
                        title: 'macOS / Linux 一键配置',
                        summary: '写入 CoinCoin provider；已有 `~/.codex/config.toml` 时先复制一份带时间戳的备份。',
                        code: codexCommand,
                    },
                    {
                        title: 'Windows PowerShell 一键配置',
                        summary: '写入 `$HOME\\.codex\\config.toml`；旧文件存在时先复制一份带时间戳的备份。',
                        code: codexWindowsCommand,
                    },
                ],
            },
            'claude-code': {
                title: 'Claude Code 配置',
                description: 'Claude Code 走 Anthropic 兼容入口，地址填根域名，不要手动加 `/v1`。',
                commandTitle: '直接用环境变量启动 Claude Code',
                commandSummary: '写入独立环境文件并自动备份旧文件；如果之前登录过官方 Claude，先在 Claude Code 里执行一次 `/logout`。',
                command: claudeCommand,
            },
            opencode: {
                title: 'OpenCode 配置',
                description: 'OpenCode 走 OpenAI-compatible provider，model 用 `coincoin/<公开 alias>`。',
                commandTitle: '写入 OpenCode provider',
                commandSummary: '写入 `~/.config/opencode/opencode.json` 后直接启动 OpenCode。',
                command: opencodeCommand,
            },
            continue: {
                title: 'Continue 配置',
                description: 'Continue 走 OpenAI provider，`apiBase` 填统一 `/v1` 入口。',
                commandTitle: '写入 Continue config.yaml',
                commandSummary: '写入 `~/.continue/config.yaml`。能 chat 后，再按需补 autocomplete/edit 模型。',
                command: continueCommand,
            },
            aider: {
                title: 'Aider 配置',
                description: 'Aider 走 OpenAI-compatible base URL，模型名用 `openai/<公开 alias>`。',
                commandTitle: '用环境变量启动 Aider',
                commandSummary: '如果你有项目级 `.aider.conf.yml`，确保没有覆盖这里的 base URL 和 key。',
                command: aiderCommand,
            },
            openclaw: {
                title: 'OpenClaw 配置',
                description: 'OpenClaw 推荐先用 `openai-completions`，不要绕到内部 gateway。',
                commandTitle: 'Provider 配置片段',
                commandSummary: '把这段合并进 OpenClaw 的模型 provider 配置。',
                command: openclawCommand,
            },
            images: {
                title: '图片接口',
                description: '文生图和图片编辑走同一个公开 `/v1` 入口，成功产出图片后按张计费。',
                commandGroup: [
                    {
                        title: '文生图',
                        summary: '请求里显式传入图片模型，例如 `gpt-image-2`。',
                        code: imageGenerationCommand,
                    },
                    {
                        title: 'Gemini 文生图',
                        summary: '需要 Gemini 生图时显式传 `model: "gemini-image"`。',
                        code: geminiImageGenerationCommand,
                    },
                    {
                        title: '查看最近用量',
                        summary: '小文本请求单条 `cost_cents` 可能是 0，图片请求按张计费更直观。',
                        code: usageCommand,
                    },
                ],
            },
            other: {
                title: '其他接入',
                description: '把低频客户端和图片接口收在这里。先选你的工具，再复制对应配置。',
                integrations: otherGuides,
            },
        }
    }, [codingModel?.id, defaultImageModel?.id, imageModels, key])

    const guide = guideId ? guides[guideId] : null
    if (!guide) {
        return <Navigate to="/guides/api-quickstart" replace />
    }

    return (
        <AppShell title={guide.title} description={guide.description}>
            <div className="guide-page">
                <section className="guide-hero glass-card">
                    <div>
                        <span className="guide-kicker">Guide</span>
                        <h1>{guide.title}</h1>
                        <p>{guide.description}</p>
                    </div>
                    <div className="guide-hero-meta">
                        <span className="meta-pill">开发者 Key：{hasDeveloperKey ? maskedKey : '未生成'}</span>
                        <span className="meta-pill">{hasLocalDeveloperKey ? '当前浏览器可直接复制真 Key' : '当前浏览器没有保存真 Key'}</span>
                    </div>
                </section>

                {!effectiveApiKey && (
                    <section className="guide-alert glass-card">
                        <strong>当前浏览器还没有可直接复制的开发者 Key</strong>
                        <p>先去 <Link to="/api-keys">API 密钥</Link> 页面生成或重新复制一把开发者 Key。拿到明文后，这里才会显示可直接复制的一键命令。</p>
                    </section>
                )}

                {(effectiveApiKey || guide.examples || guide.integrations) && (guide.examples ? (
                    <GuideCodeGrid items={guide.examples} />
                ) : guide.integrations ? (
                    <OtherGuideGrid items={guide.integrations} />
                ) : guide.commandGroup ? (
                    <GuideCommandGroup items={guide.commandGroup} />
                ) : (
                    <GuideCommand
                        title={guide.commandTitle}
                        summary={guide.commandSummary}
                        code={guide.command}
                    />
                ))}
            </div>
        </AppShell>
    )
}
