import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { describePublicModel, getApiKey } from '../api/client'
import { usePublicModels } from '../hooks/usePublicModels'
import './Playground.css'

export default function Playground() {
    const { textModels, defaultTextModel, loading: loadingModels } = usePublicModels()
    const [selectedModel, setSelectedModel] = useState(defaultTextModel?.id || 'gpt-5.2-codex')
    const [systemPrompt, setSystemPrompt] = useState('')
    const [userPrompt, setUserPrompt] = useState('')
    const [temperature, setTemperature] = useState(0.7)
    const [maxTokens, setMaxTokens] = useState(2048)
    const [response, setResponse] = useState('')
    const [loading, setLoading] = useState(false)
    const [stats, setStats] = useState(null)
    const abortRef = useRef(null)

    useEffect(() => {
        if (!textModels.find((model) => model.id === selectedModel) && defaultTextModel?.id) {
            setSelectedModel(defaultTextModel.id)
        }
    }, [defaultTextModel, selectedModel, textModels])

    const selectedModelInfo = textModels.find((model) => model.id === selectedModel) || defaultTextModel

    const handleSend = async () => {
        if (!userPrompt.trim() || loading) return
        setResponse('')
        setStats(null)
        setLoading(true)

        const messages = []
        if (systemPrompt.trim()) messages.push({ role: 'system', content: systemPrompt.trim() })
        messages.push({ role: 'user', content: userPrompt.trim() })

        const t0 = performance.now()
        const controller = new AbortController()
        abortRef.current = controller

        try {
            const res = await fetch('/v1/chat/completions', {
                method: 'POST',
                headers: {
                    Authorization: `Bearer ${getApiKey()}`,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    model: selectedModel,
                    messages,
                    temperature: parseFloat(temperature),
                    max_tokens: parseInt(maxTokens),
                    stream: true,
                }),
                signal: controller.signal,
            })

            if (!res.ok) {
                const err = await res.json().catch(() => ({}))
                const msg = err?.error?.message || err?.detail || res.statusText
                if (res.status === 402) {
                    setResponse('__INSUFFICIENT_BALANCE__')
                } else {
                    setResponse(`Error ${res.status}: ${msg}`)
                }
                setLoading(false)
                return
            }

            const reader = res.body.getReader()
            const decoder = new TextDecoder()
            let fullText = ''
            let usage = null

            while (true) {
                const { done, value } = await reader.read()
                if (done) break
                const chunk = decoder.decode(value, { stream: true })
                for (const line of chunk.split('\n')) {
                    if (!line.startsWith('data: ')) continue
                    const data = line.slice(6).trim()
                    if (data === '[DONE]') continue
                    try {
                        const evt = JSON.parse(data)
                        const delta = evt.choices?.[0]?.delta
                        if (delta?.content) {
                            fullText += delta.content
                            setResponse(fullText)
                        }
                        if (evt.usage) usage = evt.usage
                    } catch {
                        // ignore partial SSE fragments
                    }
                }
            }

            const elapsed = Math.round(performance.now() - t0)
            setStats({
                duration: elapsed,
                input_tokens: usage?.prompt_tokens || usage?.input_tokens || 0,
                output_tokens: usage?.completion_tokens || usage?.output_tokens || 0,
                model: selectedModel,
            })
        } catch (e) {
            if (e.name !== 'AbortError') {
                setResponse(`Error: ${e.message}`)
            }
        } finally {
            setLoading(false)
            abortRef.current = null
        }
    }

    const handleStop = () => {
        abortRef.current?.abort()
    }

    return (
        <div className="page-wrapper">
            <div className="container">
                <div className="page-header">
                    <h1 className="page-title">API Playground</h1>
                    <p className="page-desc">在线测试真实公开模型目录，验证只改 <code>model</code> 的实际效果</p>
                </div>

                <div className="playground-layout">
                    <div className="playground-input glass-card animate-fade-in-up">
                        <div className="pg-section">
                            <label className="pg-label">Text Model</label>
                            <select className="pg-select" value={selectedModel} onChange={(e) => setSelectedModel(e.target.value)} disabled={loadingModels || loading}>
                                {textModels.map((model) => (
                                    <option key={model.id} value={model.id}>{model.id}</option>
                                ))}
                            </select>
                            {selectedModelInfo && <p className="pg-model-note">{describePublicModel(selectedModelInfo)}</p>}
                        </div>

                        <div className="pg-section">
                            <label className="pg-label">System Prompt <small>(可选)</small></label>
                            <textarea
                                className="pg-textarea"
                                rows="3"
                                placeholder="设定 AI 的角色和行为..."
                                value={systemPrompt}
                                onChange={e => setSystemPrompt(e.target.value)}
                            />
                        </div>

                        <div className="pg-section">
                            <label className="pg-label">User Prompt</label>
                            <textarea
                                className="pg-textarea pg-main-input"
                                rows="6"
                                placeholder="输入你的问题..."
                                value={userPrompt}
                                onChange={e => setUserPrompt(e.target.value)}
                                onKeyDown={e => { if (e.key === 'Enter' && e.metaKey) handleSend() }}
                            />
                        </div>

                        <div className="pg-params">
                            <div className="pg-param">
                                <label>Temperature: {temperature}</label>
                                <input type="range" min="0" max="2" step="0.1" value={temperature} onChange={e => setTemperature(e.target.value)} />
                            </div>
                            <div className="pg-param">
                                <label>Max Tokens</label>
                                <input type="number" className="pg-number" value={maxTokens} onChange={e => setMaxTokens(e.target.value)} min="1" max="16384" />
                            </div>
                        </div>

                        <div className="pg-actions">
                            {loading ? (
                                <button className="btn btn-secondary" onClick={handleStop}>&#9632; 停止</button>
                            ) : (
                                <button className="btn btn-primary" onClick={handleSend} disabled={!userPrompt.trim()}>
                                    &#9654; 发送 <small>(&#8984;+Enter)</small>
                                </button>
                            )}
                        </div>
                    </div>

                    <div className="playground-output glass-card animate-fade-in-up" style={{ animationDelay: '100ms' }}>
                        <div className="pg-output-header">
                            <span className="pg-label">响应</span>
                            {loading && <div className="loading-spinner" style={{ width: 16, height: 16 }}></div>}
                        </div>
                        <div className="pg-response">
                            {response === '__INSUFFICIENT_BALANCE__' ? (
                                <div className="pg-empty" style={{ color: 'var(--accent-amber)' }}>
                                    余额不足，请先 <Link to="/recharge" style={{ color: 'var(--accent-emerald)', textDecoration: 'underline' }}>充值</Link> 后再试。
                                </div>
                            ) : response ? (
                                <pre className="pg-response-text">{response}</pre>
                            ) : (
                                <div className="pg-empty">发送消息查看响应...</div>
                            )}
                        </div>
                        {stats && (
                            <div className="pg-stats">
                                <span>{stats.model}</span>
                                <span>&#9201; {(stats.duration / 1000).toFixed(1)}s</span>
                                <span>&#8593; {stats.input_tokens.toLocaleString()} tokens</span>
                                <span>&#8595; {stats.output_tokens.toLocaleString()} tokens</span>
                            </div>
                        )}
                    </div>
                </div>
            </div>
        </div>
    )
}
