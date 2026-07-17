import { useMemo, useState } from 'react'
import { Link, Navigate, useParams } from 'react-router-dom'
import AppShell from '../components/AppShell'
import { useAuth } from '../hooks/useAuth'
import { usePublicModels } from '../hooks/usePublicModels'
import { getGuideCodePreview } from './guideCodePreview'
import './GuideDetail.css'

const SITE_ROOT = typeof window !== 'undefined' ? window.location.origin : ''
const OPENAI_BASE_URL = SITE_ROOT ? `${SITE_ROOT}/v1` : '/v1'
const CODEX_MODEL_ID = 'gpt-5.4'
const CLAUDE_DEFAULT_ALIAS = 'sonnet'
const CLAUDE_DEFAULT_MODEL_ID = 'claude-sonnet-4-6'
const CLAUDE_OPUS_OPTIONAL_MODEL_ID = 'claude-opus-4-8'

function CopyButton({ text, idleLabel = '复制', doneLabel = '已复制', className = '', icon = false }) {
    const [copied, setCopied] = useState(false)

    const handleCopy = async () => {
        await navigator.clipboard.writeText(text)
        setCopied(true)
        window.setTimeout(() => setCopied(false), 2000)
    }

    return (
        <button className={`btn btn-primary btn-sm ${className}`.trim()} onClick={handleCopy}>
            {icon && !copied && (
                <span className="guide-copy-button-icon" aria-hidden="true">
                    <svg viewBox="0 0 16 16" focusable="false">
                        <path d="M5 2.5A1.5 1.5 0 0 1 6.5 1h5A1.5 1.5 0 0 1 13 2.5v7A1.5 1.5 0 0 1 11.5 11h-5A1.5 1.5 0 0 1 5 9.5z" />
                        <path d="M3.5 5A1.5 1.5 0 0 0 2 6.5v6A1.5 1.5 0 0 0 3.5 14h5A1.5 1.5 0 0 0 10 12.5V12H6.5A2.5 2.5 0 0 1 4 9.5V5z" />
                    </svg>
                </span>
            )}
            <span>{copied ? `\u2713 ${doneLabel}` : idleLabel}</span>
        </button>
    )
}

function GuideCommand({ title, summary, code, secret }) {
    const previewCode = getGuideCodePreview(code, secret)

    return (
        <section className="guide-command glass-card">
            <div className="guide-command-callout">
                <div className="guide-command-callout-copy">
                    <span className="guide-command-callout-tag">一键配置</span>
                    <strong>复制后直接回终端粘贴回车</strong>
                    <p>{secret ? '页面预览已隐藏 Key，复制内容会自动带上当前开发者 Key。' : '不需要手动分段操作，整段复制即可完成配置。'}</p>
                </div>
                <CopyButton
                    text={code}
                    idleLabel="一键复制整段命令"
                    doneLabel="已复制整段命令"
                    className="guide-copy-button-prominent"
                    icon
                />
            </div>
            <div className="guide-command-header">
                <div>
                    <span className="guide-kicker">Terminal</span>
                    <h2>{title}</h2>
                    <p>{summary}</p>
                </div>
            </div>
            <pre className="guide-code-block">{previewCode}</pre>
        </section>
    )
}

function GuideCodeGrid({ items, secret }) {
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
                    <pre className="guide-code-block guide-code-block-compact">{getGuideCodePreview(item.code, secret)}</pre>
                </article>
            ))}
        </section>
    )
}

function GuideCommandGroup({ items, secret }) {
    return (
        <div className="guide-command-group">
            {items.map((item) => (
                <GuideCommand
                    key={item.title}
                    title={item.title}
                    summary={item.summary}
                    code={item.code}
                    secret={secret}
                />
            ))}
        </div>
    )
}

function GuidePlatformTabs({ items, activeIndex, onSelect, ariaLabel = '操作系统选择', compact = false }) {
    return (
        <div
            className={`guide-command-tab-list ${compact ? 'guide-command-tab-list-compact' : ''}`}
            role="tablist"
            aria-label={ariaLabel}
        >
            {items.map((item, index) => (
                <button
                    key={item.title}
                    type="button"
                    role="tab"
                    aria-selected={activeIndex === index}
                    className={`guide-command-tab ${activeIndex === index ? 'is-active' : ''}`}
                    onClick={() => onSelect(index)}
                >
                    {item.platform || item.title}
                </button>
            ))}
        </div>
    )
}

