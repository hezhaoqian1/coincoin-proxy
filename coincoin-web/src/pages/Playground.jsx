import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
    createImageEdit,
    createImageEditJob,
    createImageGeneration,
    createVideoGeneration,
    describePublicModel,
    formatModelPrice,
    getImageJob,
    getMediaArtifacts,
    getVideoGeneration,
} from '../api/client'
import AppShell from '../components/AppShell'
import { useAuth } from '../hooks/useAuth'
import { usePublicModels } from '../hooks/usePublicModels'
import { formatLocalTime } from '../utils/time'
import './Playground.css'

const HISTORY_KEY = 'coincoin_workbench_history_v1'
const HISTORY_MEDIA_DB = 'coincoin_workbench_media_cache_v1'
const HISTORY_MEDIA_STORE = 'media'
const HISTORY_DATA_URL_LIMIT = 360_000
const ACTIVE_TAB_KEY = 'coincoin_workbench_active_tab_v1'
const WORKBENCH_TABS = ['chat', 'image', 'video']
const IMAGE_SIZES = ['1024x1024', '1536x1024', '1024x1536', 'auto']
const IMAGE_QUALITIES = [
    { value: 'auto', label: '自动' },
    { value: 'low', label: '草稿' },
    { value: 'medium', label: '标准' },
    { value: 'high', label: '高清' },
]
const VIDEO_RATIOS = ['16:9', '9:16', '1:1', '4:3', '3:4', '21:9', 'adaptive']
const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled', 'canceled'])
const EMPTY_IMAGE_RUN = {
    runId: '',
    loading: false,
    statusText: '',
    error: '',
    resultImages: [],
}

let imageRunState = { ...EMPTY_IMAGE_RUN }
let imageRunController = null
let historyMediaDbPromise = null
const imageRunListeners = new Set()