function GuideCommandTabs({ items, secret }) {
    const [activeIndex, setActiveIndex] = useState(0)
    const activeItem = items[activeIndex] || items[0]

    if (!activeItem) return null

    return (
        <section className="guide-command-tabs glass-card">
            <div className="guide-command-tabs-header">
                <div>
                    <span className="guide-kicker">Platform</span>
                    <h2>选择你的系统</h2>
                </div>
                <GuidePlatformTabs items={items} activeIndex={activeIndex} onSelect={setActiveIndex} />
            </div>
            <GuideCommand
                title={activeItem.title}
                summary={activeItem.summary}
                code={activeItem.code}
                secret={secret}
            />
        </section>
    )
}

function GuideTaskTabs({ tasks, secret }) {
    const [activeTaskIndex, setActiveTaskIndex] = useState(0)
    const [activePlatform, setActivePlatform] = useState(tasks[0]?.items?.[0]?.platform || '')
    const activeTask = tasks[activeTaskIndex] || tasks[0]
    const activeItem = activeTask?.items?.find((item) => item.platform === activePlatform) || activeTask?.items?.[0]
    const activePlatformIndex = activeTask?.items?.indexOf(activeItem) ?? 0

    if (!activeTask || !activeItem) return null

    const selectPlatform = (index) => {
        setActivePlatform(activeTask.items[index]?.platform || '')
    }

    return (
        <section className="guide-task-browser glass-card">
            <div className="guide-task-browser-header">
                <span className="guide-kicker">Image workflow</span>
                <h2>选择图片任务</h2>
                <p>先选任务，再选系统。页面一次只展示当前需要执行的命令。</p>
            </div>

            <div className="guide-task-tab-list" role="tablist" aria-label="图片任务选择">
                {tasks.map((task, index) => (
                    <button
                        key={task.id}
                        type="button"
                        role="tab"
                        aria-selected={activeTaskIndex === index}
                        className={`guide-task-tab ${activeTaskIndex === index ? 'is-active' : ''}`}
                        onClick={() => setActiveTaskIndex(index)}
                    >
                        {task.title}
                    </button>
                ))}
            </div>

            <div className="guide-task-panel" role="tabpanel">
                <div className="guide-task-panel-header">
                    <div>
                        <span className="guide-kicker">Command</span>
                        <h3>{activeTask.title}</h3>
                        <p>{activeTask.summary}</p>
                    </div>
                    <GuidePlatformTabs
                        items={activeTask.items}
                        activeIndex={activePlatformIndex}
                        onSelect={selectPlatform}
                        ariaLabel={`${activeTask.title}系统选择`}
                        compact
                    />
                </div>

                <GuideCommand
                    title={activeItem.title}
                    summary={activeItem.summary}
                    code={activeItem.code}
                    secret={secret}
                />
            </div>
        </section>
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
    const { developerKeyLoading, effectiveApiKey, hasDeveloperKey, latestDeveloperKey } = useAuth({ loadRecoverableKey: true })
    const { models, textModels, imageModels, defaultTextModel, defaultImageModel } = usePublicModels()

    const key = effectiveApiKey || ''
    const codingModel = textModels.find((model) => model.id === CODEX_MODEL_ID)
        || textModels.find((model) => model.id === 'gpt-5.5')
        || defaultTextModel
        || models[0]
    const codingModelId = codingModel?.id || CODEX_MODEL_ID
    const snippetKey = key || 'YOUR_DEVELOPER_API_KEY'
    const maskedKey = effectiveApiKey
        ? `${effectiveApiKey.slice(0, 8)}\u2022\u2022\u2022\u2022${effectiveApiKey.slice(-4)}`
        : latestDeveloperKey?.masked_key || '还没有可用开发者 Key'

    const guides = useMemo(() => {
        const apiQuickstartCommand = `curl ${OPENAI_BASE_URL}/chat/completions \\
  -H "Authorization: Bearer ${snippetKey}" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${codingModelId}",
    "messages": [{"role": "user", "content": "Reply with only: OK"}],
    "stream": false
  }'`

        const apiPythonCommand = `from openai import OpenAI

client = OpenAI(
    api_key="${snippetKey}",
    base_url="${OPENAI_BASE_URL}",
)

response = client.chat.completions.create(
    model="${codingModelId}",
    messages=[{"role": "user", "content": "Reply with only: OK"}],
)

print(response.choices[0].message.content)`

        const apiJavaScriptCommand = `import OpenAI from "openai";

const client = new OpenAI({
  apiKey: "${snippetKey}",
  baseURL: "${OPENAI_BASE_URL}",
});

const response = await client.chat.completions.create({
  model: "${codingModelId}",
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
    Model: "${codingModelId}",
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
    "model" => "${codingModelId}",
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
model_provider = "clawfather"
model = "${CODEX_MODEL_ID}"
disable_response_storage = true
model_reasoning_effort = "high"
web_search = "live"
personality = "pragmatic"

[model_providers.clawfather]
name = "ClawFather"
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
model_provider = "clawfather"
model = "${CODEX_MODEL_ID}"
disable_response_storage = true
model_reasoning_effort = "high"
web_search = "live"
personality = "pragmatic"

[model_providers.clawfather]
name = "ClawFather"
base_url = "${OPENAI_BASE_URL}"
experimental_bearer_token = "${snippetKey}"
wire_api = "responses"
"@ | Set-Content "$HOME\\.codex\\config.toml" -Encoding UTF8

codex`

        const grokBuildCommand = String.raw`if ! command -v grok >/dev/null 2>&1; then
  curl -fsSL https://x.ai/cli/install.sh | bash
  export PATH="$HOME/bin:$HOME/.grok/bin:$PATH"
fi

mkdir -p ~/.grok
CONFIG="$HOME/.grok/config.toml"
[ -f "$CONFIG" ] || touch "$CONFIG"
cp "$CONFIG" "$CONFIG.bak.$(date +%Y%m%d%H%M%S)"

GROK_CONFIG="$CONFIG" python3 <<'PY'
import os
import re
from pathlib import Path

path = Path(os.environ["GROK_CONFIG"])
text = path.read_text(encoding="utf-8")
text = re.sub(r"(?ms)^\[model\.grok-build\]\s*.*?(?=^\[|\Z)", "", text)

models_match = re.search(r"(?ms)^\[models\]\s*.*?(?=^\[|\Z)", text)
if models_match:
    section = models_match.group(0)
    if re.search(r"(?m)^default\s*=", section):
        section = re.sub(r'(?m)^default\s*=.*$', 'default = "grok-build"', section)
    else:
        section = section.rstrip() + '\ndefault = "grok-build"\n'
    text = text[:models_match.start()] + section + text[models_match.end():]
else:
    text = text.rstrip() + '\n\n[models]\ndefault = "grok-build"\n'

block = '''[model.grok-build]
model = "grok-build"
base_url = "${OPENAI_BASE_URL}"
api_key = "${snippetKey}"
name = "Grok Build via CoinCoin"
api_backend = "responses"
context_window = 500000
'''
path.write_text(text.rstrip() + "\n\n" + block, encoding="utf-8")
PY

chmod 600 "$CONFIG"
printf 'Grok Build config written to %s\n' "$CONFIG"
grok inspect
grok -p "Reply exactly: COINCOIN_GROK_BUILD_OK" -m grok-build --output-format json --max-turns 1

TEST_DIR=$(mktemp -d)
printf 'GROK_BUILD_TOOL_LOOP_OK\n' > "$TEST_DIR/probe.txt"
grok --cwd "$TEST_DIR" -p "Read probe.txt with the file tool, then reply with its exact contents." -m grok-build --output-format json --max-turns 3 --always-approve
rm -rf "$TEST_DIR"`

        const grokBuildWindowsCommand = String.raw`if (-not (Get-Command grok -ErrorAction SilentlyContinue)) {
  irm https://x.ai/cli/install.ps1 | iex
}

$GrokDir = Join-Path $HOME ".grok"
$Config = Join-Path $GrokDir "config.toml"
New-Item -ItemType Directory -Force $GrokDir | Out-Null
if (Test-Path $Config) {
  Copy-Item $Config "$Config.bak.$(Get-Date -Format 'yyyyMMddHHmmss')"
  $Text = Get-Content $Config -Raw
} else {
  $Text = ""
}

$Text = [regex]::Replace($Text, '(?ms)^\[model\.grok-build\]\s*.*?(?=^\[|\z)', '')
$ModelsPattern = '(?ms)^\[models\]\s*.*?(?=^\[|\z)'
if ([regex]::IsMatch($Text, $ModelsPattern)) {
  $Text = [regex]::Replace($Text, $ModelsPattern, {
    param($Match)
    $Section = $Match.Value
    if ($Section -match '(?m)^default\s*=') {
      return [regex]::Replace($Section, '(?m)^default\s*=.*$', 'default = "grok-build"')
    }
    return $Section.TrimEnd() + [Environment]::NewLine + 'default = "grok-build"' + [Environment]::NewLine
  })
} else {
  $Text = $Text.TrimEnd() + [Environment]::NewLine + [Environment]::NewLine + '[models]' + [Environment]::NewLine + 'default = "grok-build"' + [Environment]::NewLine
}

$Block = @'
[model.grok-build]
model = "grok-build"
base_url = "${OPENAI_BASE_URL}"
api_key = "${snippetKey}"
name = "Grok Build via CoinCoin"
api_backend = "responses"
context_window = 500000
'@
($Text.TrimEnd() + [Environment]::NewLine + [Environment]::NewLine + $Block) | Set-Content $Config -Encoding UTF8

Write-Host "Grok Build config written to $Config"
grok inspect
grok -p "Reply exactly: COINCOIN_GROK_BUILD_OK" -m grok-build --output-format json --max-turns 1

$TestDir = Join-Path ([System.IO.Path]::GetTempPath()) ("coincoin-grok-build-" + [guid]::NewGuid())
New-Item -ItemType Directory -Force $TestDir | Out-Null
Set-Content (Join-Path $TestDir "probe.txt") "GROK_BUILD_TOOL_LOOP_OK" -Encoding UTF8
grok --cwd $TestDir -p "Read probe.txt with the file tool, then reply with its exact contents." -m grok-build --output-format json --max-turns 3 --always-approve
Remove-Item $TestDir -Recurse -Force`

        const claudeUnixCommand = `CLAUDE_DIR="\${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
SETTINGS_FILE="$CLAUDE_DIR/settings.json"

mkdir -p "$CLAUDE_DIR"
if [ -f "$SETTINGS_FILE" ]; then
  cp "$SETTINGS_FILE" "$SETTINGS_FILE.bak.$(date +%Y%m%d%H%M%S)"
fi

SETTINGS_FILE="$SETTINGS_FILE" python3 <<'EOF'
import json
import os
from pathlib import Path

path = Path(os.environ["SETTINGS_FILE"])
data = {}

if path.exists():
    raw = path.read_text(encoding="utf-8")
    if raw.strip():
        data = json.loads(raw)

if not isinstance(data, dict):
    raise SystemExit("Existing settings.json must contain a JSON object.")

env = data.get("env")
if not isinstance(env, dict):
    env = {}

data["$schema"] = "https://json.schemastore.org/claude-code-settings.json"
env.update({
    "ANTHROPIC_BASE_URL": "${SITE_ROOT}",
    "ANTHROPIC_AUTH_TOKEN": "${snippetKey}",
})
data["env"] = env

path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\\n", encoding="utf-8")
EOF

claude`

        const claudeWindowsCommand = `$EnvMap = @{
  "ANTHROPIC_BASE_URL" = "${SITE_ROOT}"
  "ANTHROPIC_AUTH_TOKEN" = "${snippetKey}"
}

$ClaudeDir = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HOME ".claude" }
New-Item -ItemType Directory -Force $ClaudeDir | Out-Null

$BackupFile = Join-Path $ClaudeDir ("clawfather-env-backup-{0}.txt" -f (Get-Date -Format "yyyyMMddHHmmss"))
$BackupLines = @(
  "# ClawFather Claude Code environment backup",
  "# Created at $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')",
  "# Restore a User value with: [Environment]::SetEnvironmentVariable(""KEY"", ""VALUE"", ""User"")"
)

foreach ($Item in $EnvMap.GetEnumerator()) {
  $BackupLines += "$($Item.Key).User=$([Environment]::GetEnvironmentVariable($Item.Key, 'User'))"
  $BackupLines += "$($Item.Key).Process=$([Environment]::GetEnvironmentVariable($Item.Key, 'Process'))"
}

$BackupLines | Set-Content $BackupFile -Encoding UTF8

foreach ($Item in $EnvMap.GetEnumerator()) {
  [Environment]::SetEnvironmentVariable($Item.Key, $Item.Value, "User")
  Set-Item -Path "Env:$($Item.Key)" -Value $Item.Value
}

Write-Host "Previous Claude Code gateway environment saved to $BackupFile"
Write-Host "Claude Code gateway configured for current and future PowerShell sessions."

claude`

        const opencodeCommand = `mkdir -p ~/.config/opencode && cat > ~/.config/opencode/opencode.json <<'EOF'
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "clawfather": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "ClawFather",
      "options": {
        "baseURL": "${OPENAI_BASE_URL}",
        "apiKey": "${snippetKey}"
      },
      "models": {
        "${codingModelId}": {}
      }
    }
  },
  "model": "clawfather/${codingModelId}"
}
EOF

opencode`

        const continueCommand = `mkdir -p ~/.continue && cat > ~/.continue/config.yaml <<'EOF'
name: ClawFather
version: 0.0.1
schema: v1
models:
  - name: ClawFather Codex
    provider: openai
    model: ${codingModelId}
    apiKey: ${snippetKey}
    apiBase: ${OPENAI_BASE_URL}
    roles:
      - chat
      - edit
EOF`

const aiderCommand = `export OPENAI_API_KEY="${snippetKey}"
export OPENAI_API_BASE="${OPENAI_BASE_URL}"

aider --model openai/${codingModelId}`

        const openclawCommand = `{
  "models": {
    "providers": {
      "clawfather": {
        "baseUrl": "${OPENAI_BASE_URL}",
        "apiKey": "${snippetKey}",
        "api": "openai-completions",
        "models": [{"id": "${codingModelId}", "contextWindow": 131072}]
      }
    },
    "defaults": {
      "provider": "clawfather",
      "model": "${codingModelId}"
    }
  }
}`

        const imageModelId = defaultImageModel?.id || imageModels[0]?.id || 'gpt-image-2'

        const imageGenerationCommand = `OUT="coincoin_image.png"
RESP="$(mktemp)"

curl -sS ${OPENAI_BASE_URL}/images/generations \\
  -H "Authorization: Bearer ${snippetKey}" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${imageModelId}",
    "prompt": "A clean product poster for an AI gateway",
    "size": "1024x1024",
    "n": 1
  }' \\
  -o "$RESP"

python3 - "$RESP" "$OUT" <<'PY'
import base64
import json
import subprocess
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
item = (data.get("data") or [{}])[0]
if item.get("b64_json"):
    open(sys.argv[2], "wb").write(base64.b64decode(item["b64_json"]))
elif item.get("url"):
    subprocess.run(["curl", "-L", "-sS", "--fail", "-o", sys.argv[2], item["url"]], check=True)
else:
    raise SystemExit(json.dumps(data, ensure_ascii=False))
print("saved", sys.argv[2])
PY`

        const imageGenerationWindowsCommand = `$Output = "coincoin_image.png"
$Response = "coincoin_image_response.json"
$Request = "coincoin_image_request.json"
$Body = @{
  model = "${imageModelId}"
  prompt = "A clean product poster for an AI gateway"
  size = "1024x1024"
  n = 1
} | ConvertTo-Json
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText($Request, $Body, $Utf8NoBom)

curl.exe ${OPENAI_BASE_URL}/images/generations \`
  -H "Authorization: Bearer ${snippetKey}" \`
  -H "Content-Type: application/json" \`
  --data-binary "@$Request" \`
  -o $Response

$Result = Get-Content $Response -Raw | ConvertFrom-Json
$Item = $Result.data[0]
if ($Item.b64_json) {
  [IO.File]::WriteAllBytes($Output, [Convert]::FromBase64String($Item.b64_json))
} elseif ($Item.url) {
  curl.exe -L -sS --fail -o $Output $Item.url
  if ($LASTEXITCODE -ne 0) { throw "Image URL download failed." }
} else {
  throw ($Result | ConvertTo-Json -Depth 8)
}
Write-Host "saved $Output"`

        const asyncImageGenerationCommand = `OUT="coincoin_image_async.png"
JOB_JSON="$(mktemp)"
RESULT_JSON="$(mktemp)"

curl -sS ${OPENAI_BASE_URL}/image-jobs/generations \\
  -H "Authorization: Bearer ${snippetKey}" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${imageModelId}",
    "prompt": "A clean product poster for an AI gateway",
    "size": "1024x1024",
    "n": 1
  }' \\
  -o "$JOB_JSON"

JOB_ID=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$JOB_JSON")
echo "queued $JOB_ID"

DEADLINE=$((SECONDS + 600))
while true; do
  curl -sS ${OPENAI_BASE_URL}/image-jobs/$JOB_ID \\
    -H "Authorization: Bearer ${snippetKey}" \\
    -o "$RESULT_JSON"
  STATUS=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["status"])' "$RESULT_JSON")
  echo "status $STATUS"

  if [ "$STATUS" = "completed" ]; then
    python3 - "$RESULT_JSON" "$OUT" <<'PY'
import base64
import json
import subprocess
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
item = (data.get("result", {}).get("data") or [{}])[0]
if item.get("b64_json"):
    open(sys.argv[2], "wb").write(base64.b64decode(item["b64_json"]))
elif item.get("url"):
    subprocess.run(["curl", "-L", "-sS", "--fail", "-o", sys.argv[2], item["url"]], check=True)
else:
    raise SystemExit(json.dumps(data, ensure_ascii=False))
print("saved", sys.argv[2])
PY
    break
  fi
  if [ "$STATUS" = "failed" ]; then
    cat "$RESULT_JSON"
    exit 1
  fi
  if [ "$SECONDS" -gt "$DEADLINE" ]; then
    echo "image job timed out"
    exit 1
  fi
  sleep 5
done`

        const asyncImageGenerationWindowsCommand = `$Output = "coincoin_image_async.png"
$JobJson = "coincoin_image_job.json"
$ResultJson = "coincoin_image_job_result.json"
$RequestJson = "coincoin_image_job_request.json"
$Body = @{
  model = "${imageModelId}"
  prompt = "A clean product poster for an AI gateway"
  size = "1024x1024"
  n = 1
} | ConvertTo-Json
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText($RequestJson, $Body, $Utf8NoBom)

curl.exe ${OPENAI_BASE_URL}/image-jobs/generations \`
  -H "Authorization: Bearer ${snippetKey}" \`
  -H "Content-Type: application/json" \`
  --data-binary "@$RequestJson" \`
  -o $JobJson

$Job = Get-Content $JobJson -Raw | ConvertFrom-Json
Write-Host "queued $($Job.id)"
$Deadline = (Get-Date).AddMinutes(10)

do {
  Start-Sleep -Seconds 5
  curl.exe "${OPENAI_BASE_URL}/image-jobs/$($Job.id)" \`
    -H "Authorization: Bearer ${snippetKey}" \`
    -o $ResultJson
  $Result = Get-Content $ResultJson -Raw | ConvertFrom-Json
  Write-Host "status $($Result.status)"
  if ($Result.status -eq "failed") {
    throw ($Result | ConvertTo-Json -Depth 8)
  }
  if ((Get-Date) -gt $Deadline) {
    throw "image job timed out"
  }
} until ($Result.status -eq "completed")

$Item = $Result.result.data[0]
if ($Item.b64_json) {
  [IO.File]::WriteAllBytes($Output, [Convert]::FromBase64String($Item.b64_json))
} elseif ($Item.url) {
  curl.exe -L -sS --fail -o $Output $Item.url
  if ($LASTEXITCODE -ne 0) { throw "Image URL download failed." }
} else {
  throw ($Result | ConvertTo-Json -Depth 8)
}
Write-Host "saved $Output"`

        const imageEditCommand = `INPUT="./input.png"
OUT="coincoin_image_edit.png"
RESP="$(mktemp)"

if [ ! -f "$INPUT" ]; then
  echo "Put your reference image at $INPUT first."
  exit 1
fi

curl -sS "${OPENAI_BASE_URL}/images/edits" \\
  -H "Authorization: Bearer ${snippetKey}" \\
  -F "model=${imageModelId}" \\
  -F "prompt=Keep the main subject, change the background into a clean studio product scene" \\
  -F "n=1" \\
  -F "size=1024x1024" \\
  -F "image=@$INPUT" \\
  -o "$RESP"

python3 - "$RESP" "$OUT" <<'PY'
import base64
import json
import subprocess
import sys

resp_path, out_path = sys.argv[1], sys.argv[2]
data = json.load(open(resp_path, encoding="utf-8"))
item = (data.get("data") or [{}])[0]
b64 = item.get("b64_json")
if b64:
    open(out_path, "wb").write(base64.b64decode(b64))
elif item.get("url"):
    subprocess.run(["curl", "-L", "-sS", "--fail", "-o", out_path, item["url"]], check=True)
else:
    raise SystemExit(json.dumps(data, ensure_ascii=False))
print("saved", out_path)
PY`

        const imageEditWindowsCommand = `$InputImage = ".\\input.png"
$Output = "coincoin_image_edit.png"
$Response = "coincoin_image_edit_response.json"

if (-not (Test-Path $InputImage)) {
  throw "Put your reference image at $InputImage first."
}

curl.exe "${OPENAI_BASE_URL}/images/edits" \`
  -H "Authorization: Bearer ${snippetKey}" \`
  -F "model=${imageModelId}" \`
  -F "prompt=Keep the main subject, change the background into a clean studio product scene" \`
  -F "n=1" \`
  -F "size=1024x1024" \`
  -F "image=@$InputImage" \`
  -o $Response

$Result = Get-Content $Response -Raw | ConvertFrom-Json
$Item = $Result.data[0]
if ($Item.b64_json) {
  [IO.File]::WriteAllBytes($Output, [Convert]::FromBase64String($Item.b64_json))
} elseif ($Item.url) {
  curl.exe -L -sS --fail -o $Output $Item.url
  if ($LASTEXITCODE -ne 0) { throw "Image URL download failed." }
} else {
  throw ($Result | ConvertTo-Json -Depth 8)
}
Write-Host "saved $Output"`

        const usageCommand = `curl ${OPENAI_BASE_URL}/usage?limit=5 \\
  -H "Authorization: Bearer ${snippetKey}"`

        const usageWindowsCommand = `curl.exe "${OPENAI_BASE_URL}/usage?limit=5" \`
  -H "Authorization: Bearer ${snippetKey}"`

        const otherGuides = [
            {
                id: 'grok-build',
                icon: 'X',
                title: 'Grok Build 接入',
                summary: '官方 CLI、Responses 后端和完整文件工具回路。',
            },
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
                title: '图片接口 / 图生图',
                summary: '同步/异步文生图、单图图生图和 usage 计费检查。',
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
                        platform: 'macOS / Linux',
                        summary: '写入 ClawFather provider；已有 `~/.codex/config.toml` 时先复制一份带时间戳的备份。',
                        code: codexCommand,
                    },
                    {
                        title: 'Windows PowerShell 一键配置',
                        platform: 'Windows',
                        summary: '写入 `$HOME\\.codex\\config.toml`；旧文件存在时先复制一份带时间戳的备份。',
                        code: codexWindowsCommand,
                    },
                ],
                commandGroupMode: 'tabs',
            },
            'grok-build': {
                title: 'Grok Build 配置',
                description: '按 xAI 官方自定义模型格式写入用户级 `~/.grok/config.toml`，把 `grok-build` 指向 CoinCoin Responses 入口。写入后会先运行 `grok inspect`，再验证基础对话和真实文件工具回路；成功后无需登录 xAI 账号。',
                commandGroup: [
                    {
                        title: 'macOS / Linux 一键配置',
                        platform: 'macOS / Linux',
                        summary: '安装官方 CLI、保留其他 Grok 设置、写入并检查 `~/.grok/config.toml`，然后运行文件读取工具回路。',
                        code: grokBuildCommand,
                    },
                    {
                        title: 'Windows PowerShell 一键配置',
                        platform: 'Windows',
                        summary: '安装官方 CLI、备份并检查 `$HOME\\.grok\\config.toml`、替换 `grok-build` 模型段，然后运行文件读取工具回路。',
                        code: grokBuildWindowsCommand,
                    },
                ],
                commandGroupMode: 'tabs',
            },
            'claude-code': {
                title: 'Claude Code 配置',
                description: 'Claude Code 走 Anthropic 兼容入口，地址填根域名，不要手动加 `/v1`。脚本只写 URL 和 Key，模型交给 Claude Code 默认 sonnet。',
                commandGroup: [
                    {
                        title: 'macOS / Linux 一键配置',
                        platform: 'macOS / Linux',
                        summary: '按 Claude Code 官方 `~/.claude/settings.json` 机制写入 URL 和 Key；旧文件先备份，再保留其他设置。',
                        code: claudeUnixCommand,
                    },
                    {
                        title: 'Windows PowerShell 一键配置',
                        platform: 'Windows',
                        summary: '先备份旧 URL / Key 环境变量，再写入当前 PowerShell 和用户级环境变量；不指定模型，交给 Claude Code 默认 sonnet。',
                        code: claudeWindowsCommand,
                    },
                ],
                commandGroupMode: 'tabs',
            },
            opencode: {
                title: 'OpenCode 配置',
                description: 'OpenCode 走 OpenAI-compatible provider，model 用 `clawfather/<公开 alias>`。',
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
                title: '图片接口 / 图生图',
                description: '文生图和单图图生图走同一个公开 `/v1` 入口，成功产出图片后按张计费。同步慢请求会发送 JSON 合法空白保持连接；客户端有总超时时可改用异步文生图任务。',
                commandTasks: [
                    {
                        id: 'sync-generation',
                        title: '同步文生图',
                        summary: '适合能持续读取响应的客户端。示例使用当前默认图片模型，并自动保存 base64 或 URL 结果。',
                        items: [
                            {
                                title: 'macOS / Linux 同步文生图',
                                platform: 'macOS / Linux',
                                summary: '生成完成后保存为 `coincoin_image.png`。示例使用 `1024x1024`；也可按上游支持情况改为 `1536x1024`、`1024x1536` 或 `auto`，最终像素以实际文件为准。',
                                code: imageGenerationCommand,
                            },
                            {
                                title: 'Windows PowerShell 同步文生图',
                                platform: 'Windows PowerShell',
                                summary: '生成完成后保存为 `coincoin_image.png`。`size` 是目标尺寸或 `auto`，最终像素由上游决定；不要把 `1K`、`2K`、`4K` 当成通用兼容值。',
                                code: imageGenerationWindowsCommand,
                            },
                        ],
                    },
                    {
                        id: 'async-generation',
                        title: '异步文生图',
                        summary: '创建任务后轮询结果，避免维持一条可能超过 120 秒的长连接；上游实际生图时间不会因此变短。',
                        items: [
                            {
                                title: 'macOS / Linux 异步文生图',
                                platform: 'macOS / Linux',
                                summary: '创建任务后每 5 秒轮询，完成后保存 `coincoin_image_async.png`。适合客户端或网络链路有总超时限制的情况。',
                                code: asyncImageGenerationCommand,
                            },
                            {
                                title: 'Windows PowerShell 异步文生图',
                                platform: 'Windows PowerShell',
                                summary: '创建任务后每 5 秒轮询，完成后保存 `coincoin_image_async.png`。每次 HTTP 请求都很短。',
                                code: asyncImageGenerationWindowsCommand,
                            },
                        ],
                    },
                    {
                        id: 'single-image-edit',
                        title: '单图图生图',
                        summary: '上传一张参考图到同步 `/images/edits` 接口，完成后把 base64 或 URL 结果保存为本地 PNG。',
                        items: [
                            {
                                title: 'macOS / Linux 单图图生图',
                                platform: 'macOS / Linux',
                                summary: '把参考图保存成 `input.png` 后运行；同步编辑完成后保存 `coincoin_image_edit.png`。',
                                code: imageEditCommand,
                            },
                            {
                                title: 'Windows PowerShell 单图图生图',
                                platform: 'Windows PowerShell',
                                summary: '把参考图保存成 `input.png` 后运行；同步编辑完成后保存 `coincoin_image_edit.png`。',
                                code: imageEditWindowsCommand,
                            },
                        ],
                    },
                    {
                        id: 'usage',
                        title: '查看用量',
                        summary: '读取最近 5 条调用记录，核对图片张数、费用和耗时。',
                        items: [
                            {
                                title: 'macOS / Linux 查看最近用量',
                                platform: 'macOS / Linux',
                                summary: '图片请求按张计费，查看 `image_count`、`cost_usd` 和 `duration_ms`。',
                                code: usageCommand,
                            },
                            {
                                title: 'Windows PowerShell 查看最近用量',
                                platform: 'Windows PowerShell',
                                summary: '图片请求按张计费，查看 `image_count`、`cost_usd` 和 `duration_ms`。',
                                code: usageWindowsCommand,
                            },
                        ],
                    },
                ],
            },
            other: {
                title: '其他接入',
                description: '把低频客户端和图片接口收在这里。先选你的工具，再复制对应配置。',
                integrations: otherGuides,
            },
        }
    }, [codingModelId, defaultImageModel?.id, imageModels, key])

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
                    </div>
                </section>

                {!developerKeyLoading && !effectiveApiKey && (
                    <section className="guide-alert glass-card">
                        <strong>当前浏览器暂时没有可直接复制的开发者 Key</strong>
                        <p>先去 <Link to="/api-keys">API 密钥</Link> 页面刷新、复制或重新生成开发者 Key。拿到明文后，这里会显示可直接复制的一键命令。</p>
                    </section>
                )}

                {(effectiveApiKey || guide.examples || guide.integrations) && (guide.examples ? (
                    <GuideCodeGrid items={guide.examples} secret={effectiveApiKey} />
                ) : guide.integrations ? (
                    <OtherGuideGrid items={guide.integrations} />
                ) : guide.commandTasks ? (
                    <GuideTaskTabs tasks={guide.commandTasks} secret={effectiveApiKey} />
                ) : guide.commandGroupMode === 'tabs' ? (
                    <GuideCommandTabs items={guide.commandGroup} secret={effectiveApiKey} />
                ) : guide.commandGroup ? (
                    <GuideCommandGroup items={guide.commandGroup} secret={effectiveApiKey} />
                ) : (
                    <GuideCommand
                        title={guide.commandTitle}
                        summary={guide.commandSummary}
                        code={guide.command}
                        secret={effectiveApiKey}
                    />
                ))}
            </div>
        </AppShell>
    )
}