function nowId(prefix) {
    return `${prefix}_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
}

function sleep(ms, signal) {
    return new Promise((resolve, reject) => {
        const timer = setTimeout(resolve, ms)
        if (signal) {
            signal.addEventListener('abort', () => {
                clearTimeout(timer)
                reject(new DOMException('Aborted', 'AbortError'))
            }, { once: true })
        }
    })
}

function safeReadHistory() {
    try {
        const parsed = JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]')
        return Array.isArray(parsed) ? parsed : []
    } catch {
        return []
    }
}

function isLargeDataUrl(url) {
    return typeof url === 'string' && url.startsWith('data:') && url.length > HISTORY_DATA_URL_LIMIT
}

function openHistoryMediaDb() {
    if (typeof indexedDB === 'undefined') return Promise.reject(new Error('IndexedDB unavailable'))
    if (!historyMediaDbPromise) {
        historyMediaDbPromise = new Promise((resolve, reject) => {
            const request = indexedDB.open(HISTORY_MEDIA_DB, 1)
            request.onupgradeneeded = () => {
                const db = request.result
                if (!db.objectStoreNames.contains(HISTORY_MEDIA_STORE)) {
                    db.createObjectStore(HISTORY_MEDIA_STORE)
                }
            }
            request.onsuccess = () => resolve(request.result)
            request.onerror = () => reject(request.error || new Error('Failed to open media cache'))
        })
    }
    return historyMediaDbPromise
}

async function writeCachedHistoryMedia(cacheKey, url) {
    if (!cacheKey || !url) return
    const db = await openHistoryMediaDb()
    await new Promise((resolve, reject) => {
        const tx = db.transaction(HISTORY_MEDIA_STORE, 'readwrite')
        tx.objectStore(HISTORY_MEDIA_STORE).put(url, cacheKey)
        tx.oncomplete = resolve
        tx.onerror = () => reject(tx.error || new Error('Failed to write media cache'))
    })
}

async function readCachedHistoryMedia(cacheKey) {
    if (!cacheKey) return ''
    const db = await openHistoryMediaDb()
    return new Promise((resolve, reject) => {
        const tx = db.transaction(HISTORY_MEDIA_STORE, 'readonly')
        const request = tx.objectStore(HISTORY_MEDIA_STORE).get(cacheKey)
        request.onsuccess = () => resolve(typeof request.result === 'string' ? request.result : '')
        request.onerror = () => reject(request.error || new Error('Failed to read media cache'))
    })
}

function cacheHistoryMedia(cacheKey, url) {
    writeCachedHistoryMedia(cacheKey, url).catch(() => {
        // Best-effort browser cache. The history entry remains without preview if this fails.
    })
}

function compactHistoryRecord(record) {
    if (!isLargeDataUrl(record.url)) return record
    const cacheKey = record.cacheKey || record.id || nowId('media_cache')
    cacheHistoryMedia(cacheKey, record.url)
    return {
        ...record,
        url: '',
        cacheKey,
        cachedMedia: true,
        previewUnavailable: false,
    }
}

function safeStoreHistory(records) {
    try {
        const compact = records.slice(0, 24).map(compactHistoryRecord)
        localStorage.setItem(HISTORY_KEY, JSON.stringify(compact))
    } catch {
        // Best-effort local history. Large image data URLs can exceed browser quota.
    }
}

function safeReadActiveTab() {
    try {
        const value = sessionStorage.getItem(ACTIVE_TAB_KEY)
        return WORKBENCH_TABS.includes(value) ? value : 'chat'
    } catch {
        return 'chat'
    }
}

function safeStoreActiveTab(tab) {
    try {
        sessionStorage.setItem(ACTIVE_TAB_KEY, tab)
    } catch {
        // Best-effort workbench tab restore.
    }
}

function dedupeHistory(items, limit = 30) {
    const seen = new Set()
    return items.filter((item) => {
        const key = `${item.type}:${item.id || item.url}`
        if (seen.has(key)) return false
        seen.add(key)
        return true
    }).slice(0, limit)
}

function mergeStoredHistory(items) {
    const compact = dedupeHistory([...items, ...safeReadHistory()])
    safeStoreHistory(compact)
    return compact
}

async function restoreCachedHistory(records) {
    let changed = false
    const restored = await Promise.all(records.map(async (record) => {
        if (record.url || !record.cacheKey || !record.cachedMedia) return record
        try {
            const url = await readCachedHistoryMedia(record.cacheKey)
            if (!url) return record
            changed = true
            return { ...record, url, previewUnavailable: false }
        } catch {
            return record
        }
    }))
    return changed ? restored : records
}

function extractErrorMessage(error) {
    return error?.message || '请求失败'
}

function normalizeMediaUrl(value) {
    if (typeof value === 'string') return value
    if (value && typeof value === 'object' && typeof value.url === 'string') return value.url
    return ''
}

function extractImages(payload) {
    const candidates = []
    const pushData = (value) => {
        if (Array.isArray(value)) candidates.push(...value)
    }

    pushData(payload?.data)
    pushData(payload?.result?.data)
    pushData(payload?.result?.images)
    pushData(payload?.images)

    const nestedData = payload?.result?.output?.data || payload?.output?.data
    pushData(nestedData)

    return candidates
        .map((item, index) => {
            const rawUrl = normalizeMediaUrl(item?.url) || normalizeMediaUrl(item?.image_url) || normalizeMediaUrl(item?.download_url)
            const b64 = item?.b64_json || item?.base64 || item?.image_base64
            const url = rawUrl || (b64 ? `data:image/png;base64,${b64}` : '')
            if (!url) return null
            return {
                id: nowId(`img${index}`),
                type: 'image',
                url,
                revisedPrompt: item?.revised_prompt || '',
            }
        })
        .filter(Boolean)
}

function extractVideoUrl(payload) {
    return normalizeMediaUrl(payload?.output?.url)
        || normalizeMediaUrl(payload?.output?.video_url)
        || normalizeMediaUrl(payload?.result?.output?.url)
        || normalizeMediaUrl(payload?.result?.output?.video_url)
        || normalizeMediaUrl(payload?.result?.data?.output?.url)
        || normalizeMediaUrl(payload?.result?.data?.output?.video_url)
        || normalizeMediaUrl(payload?.result?.data?.url)
        || normalizeMediaUrl(payload?.result?.data?.video_url)
        || normalizeMediaUrl(payload?.result?.url)
        || normalizeMediaUrl(payload?.result?.video_url)
        || normalizeMediaUrl(payload?.url)
        || normalizeMediaUrl(payload?.video_url)
        || ''
}

function formatCost(cents) {
    const value = Number(cents || 0)
    if (!value) return ''
    return `$${(value / 100).toFixed(2)}`
}

function normalizeTokenCount(...values) {
    for (const value of values) {
        if (value === null || value === undefined) continue
        const number = Number(value)
        if (Number.isFinite(number)) return number
    }
    return null
}

function formatTokenCount(value) {
    return typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString() : '-'
}

function canUseAsRemoteReference(url) {
    return /^https?:\/\//i.test(url || '')
}

async function dataUrlToFile(url, filename = 'reference.png') {
    const res = await fetch(url)
    const blob = await res.blob()
    return new File([blob], filename, { type: blob.type || 'image/png' })
}

function emitImageRunState(patch) {
    imageRunState = { ...imageRunState, ...patch }
    imageRunListeners.forEach((listener) => listener(imageRunState))
}

function subscribeImageRunState(listener) {
    imageRunListeners.add(listener)
    listener(imageRunState)
    return () => imageRunListeners.delete(listener)
}

function cancelImageRun() {
    imageRunController?.abort()
}

async function pollImageRunJob(apiKey, job, signal) {
    let current = job
    for (let attempt = 0; attempt < 80; attempt += 1) {
        if (TERMINAL_STATUSES.has(current.status)) return current
        emitImageRunState({ statusText: `图片任务 ${current.status || 'queued'} · ${attempt + 1}` })
        await sleep(2500, signal)
        current = await getImageJob(apiKey, current.id || current.job_id, { signal })
    }
    return current
}

async function startImageRun({ apiKey, modelId, prompt, mode, body, formData }) {
    if (imageRunState.loading) return

    const runId = nowId('image_run')
    const controller = new AbortController()
    imageRunController = controller
    emitImageRunState({
        runId,
        loading: true,
        error: '',
        statusText: mode === 'edit-job' ? '提交图片任务' : (mode === 'edit' ? '提交图片编辑' : '提交图片生成'),
        resultImages: [],
    })

    try {
        let payload
        if (mode === 'generation') {
            payload = await createImageGeneration(apiKey, body, { signal: controller.signal })
        } else if (mode === 'edit') {
            payload = await createImageEdit(apiKey, formData, { signal: controller.signal })
        } else {
            const job = await createImageEditJob(apiKey, formData, { signal: controller.signal })
            payload = await pollImageRunJob(apiKey, job, controller.signal)
            if (payload.status === 'failed') {
                throw new Error(payload.error?.message || '图片任务失败')
            }
        }

        const images = extractImages(payload).map((image, index) => ({
            ...image,
            model: modelId,
            prompt,
            createdAt: new Date().toISOString(),
            title: `图片 ${index + 1}`,
        }))
        if (!images.length) throw new Error('响应里没有图片结果')
        mergeStoredHistory(images)
        emitImageRunState({
            runId,
            loading: false,
            statusText: '完成',
            error: '',
            resultImages: images,
        })
    } catch (err) {
        if (err.name === 'AbortError') {
            emitImageRunState({ runId, loading: false, statusText: '已停止', error: '' })
        } else {
            emitImageRunState({ runId, loading: false, error: extractErrorMessage(err) })
        }
    } finally {
        if (imageRunState.runId === runId) imageRunController = null
    }
}

function downloadMedia(url, filename) {
    if (!url) return
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = filename
    anchor.target = '_blank'
    anchor.rel = 'noreferrer'
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
}

function KeyNotice({ authMode, hasDeveloperKey, hasLocalDeveloperKey }) {
    if (hasLocalDeveloperKey) return null
    return (
        <div className="wb-alert">
            <strong>需要开发者 Key</strong>
            <span>
                {authMode === 'session_only'
                    ? (hasDeveloperKey ? '当前浏览器没有保存明文，请重新生成。' : '请先生成开发者 Key。')
                    : '请使用开发者 Key 登录或回控制台生成。'}
            </span>
        </div>
    )
}

function ModelSelect({ label, models, value, onChange, disabled, fallbackLabel = '暂无模型' }) {
    return (
        <label className="wb-field">
            <span>{label}</span>
            <select value={value} onChange={(event) => onChange(event.target.value)} disabled={disabled || models.length === 0}>
                {models.length === 0 ? (
                    <option value="">{fallbackLabel}</option>
                ) : models.map((model) => (
                    <option key={model.id} value={model.id}>{model.id}</option>
                ))}
            </select>
        </label>
    )
}

function ApiMediaRecords({ records, loading, activeTab, setVideoReference }) {
    const filtered = records.filter((record) => record.media_type === activeTab || record.type === activeTab)
    return (
        <div className="wb-api-records">
            <div className="wb-history-head">
                <span>API 媒体</span>
                {loading ? <small>加载中</small> : <small>{filtered.length} 条</small>}
            </div>
            {filtered.length === 0 ? (
                <div className="wb-empty-line">暂无媒体记录</div>
            ) : (
                <div className="wb-api-grid">
                    {filtered.slice(0, 12).map((record) => (
                        <div key={record.id || `${record.created_at}_${record.url}`} className="wb-api-card">
                            <div className="wb-api-thumb">
                                {(record.media_type || record.type) === 'video' ? (
                                    <video src={record.url} muted playsInline preload="metadata" />
                                ) : (
                                    <img src={record.thumbnail_url || record.url} alt={record.model || 'image'} loading="lazy" />
                                )}
                            </div>
                            <div>
                                <strong>{record.model || '-'}</strong>
                                <span>{record.created_at ? formatLocalTime(record.created_at) : record.endpoint}</span>
                            </div>
                            <div className="wb-history-actions">
                                {record.url ? <button type="button" onClick={() => downloadMedia(record.url, (record.media_type || record.type) === 'video' ? 'coincoin-api-video.mp4' : 'coincoin-api-image.png')}>下载</button> : null}
                                {(record.media_type || record.type) === 'image' && canUseAsRemoteReference(record.url) ? (
                                    <button type="button" onClick={() => setVideoReference(record.url)}>视频参考</button>
                                ) : null}
                                {record.cost_cents ? <span>{formatCost(record.cost_cents)}</span> : null}
                            </div>
                        </div>
                    ))}
                </div>
            )}
        </div>
    )
}

function ChatWorkspace({
    authMode,
    effectiveApiKey,
    hasDeveloperKey,
    hasLocalDeveloperKey,
    loadingModels,
    models,
    selectedModel,
    setSelectedModel,
    selectedModelInfo,
}) {
    const [systemPrompt, setSystemPrompt] = useState('')
    const [userPrompt, setUserPrompt] = useState('')
    const [temperature, setTemperature] = useState(0.7)
    const [maxTokens, setMaxTokens] = useState(2048)
    const [response, setResponse] = useState('')
    const [loading, setLoading] = useState(false)
    const [stats, setStats] = useState(null)
    const abortRef = useRef(null)

    const handleSend = async () => {
        if (!userPrompt.trim() || loading) return
        if (!effectiveApiKey) {
            setResponse('Error: 当前没有可用的开发者 API Key。')
            setStats(null)
            return
        }
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
                    Authorization: `Bearer ${effectiveApiKey}`,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    model: selectedModel,
                    messages,
                    temperature: parseFloat(temperature),
                    max_tokens: parseInt(maxTokens, 10),
                    stream: true,
                    stream_options: { include_usage: true },
                }),
                signal: controller.signal,
            })

            if (!res.ok) {
                const err = await res.json().catch(() => ({}))
                const msg = err?.error?.message || err?.detail || res.statusText
                setResponse(res.status === 402 ? '__INSUFFICIENT_BALANCE__' : `Error ${res.status}: ${msg}`)
                setLoading(false)
                return
            }

            const reader = res.body.getReader()
            const decoder = new TextDecoder()
            let fullText = ''
            let usage = null
            let sseBuffer = ''

            const processSseLine = (line) => {
                if (!line.startsWith('data:')) return
                const data = line.slice(5).trim()
                if (!data || data === '[DONE]') return
                try {
                    const evt = JSON.parse(data)
                    const delta = evt.choices?.[0]?.delta
                    if (delta?.content) {
                        fullText += delta.content
                        setResponse(fullText)
                    }
                    if (evt.usage && typeof evt.usage === 'object') usage = evt.usage
                } catch {
                    // Ignore malformed SSE data from interrupted streams.
                }
            }

            while (true) {
                const { done, value } = await reader.read()
                if (done) break
                sseBuffer += decoder.decode(value, { stream: true })
                const lines = sseBuffer.split(/\r?\n/)
                sseBuffer = lines.pop() || ''
                for (const line of lines) {
                    processSseLine(line)
                }
            }
            sseBuffer += decoder.decode()
            if (sseBuffer.trim()) {
                for (const line of sseBuffer.split(/\r?\n/)) processSseLine(line)
            }

            setStats({
                duration: Math.round(performance.now() - t0),
                input_tokens: normalizeTokenCount(usage?.prompt_tokens, usage?.input_tokens),
                output_tokens: normalizeTokenCount(usage?.completion_tokens, usage?.output_tokens),
                model: selectedModel,
            })
        } catch (error) {
            if (error.name !== 'AbortError') setResponse(`Error: ${error.message}`)
        } finally {
            setLoading(false)
            abortRef.current = null
        }
    }

    return (
        <div className="wb-layout">
            <aside className="wb-rail">
                <KeyNotice authMode={authMode} hasDeveloperKey={hasDeveloperKey} hasLocalDeveloperKey={hasLocalDeveloperKey} />
                <ModelSelect
                    label="模型"
                    models={models}
                    value={selectedModel}
                    onChange={setSelectedModel}
                    disabled={loadingModels || loading}
                />
                {selectedModelInfo ? (
                    <div className="wb-model-note">
                        <span>{formatModelPrice(selectedModelInfo)}</span>
                        <small>{describePublicModel(selectedModelInfo)}</small>
                    </div>
                ) : null}
                <label className="wb-field">
                    <span>System</span>
                    <textarea rows="4" value={systemPrompt} onChange={(event) => setSystemPrompt(event.target.value)} placeholder="可选" />
                </label>
                <div className="wb-split">
                    <label className="wb-field">
                        <span>Temperature</span>
                        <input type="number" min="0" max="2" step="0.1" value={temperature} onChange={(event) => setTemperature(event.target.value)} />
                    </label>
                    <label className="wb-field">
                        <span>Max tokens</span>
                        <input type="number" min="1" max="16384" value={maxTokens} onChange={(event) => setMaxTokens(event.target.value)} />
                    </label>
                </div>
            </aside>

            <section className="wb-stage">
                <div className="wb-result-surface wb-chat-surface">
                    <div className="wb-stage-head">
                        <span>响应</span>
                        {loading ? <div className="loading-spinner wb-spinner" /> : null}
                    </div>
                    <div className="wb-chat-response">
                        {response === '__INSUFFICIENT_BALANCE__' ? (
                            <div className="wb-empty-state">余额不足，请先 <Link to="/recharge">充值</Link>。</div>
                        ) : response ? (
                            <pre>{response}</pre>
                        ) : (
                            <div className="wb-empty-state">发送后查看响应</div>
                        )}
                    </div>
                    {stats ? (
                        <div className="wb-stats">
                            <span>{stats.model}</span>
                            <span>{(stats.duration / 1000).toFixed(1)}s</span>
                            <span>输入 {formatTokenCount(stats.input_tokens)}</span>
                            <span>输出 {formatTokenCount(stats.output_tokens)}</span>
                        </div>
                    ) : null}
                </div>
                <div className="wb-composer">
                    <textarea
                        value={userPrompt}
                        onChange={(event) => setUserPrompt(event.target.value)}
                        onKeyDown={(event) => {
                            if (event.key === 'Enter' && event.metaKey) handleSend()
                        }}
                        placeholder="输入消息..."
                        rows="4"
                    />
                    <div className="wb-composer-actions">
                        {loading ? (
                            <button className="btn btn-secondary" onClick={() => abortRef.current?.abort()}>停止</button>
                        ) : (
                            <button className="btn btn-primary" onClick={handleSend} disabled={!userPrompt.trim() || !effectiveApiKey || !selectedModel}>发送</button>
                        )}
                    </div>
                </div>
            </section>
        </div>
    )
}

function ImageWorkspace({
    authMode,
    effectiveApiKey,
    hasDeveloperKey,
    hasLocalDeveloperKey,
    loadingModels,
    models,
    selectedModel,
    setSelectedModel,
    selectedModelInfo,
    history,
    setVideoReference,
}) {
    const [customModel, setCustomModel] = useState('')
    const [prompt, setPrompt] = useState('')
    const [size, setSize] = useState('1024x1024')
    const [quality, setQuality] = useState('auto')
    const [count, setCount] = useState(1)
    const [references, setReferences] = useState([])
    const [runState, setRunState] = useState(imageRunState)

    useEffect(() => subscribeImageRunState(setRunState), [])

    useEffect(() => () => {
        references.forEach((item) => URL.revokeObjectURL(item.previewUrl))
    }, [references])

    const modelId = customModel.trim() || selectedModel
    const { loading, error, statusText, resultImages } = runState

    const handleReferenceChange = (event) => {
        const files = Array.from(event.target.files || []).slice(0, 8)
        references.forEach((item) => URL.revokeObjectURL(item.previewUrl))
        setReferences(files.map((file) => ({
            id: nowId('ref'),
            file,
            name: file.name,
            previewUrl: URL.createObjectURL(file),
        })))
        event.target.value = ''
    }

    const clearReferences = () => {
        references.forEach((item) => URL.revokeObjectURL(item.previewUrl))
        setReferences([])
    }

    const buildImageForm = () => {
        const form = new FormData()
        form.append('model', modelId)
        form.append('prompt', prompt.trim())
        if (size !== 'auto') form.append('size', size)
        form.append('n', String(Math.max(1, Math.min(4, Number(count) || 1))))
        if (quality !== 'auto') form.append('quality', quality)
        references.forEach((item, index) => {
            form.append(references.length > 1 ? 'image[]' : 'image', item.file, item.file.name || `reference-${index}.png`)
        })
        return form
    }

    const handleGenerate = async () => {
        if (!prompt.trim() || !modelId || loading) return
        if (!effectiveApiKey) {
            emitImageRunState({ error: '当前没有可用的开发者 API Key。' })
            return
        }

        const trimmedPrompt = prompt.trim()
        const body = references.length === 0 ? {
            model: modelId,
            prompt: trimmedPrompt,
            n: Math.max(1, Math.min(4, Number(count) || 1)),
        } : null
        if (body && size !== 'auto') body.size = size
        if (body && quality !== 'auto') body.quality = quality

        startImageRun({
            apiKey: effectiveApiKey,
            modelId,
            prompt: trimmedPrompt,
            mode: references.length === 0 ? 'generation' : (references.length <= 2 ? 'edit' : 'edit-job'),
            body,
            formData: references.length ? buildImageForm() : null,
        })
    }

    const useResultAsReference = async (image) => {
        emitImageRunState({ error: '' })
        try {
            if (!image.url) return
            const file = await dataUrlToFile(image.url, 'generated-reference.png')
            clearReferences()
            setReferences([{
                id: nowId('ref'),
                file,
                name: file.name,
                previewUrl: URL.createObjectURL(file),
            }])
        } catch {
            emitImageRunState({ error: '无法读取该图片，请下载后上传。' })
        }
    }

    const latestUsable = history.find((item) => item.type === 'image' && item.url)

    return (
        <div className="wb-layout">
            <aside className="wb-rail">
                <KeyNotice authMode={authMode} hasDeveloperKey={hasDeveloperKey} hasLocalDeveloperKey={hasLocalDeveloperKey} />
                <ModelSelect label="推荐模型" models={models} value={selectedModel} onChange={setSelectedModel} disabled={loadingModels || loading} />
                <label className="wb-field">
                    <span>自定义模型</span>
                    <input value={customModel} onChange={(event) => setCustomModel(event.target.value)} placeholder="留空使用推荐模型" />
                </label>
                {selectedModelInfo ? (
                    <div className="wb-model-note">
                        <span>{formatModelPrice(selectedModelInfo)}</span>
                        <small>{describePublicModel(selectedModelInfo)}</small>
                    </div>
                ) : null}
                <div className="wb-split">
                    <label className="wb-field">
                        <span>尺寸</span>
                        <select value={size} onChange={(event) => setSize(event.target.value)}>
                            {IMAGE_SIZES.map((option) => <option key={option} value={option}>{option}</option>)}
                        </select>
                    </label>
                    <label className="wb-field">
                        <span>清晰度</span>
                        <select value={quality} onChange={(event) => setQuality(event.target.value)}>
                            {IMAGE_QUALITIES.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                        </select>
                    </label>
                </div>
                <label className="wb-field">
                    <span>张数</span>
                    <input type="number" min="1" max="4" value={count} onChange={(event) => setCount(event.target.value)} />
                </label>
                <div className="wb-upload">
                    <div className="wb-upload-head">
                        <span>参考图</span>
                        {references.length ? <button type="button" onClick={clearReferences}>清除</button> : null}
                    </div>
                    <label className="wb-upload-box">
                        <input type="file" accept="image/*" multiple onChange={handleReferenceChange} />
                        <span>上传参考图</span>
                    </label>
                    {references.length ? (
                        <div className="wb-reference-grid">
                            {references.map((item) => <img key={item.id} src={item.previewUrl} alt={item.name} />)}
                        </div>
                    ) : null}
                </div>
                {latestUsable ? (
                    <button className="btn btn-secondary btn-sm wb-wide-btn" type="button" onClick={() => useResultAsReference(latestUsable)}>
                        引用最近结果
                    </button>
                ) : null}
            </aside>

            <section className="wb-stage">
                <div className="wb-result-surface wb-media-surface">
                    <div className="wb-stage-head">
                        <span>图片结果</span>
                        <small>{statusText}</small>
                    </div>
                    {error ? <div className="wb-error">{error}</div> : null}
                    {loading ? (
                        <div className="wb-empty-state"><div className="loading-spinner wb-spinner-lg" />生成中</div>
                    ) : resultImages.length ? (
                        <div className="wb-image-grid">
                            {resultImages.map((image, index) => (
                                <figure key={image.id} className="wb-image-result">
                                    <img src={image.url} alt={image.prompt || `generated ${index + 1}`} />
                                    <figcaption>
                                        <button type="button" onClick={() => downloadMedia(image.url, `coincoin-image-${index + 1}.png`)}>下载</button>
                                        <button type="button" onClick={() => useResultAsReference(image)}>继续修改</button>
                                        <button type="button" disabled={!canUseAsRemoteReference(image.url)} onClick={() => setVideoReference(image.url)}>用作视频参考</button>
                                    </figcaption>
                                </figure>
                            ))}
                        </div>
                    ) : (
                        <div className="wb-empty-state">生成结果会显示在这里</div>
                    )}
                </div>
                <div className="wb-composer">
                    <textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="描述你想要的画面..." rows="4" />
                    <div className="wb-composer-actions">
                        {loading ? (
                            <button className="btn btn-secondary" onClick={cancelImageRun}>停止</button>
                        ) : (
                            <button className="btn btn-primary" onClick={handleGenerate} disabled={!prompt.trim() || !modelId || !effectiveApiKey}>开始生成</button>
                        )}
                    </div>
                </div>
            </section>
        </div>
    )
}

function VideoWorkspace({
    authMode,
    effectiveApiKey,
    hasDeveloperKey,
    hasLocalDeveloperKey,
    loadingModels,
    models,
    selectedModel,
    setSelectedModel,
    selectedModelInfo,
    addHistory,
    history,
    videoReference,
    setVideoReference,
    reloadApiRecords,
}) {
    const [customModel, setCustomModel] = useState('')
    const [prompt, setPrompt] = useState('')
    const [ratio, setRatio] = useState('16:9')
    const [task, setTask] = useState(null)
    const [videoUrl, setVideoUrl] = useState('')
    const [loading, setLoading] = useState(false)
    const [error, setError] = useState('')
    const abortRef = useRef(null)
    const modelId = customModel.trim() || selectedModel

    const pollVideo = async (job, signal) => {
        let current = job
        for (let attempt = 0; attempt < 120; attempt += 1) {
            setTask(current)
            if (TERMINAL_STATUSES.has(current.status)) return current
            await sleep(5000, signal)
            current = await getVideoGeneration(effectiveApiKey, current.id || current.task_id || current.job_id, { signal })
        }
        return current
    }

    const handleGenerate = async () => {
        if (!prompt.trim() || !modelId || loading) return
        if (!effectiveApiKey) {
            setError('当前没有可用的开发者 API Key。')
            return
        }
        if (!canUseAsRemoteReference(videoReference)) {
            setError('Seedance 需要可访问的图片 URL。')
            return
        }

        const controller = new AbortController()
        abortRef.current = controller
        setLoading(true)
        setError('')
        setVideoUrl('')
        setTask({ status: 'queued', model: modelId })

        try {
            const created = await createVideoGeneration(effectiveApiKey, {
                model: modelId,
                prompt: prompt.trim(),
                params: {
                    ratio,
                    images: [videoReference.trim()],
                },
            }, { signal: controller.signal })
            const finalTask = await pollVideo(created, controller.signal)
            setTask(finalTask)
            if (finalTask.status === 'failed') {
                throw new Error(finalTask.error?.message || '视频任务失败')
            }
            const url = extractVideoUrl(finalTask)
            if (!url) throw new Error('视频任务完成但没有返回视频 URL')
            setVideoUrl(url)
            addHistory([{
                id: finalTask.id || nowId('video'),
                type: 'video',
                url,
                model: modelId,
                prompt: prompt.trim(),
                status: finalTask.status,
                costCents: finalTask.charged_cents,
                createdAt: finalTask.created_at || new Date().toISOString(),
            }])
            reloadApiRecords()
        } catch (err) {
            if (err.name !== 'AbortError') setError(extractErrorMessage(err))
        } finally {
            setLoading(false)
            abortRef.current = null
        }
    }

    const latestImageUrl = history.find((item) => item.type === 'image' && canUseAsRemoteReference(item.url))?.url || ''
    const latestVideo = history.find((item) => item.type === 'video' && item.url)

    return (
        <div className="wb-layout">
            <aside className="wb-rail">
                <KeyNotice authMode={authMode} hasDeveloperKey={hasDeveloperKey} hasLocalDeveloperKey={hasLocalDeveloperKey} />
                <ModelSelect label="推荐模型" models={models} value={selectedModel} onChange={setSelectedModel} disabled={loadingModels || loading} />
                <label className="wb-field">
                    <span>自定义模型</span>
                    <input value={customModel} onChange={(event) => setCustomModel(event.target.value)} placeholder="留空使用推荐模型" />
                </label>
                {selectedModelInfo ? (
                    <div className="wb-model-note">
                        <span>{formatModelPrice(selectedModelInfo)}</span>
                        <small>{describePublicModel(selectedModelInfo)}</small>
                    </div>
                ) : null}
                <label className="wb-field">
                    <span>画面比例</span>
                    <select value={ratio} onChange={(event) => setRatio(event.target.value)}>
                        {VIDEO_RATIOS.map((option) => <option key={option} value={option}>{option}</option>)}
                    </select>
                </label>
                <label className="wb-field">
                    <span>参考图 URL</span>
                    <input value={videoReference} onChange={(event) => setVideoReference(event.target.value)} placeholder="https://..." />
                </label>
                {latestImageUrl ? (
                    <button className="btn btn-secondary btn-sm wb-wide-btn" type="button" onClick={() => setVideoReference(latestImageUrl)}>
                        引用最近图片
                    </button>
                ) : null}
            </aside>

            <section className="wb-stage">
                <div className="wb-result-surface wb-video-surface">
                    <div className="wb-stage-head">
                        <span>视频结果</span>
                        <small>{task?.status || ''}</small>
                    </div>
                    {error ? <div className="wb-error">{error}</div> : null}
                    {loading ? (
                        <div className="wb-empty-state"><div className="loading-spinner wb-spinner-lg" />任务生成中</div>
                    ) : videoUrl ? (
                        <div className="wb-video-result">
                            <video src={videoUrl} controls playsInline />
                            <div className="wb-video-actions">
                                <button type="button" onClick={() => downloadMedia(videoUrl, 'coincoin-seedance.mp4')}>下载视频</button>
                                {task?.charged_cents ? <span>{formatCost(task.charged_cents)}</span> : null}
                            </div>
                        </div>
                    ) : latestVideo ? (
                        <div className="wb-video-result">
                            <video src={latestVideo.url} controls playsInline />
                            <div className="wb-video-actions">
                                <button type="button" onClick={() => downloadMedia(latestVideo.url, 'coincoin-seedance.mp4')}>下载视频</button>
                                <span>最近结果</span>
                            </div>
                        </div>
                    ) : (
                        <div className="wb-empty-state">视频会显示在这里</div>
                    )}
                    {task ? (
                        <div className="wb-task-meta">
                            <span>{task.id || task.task_id || '-'}</span>
                            <span>{task.upstream_task_id || ''}</span>
                        </div>
                    ) : null}
                </div>
                <div className="wb-composer">
                    <textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="描述视频动作和镜头..." rows="4" />
                    <div className="wb-composer-actions">
                        {loading ? (
                            <button className="btn btn-secondary" onClick={() => abortRef.current?.abort()}>停止轮询</button>
                        ) : (
                            <button className="btn btn-primary" onClick={handleGenerate} disabled={!prompt.trim() || !modelId || !effectiveApiKey || !canUseAsRemoteReference(videoReference)}>
                                生成视频
                            </button>
                        )}
                    </div>
                </div>
            </section>
        </div>
    )
}

function MediaHistory({ history, activeTab, setVideoReference }) {
    const filtered = history.filter((item) => activeTab === 'video' ? item.type === 'video' : item.type === 'image')
    return (
        <div className="wb-history">
            <div className="wb-history-head">
                <span>网页历史</span>
                <small>{filtered.length} 条</small>
            </div>
            {filtered.length === 0 ? (
                <div className="wb-empty-line">暂无结果</div>
            ) : (
                <div className="wb-history-strip">
                    {filtered.slice(0, 10).map((item) => (
                        <div key={item.id} className="wb-history-item">
                            {item.type === 'video' ? (
                                <video src={item.url} muted playsInline />
                            ) : item.url ? (
                                <img src={item.url} alt={item.prompt || item.model} />
                            ) : (
                                <div className="wb-history-missing">{item.cachedMedia ? '加载中' : '已失效'}</div>
                            )}
                            <div>
                                <strong>{item.model}</strong>
                                <span>{item.createdAt ? formatLocalTime(item.createdAt) : ''}</span>
                            </div>
                            <div className="wb-history-actions">
                                {item.url ? <button type="button" onClick={() => downloadMedia(item.url, item.type === 'video' ? 'coincoin-video.mp4' : 'coincoin-image.png')}>下载</button> : null}
                                {item.type === 'image' && canUseAsRemoteReference(item.url) ? (
                                    <button type="button" onClick={() => setVideoReference(item.url)}>视频参考</button>
                                ) : null}
                            </div>
                        </div>
                    ))}
                </div>
            )}
        </div>
    )
}

export default function Playground() {
    const { authMode, effectiveApiKey, hasDeveloperKey, hasLocalDeveloperKey } = useAuth()
    const {
        textModels,
        imageModels,
        videoModels,
        defaultTextModel,
        defaultImageModel,
        defaultVideoModel,
        loading: loadingModels,
    } = usePublicModels()
    const [activeTab, setActiveTab] = useState(() => safeReadActiveTab())
    const [selectedTextModel, setSelectedTextModel] = useState('')
    const [selectedImageModel, setSelectedImageModel] = useState('')
    const [selectedVideoModel, setSelectedVideoModel] = useState('')
    const [history, setHistory] = useState(() => safeReadHistory())
    const [videoReference, setVideoReference] = useState('')
    const [apiMediaRecords, setApiMediaRecords] = useState([])
    const [apiRecordsLoading, setApiRecordsLoading] = useState(false)

    useEffect(() => {
        if ((!selectedTextModel || !textModels.find((model) => model.id === selectedTextModel)) && defaultTextModel?.id) {
            setSelectedTextModel(defaultTextModel.id)
        }
    }, [defaultTextModel, selectedTextModel, textModels])

    useEffect(() => {
        if ((!selectedImageModel || !imageModels.find((model) => model.id === selectedImageModel)) && defaultImageModel?.id) {
            setSelectedImageModel(defaultImageModel.id)
        }
    }, [defaultImageModel, imageModels, selectedImageModel])

    useEffect(() => {
        if ((!selectedVideoModel || !videoModels.find((model) => model.id === selectedVideoModel)) && defaultVideoModel?.id) {
            setSelectedVideoModel(defaultVideoModel.id)
        }
    }, [defaultVideoModel, selectedVideoModel, videoModels])

    useEffect(() => {
        safeStoreHistory(history)
    }, [history])

    useEffect(() => {
        let active = true
        restoreCachedHistory(history).then((restored) => {
            if (active && restored !== history) setHistory(restored)
        })
        return () => {
            active = false
        }
    }, [history])

    useEffect(() => {
        safeStoreActiveTab(activeTab)
    }, [activeTab])

    const selectedTextModelInfo = useMemo(
        () => textModels.find((model) => model.id === selectedTextModel) || defaultTextModel,
        [defaultTextModel, selectedTextModel, textModels]
    )
    const selectedImageModelInfo = useMemo(
        () => imageModels.find((model) => model.id === selectedImageModel) || defaultImageModel,
        [defaultImageModel, imageModels, selectedImageModel]
    )
    const selectedVideoModelInfo = useMemo(
        () => videoModels.find((model) => model.id === selectedVideoModel) || defaultVideoModel,
        [defaultVideoModel, selectedVideoModel, videoModels]
    )

    const addHistory = useCallback((items) => {
        setHistory((current) => {
            const next = [...items, ...current]
            const seen = new Set()
            return next.filter((item) => {
                const key = `${item.type}:${item.url || item.id}`
                if (seen.has(key)) return false
                seen.add(key)
                return true
            }).slice(0, 30)
        })
    }, [])

    const reloadApiRecords = useCallback(async () => {
        setApiRecordsLoading(true)
        try {
            const media = await getMediaArtifacts(48, 0).catch(() => ({ data: [] }))
            setApiMediaRecords(Array.isArray(media.data) ? media.data : [])
        } finally {
            setApiRecordsLoading(false)
        }
    }, [])

    const syncedImageRunRef = useRef('')

    useEffect(() => subscribeImageRunState((state) => {
        if (state.loading || !state.resultImages?.length || syncedImageRunRef.current === state.runId) return
        syncedImageRunRef.current = state.runId
        setHistory((current) => dedupeHistory([...state.resultImages, ...current]))
        reloadApiRecords()
    }), [reloadApiRecords])

    useEffect(() => {
        reloadApiRecords()
    }, [reloadApiRecords])

    const tabs = [
        { key: 'chat', label: '对话' },
        { key: 'image', label: '图片' },
        { key: 'video', label: '视频' },
    ]

    return (
        <AppShell title="工作台">
            <div className="workbench">
                <div className="wb-topbar">
                    <div className="wb-tabs" role="tablist" aria-label="工作台类型">
                        {tabs.map((tab) => (
                            <button
                                key={tab.key}
                                type="button"
                                role="tab"
                                aria-selected={activeTab === tab.key}
                                className={activeTab === tab.key ? 'active' : ''}
                                onClick={() => setActiveTab(tab.key)}
                            >
                                {tab.label}
                            </button>
                        ))}
                    </div>
                    <div className="wb-top-actions">
                        <Link to="/docs?tab=api" className="btn btn-ghost btn-sm">API</Link>
                        <Link to="/recharge" className="btn btn-secondary btn-sm">充值</Link>
                    </div>
                </div>

                {activeTab === 'chat' ? (
                    <ChatWorkspace
                        authMode={authMode}
                        effectiveApiKey={effectiveApiKey}
                        hasDeveloperKey={hasDeveloperKey}
                        hasLocalDeveloperKey={hasLocalDeveloperKey}
                        loadingModels={loadingModels}
                        models={textModels}
                        selectedModel={selectedTextModel}
                        setSelectedModel={setSelectedTextModel}
                        selectedModelInfo={selectedTextModelInfo}
                    />
                ) : null}

                {activeTab === 'image' ? (
                    <>
                        <ImageWorkspace
                            authMode={authMode}
                            effectiveApiKey={effectiveApiKey}
                            hasDeveloperKey={hasDeveloperKey}
                            hasLocalDeveloperKey={hasLocalDeveloperKey}
                            loadingModels={loadingModels}
                            models={imageModels}
                            selectedModel={selectedImageModel}
                            setSelectedModel={setSelectedImageModel}
                            selectedModelInfo={selectedImageModelInfo}
                            history={history}
                            setVideoReference={(url) => {
                                setVideoReference(url)
                                setActiveTab('video')
                            }}
                        />
                        <div className="wb-bottom-panels">
                            <MediaHistory history={history} activeTab="image" setVideoReference={(url) => {
                                setVideoReference(url)
                                setActiveTab('video')
                            }} />
                            <ApiMediaRecords records={apiMediaRecords} loading={apiRecordsLoading} activeTab="image" setVideoReference={(url) => {
                                setVideoReference(url)
                                setActiveTab('video')
                            }} />
                        </div>
                    </>
                ) : null}

                {activeTab === 'video' ? (
                    <>
                        <VideoWorkspace
                            authMode={authMode}
                            effectiveApiKey={effectiveApiKey}
                            hasDeveloperKey={hasDeveloperKey}
                            hasLocalDeveloperKey={hasLocalDeveloperKey}
                            loadingModels={loadingModels}
                            models={videoModels}
                            selectedModel={selectedVideoModel}
                            setSelectedModel={setSelectedVideoModel}
                            selectedModelInfo={selectedVideoModelInfo}
                            addHistory={addHistory}
                            history={history}
                            videoReference={videoReference}
                            setVideoReference={setVideoReference}
                            reloadApiRecords={reloadApiRecords}
                        />
                        <div className="wb-bottom-panels">
                            <MediaHistory history={history} activeTab="video" setVideoReference={setVideoReference} />
                            <ApiMediaRecords records={apiMediaRecords} loading={apiRecordsLoading} activeTab="video" setVideoReference={setVideoReference} />
                        </div>
                    </>
                ) : null}
            </div>
        </AppShell>
    )
}
