import { useState, useEffect, useCallback, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import { getUsageLogs, listDeveloperKeys } from '../api/client'
import AppShell from '../components/AppShell'
import { formatLocalTime, getLocalDateRangeIso, getLocalIsoDate } from '../utils/time'
import './Usage.css'

const RANGE_OPTIONS = [
    { key: 'today', label: '今天', days: 1 },
    { key: 'yesterday', label: '昨天', preset: 'yesterday' },
    { key: '7d', label: '近 7 天', days: 7 },
    { key: 'month', label: '本月迄今', preset: 'month' },
]

function formatCacheHitRate(cachedTokens, inputTokens) {
    if (!inputTokens) return '0%'
    return `${((cachedTokens / inputTokens) * 100).toFixed(1)}%`
}

function getCacheReadTokens(log) {
    return log.cache_read_tokens ?? log.cached_tokens ?? 0
}

function getCacheCreationTokens(log) {
    return log.cache_creation_tokens ?? 0
}

function getTotalInputForCache(log) {
    const input = Number(log.input_tokens || 0)
    const cacheRead = getCacheReadTokens(log)
    const cacheCreation = getCacheCreationTokens(log)
    return input >= cacheRead + cacheCreation ? input : input + cacheRead + cacheCreation
}

function getRegularInputTokens(log) {
    const input = Number(log.input_tokens || 0)
    const cacheRead = getCacheReadTokens(log)
    const cacheCreation = getCacheCreationTokens(log)
    return input >= cacheRead + cacheCreation
        ? Math.max(0, input - cacheRead - cacheCreation)
        : input
}

function formatDurationMs(durationMs) {
    const ms = Number(durationMs || 0)
    if (ms < 1000) return `${Math.max(0, Math.round(ms))}ms`
    if (ms < 10_000) return `${(ms / 1000).toFixed(2)}s`
    return `${(ms / 1000).toFixed(1)}s`
}

function getDateRange(option) {
    if (!option) return { start_date: '', end_date: '' }
    const now = new Date()
    const start = new Date(now)
    const end = new Date(now)

    if (option.preset === 'yesterday') {
        start.setDate(start.getDate() - 1)
        end.setDate(end.getDate() - 1)
    } else if (option.preset === 'month') {
        start.setDate(1)
    } else {
        start.setDate(start.getDate() - Math.max(1, Number(option.days || 1)) + 1)
    }

    return {
        start_date: getLocalIsoDate(start),
        end_date: getLocalIsoDate(end),
    }
}

function buildUsageFilterParams(filters) {
    const params = new URLSearchParams()
    if (filters.endpoint) params.set('endpoint', filters.endpoint)
    if (filters.status_code) params.set('status_code', filters.status_code)
    if (filters.api_key_id) params.set('api_key_id', filters.api_key_id)

    const startRange = filters.start_date ? getLocalDateRangeIso(filters.start_date) : null
    const endRange = filters.end_date ? getLocalDateRangeIso(filters.end_date) : null
    if (startRange) params.set('start_date', startRange.start)
    if (endRange) {
        params.set('end_date', endRange.end)
        params.set('end_exclusive', 'true')
    }

    return params
}

export default function Usage() {
    const [searchParams, setSearchParams] = useSearchParams()
    const requestedRangeKey = searchParams.get('range') || 'today'
    const initialRangeKey = requestedRangeKey === 'custom' || RANGE_OPTIONS.some(option => option.key === requestedRangeKey)
        ? requestedRangeKey
        : 'today'
    const initialRangeOption = RANGE_OPTIONS.find(option => option.key === initialRangeKey) || RANGE_OPTIONS[0]
    const initialDateRange = initialRangeKey === 'custom'
        ? {
            start_date: searchParams.get('start_date') || getDateRange(RANGE_OPTIONS[0]).start_date,
            end_date: searchParams.get('end_date') || getDateRange(RANGE_OPTIONS[0]).end_date,
        }
        : getDateRange(initialRangeOption)
    const [usage, setUsage] = useState(null)
    const [loadError, setLoadError] = useState('')
    const [keysState, setKeysState] = useState({ data: [] })
    const [rangeOpen, setRangeOpen] = useState(false)
    const rangePickerRef = useRef(null)
    const [customRange, setCustomRange] = useState({
        start_date: initialRangeKey === 'custom' ? initialDateRange.start_date : searchParams.get('start_date') || initialDateRange.start_date,
        end_date: initialRangeKey === 'custom' ? initialDateRange.end_date : searchParams.get('end_date') || initialDateRange.end_date,
    })
    const [page, setPage] = useState(0)
    const [filters, setFilters] = useState({
        endpoint: '',
        status_code: '',
        api_key_id: searchParams.get('api_key_id') || '',
        start_date: initialDateRange.start_date,
        end_date: initialDateRange.end_date,
        range_key: initialRangeKey,
    })
    const limit = 15

    const load = useCallback(async () => {
        try {
            const queryFilters = {}
            buildUsageFilterParams(filters).forEach((value, key) => { queryFilters[key] = value })
            setUsage(await getUsageLogs(limit, page * limit, queryFilters))
            setLoadError('')
        } catch (err) {
            setUsage(null)
            setLoadError(err.message || '请求日志加载失败，请重新登录后再试。')
        }
    }, [page, filters])

    useEffect(() => { load() }, [load])

    useEffect(() => {
        let cancelled = false
        listDeveloperKeys()
            .then((data) => {
                if (!cancelled) setKeysState(data)
            })
            .catch(() => {
                if (!cancelled) setKeysState({ data: [] })
            })
        return () => { cancelled = true }
    }, [])

    useEffect(() => {
        if (!rangeOpen) return undefined

        const closeOnOutside = (event) => {
            if (!rangePickerRef.current?.contains(event.target)) {
                setRangeOpen(false)
            }
        }

        const closeOnEscape = (event) => {
            if (event.key === 'Escape') setRangeOpen(false)
        }

        document.addEventListener('mousedown', closeOnOutside)
        document.addEventListener('keydown', closeOnEscape)
        return () => {
            document.removeEventListener('mousedown', closeOnOutside)
            document.removeEventListener('keydown', closeOnEscape)
        }
    }, [rangeOpen])

    const exportCSV = () => {
        if (!usage?.data?.length) return
        const headers = ['时间', '端点', '模型', '计量类型', '计量值', 'Input Token', '缓存读取 Token', '缓存写入 Token', '普通输入 Token', 'Output Token', '总 Token', '缓存读取占比', '花费($)', '耗时(ms)', '状态码']
        const rows = usage.data.map(d => [
            d.created_at, d.endpoint, d.model,
            d.usage_unit_type, d.usage_unit_type === 'images' ? (d.image_count || d.usage_unit_count) : d.usage_unit_count,
            d.input_tokens, getCacheReadTokens(d), getCacheCreationTokens(d), getRegularInputTokens(d), d.output_tokens, d.total_tokens,
            formatCacheHitRate(getCacheReadTokens(d), getTotalInputForCache(d)),
            d.cost_usd.toFixed(4), d.duration_ms, d.status_code
        ])
        const csv = [headers, ...rows].map(r => r.join(',')).join('\n')
        const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8' })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = `coincoin_usage_${new Date().toISOString().slice(0, 10)}.csv`
        a.click()
        URL.revokeObjectURL(url)
    }

    const applyFilter = (key, value) => {
        setFilters(f => ({ ...f, [key]: value }))
        if (['api_key_id', 'start_date', 'end_date', 'range_key'].includes(key)) {
            const next = new URLSearchParams(searchParams)
            if (key === 'range_key') {
                if (value && value !== 'today') next.set('range', value)
                else next.delete('range')
            } else if (value) {
                next.set(key, value)
            } else {
                next.delete(key)
            }
            setSearchParams(next, { replace: true })
        }
        setPage(0)
    }

    const applyDateRange = (option) => {
        const range = getDateRange(option)
        setFilters(f => ({
            ...f,
            start_date: range.start_date,
            end_date: range.end_date,
            range_key: option.key,
        }))
        setCustomRange(range)
        const next = new URLSearchParams(searchParams)
        if (option.key === 'today') next.delete('range')
        else next.set('range', option.key)
        next.delete('start_date')
        next.delete('end_date')
        setSearchParams(next, { replace: true })
        setPage(0)
        setRangeOpen(false)
    }

    const applyCustomRange = () => {
        const nextFilters = { ...filters, ...customRange, range_key: 'custom' }
        setFilters(nextFilters)
        const next = new URLSearchParams(searchParams)
        next.set('range', 'custom')
        if (nextFilters.start_date) next.set('start_date', nextFilters.start_date)
        else next.delete('start_date')
        if (nextFilters.end_date) next.set('end_date', nextFilters.end_date)
        else next.delete('end_date')
        setSearchParams(next, { replace: true })
        setPage(0)
        setRangeOpen(false)
    }

    const selectedKey = (keysState.data || []).find(key => key.key_id === filters.api_key_id)

    if (!usage) {
        return (
            <AppShell title="请求日志" description="看每次请求的模型、计量、状态码和花费。">
                {loadError ? (
                    <div className="loading-state error-state">
                        <h3>需要重新登录</h3>
                        <p>{loadError}</p>
                    </div>
                ) : (
                    <div className="loading-state"><div className="loading-spinner"></div><p>加载中...</p></div>
                )}
            </AppShell>
        )
    }

    const summary = usage.summary || null
    const totalCost = summary ? summary.cost_usd : usage.data.reduce((s, d) => s + d.cost_usd, 0)
    const totalTokens = summary ? summary.total_tokens : usage.data.reduce((s, d) => s + d.total_tokens, 0)
    const totalImages = summary ? summary.image_count : usage.data.reduce((s, d) => s + (d.image_count || 0), 0)
    const totalCacheReadTokens = summary ? (summary.cache_read_tokens ?? summary.cached_tokens ?? 0) : usage.data.reduce((s, d) => s + getCacheReadTokens(d), 0)
    const totalCacheCreationTokens = summary ? (summary.cache_creation_tokens ?? 0) : usage.data.reduce((s, d) => s + getCacheCreationTokens(d), 0)
    const totalInputTokens = summary ? summary.input_tokens : usage.data.reduce((s, d) => s + getTotalInputForCache(d), 0)
    const totalInputForCache = Math.max(totalInputTokens, totalCacheReadTokens + totalCacheCreationTokens)

    return (
        <AppShell
            title="请求日志"
            description="看每次请求的模型、缓存读取、缓存写入、计量和花费。"
            actions={<button className="btn btn-secondary btn-sm" onClick={exportCSV}>导出 CSV</button>}
        >
            <div className="usage-page">

                <div className="stats-grid stagger-children">
                    <div className="stat-card glass-card animate-fade-in-up">
                        <div className="stat-icon" style={{ background: 'rgba(99,102,241,0.12)' }}>
                            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="var(--accent-indigo)" strokeWidth="2"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" /><circle cx="9" cy="7" r="4" /><path d="M22 21v-2a4 4 0 0 0-3-3.87" /><path d="M16 3.13a4 4 0 0 1 0 7.75" /></svg>
                        </div>
                        <div className="stat-info">
                            <span className="stat-label">总请求</span>
                            <span className="stat-value">{usage.total}</span>
                        </div>
                    </div>
                    <div className="stat-card glass-card animate-fade-in-up">
                        <div className="stat-icon" style={{ background: 'rgba(16,185,129,0.12)' }}>
                            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="var(--accent-emerald)" strokeWidth="2"><path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6" /></svg>
                        </div>
                        <div className="stat-info">
                            <span className="stat-label">当前筛选花费</span>
                            <span className="stat-value">${totalCost.toFixed(2)}</span>
                        </div>
                    </div>
                    <div className="stat-card glass-card animate-fade-in-up">
                        <div className="stat-icon" style={{ background: 'rgba(6,182,212,0.12)' }}>
                            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="var(--accent-cyan)" strokeWidth="2"><path d="M22 12h-4l-3 9L9 3l-3 9H2" /></svg>
                        </div>
                        <div className="stat-info">
                            <span className="stat-label">当前筛选 Token</span>
                            <span className="stat-value">{totalTokens.toLocaleString()}</span>
                        </div>
                    </div>
                    <div className="stat-card glass-card animate-fade-in-up">
                        <div className="stat-icon" style={{ background: 'rgba(245,158,11,0.12)' }}>
                            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="var(--accent-amber)" strokeWidth="2"><path d="M4 7h16M4 17h16M7 4v16M17 4v16" /></svg>
                        </div>
                        <div className="stat-info">
                            <span className="stat-label">当前筛选图片</span>
                            <span className="stat-value">{totalImages}</span>
                        </div>
                    </div>
                    <div className="stat-card glass-card animate-fade-in-up">
                        <div className="stat-icon" style={{ background: 'rgba(244,114,182,0.12)' }}>
                            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#ec4899" strokeWidth="2"><path d="M12 3l7 4v10l-7 4-7-4V7l7-4z" /><path d="M9 12h6" /></svg>
                        </div>
                        <div className="stat-info">
                            <span className="stat-label">当前筛选缓存读取</span>
                            <span className="stat-value">{totalCacheReadTokens.toLocaleString()}</span>
                            <span className="stat-subvalue">
                                写入 {totalCacheCreationTokens.toLocaleString()} · {formatCacheHitRate(totalCacheReadTokens, totalInputForCache)} 读取占比
                            </span>
                        </div>
                    </div>
                </div>

                {/* Filters */}
                <div className="usage-filters glass-card animate-fade-in-up">
                    <div className="usage-filters-header">
                        <div>
                            <h3>筛选与导出</h3>
                            <p>默认显示今天。需要排查趋势时，切换时间范围即可。</p>
                        </div>
                    </div>
                    <div className="filter-row">
                        <select className="filter-select" value={filters.endpoint} onChange={e => applyFilter('endpoint', e.target.value)}>
                            <option value="">全部端点</option>
                            <option value="responses">responses</option>
                            <option value="responses:stream">responses:stream</option>
                            <option value="chat/completions">chat/completions</option>
                            <option value="chat/completions:stream">chat/completions:stream</option>
                            <option value="images/generations">images/generations</option>
                            <option value="embeddings">embeddings</option>
                        </select>
                        <select className="filter-select" value={filters.status_code} onChange={e => applyFilter('status_code', e.target.value)}>
                            <option value="">全部状态</option>
                            <option value="200">200 成功</option>
                            <option value="400">400 错误</option>
                            <option value="429">429 限流</option>
                            <option value="500">500 服务器错误</option>
                        </select>
                        <select className="filter-select filter-select-key" value={filters.api_key_id} onChange={e => applyFilter('api_key_id', e.target.value)}>
                            <option value="">全部 API Key</option>
                            {(keysState.data || []).map(key => (
                                <option key={key.key_id} value={key.key_id}>
                                    {key.masked_key} · {key.status === 'active' ? '可用' : '已禁用'}
                                </option>
                            ))}
                            {filters.api_key_id && !selectedKey && (
                                <option value={filters.api_key_id}>当前选定 Key</option>
                            )}
                        </select>
                        <div className="range-picker range-picker-segmented" ref={rangePickerRef}>
                            <div className="range-segmented" role="group" aria-label="时间范围">
                                {RANGE_OPTIONS.map(option => (
                                    <button
                                        key={option.key}
                                        type="button"
                                        className={`range-segment ${filters.range_key === option.key ? 'active' : ''}`}
                                        onClick={() => applyDateRange(option)}
                                    >
                                        {option.label}
                                    </button>
                                ))}
                                <button
                                    type="button"
                                    className={`range-segment ${filters.range_key === 'custom' || rangeOpen ? 'active' : ''}`}
                                    onClick={() => setRangeOpen(open => !open)}
                                    aria-expanded={rangeOpen}
                                    aria-haspopup="dialog"
                                >
                                    自定义
                                </button>
                            </div>
                            {rangeOpen && (
                                <div className="range-popover" role="dialog" aria-label="选择时间范围">
                                    <div className="range-custom">
                                        <label className="date-filter-field">
                                            <span>开始日期</span>
                                            <input type="date" className="filter-input" value={customRange.start_date} onChange={e => setCustomRange(range => ({ ...range, start_date: e.target.value }))} />
                                        </label>
                                        <span className="range-custom-arrow">至</span>
                                        <label className="date-filter-field">
                                            <span>结束日期</span>
                                            <input type="date" className="filter-input" value={customRange.end_date} onChange={e => setCustomRange(range => ({ ...range, end_date: e.target.value }))} />
                                        </label>
                                        <button className="btn btn-primary btn-sm" type="button" onClick={applyCustomRange}>应用</button>
                                    </div>
                                </div>
                            )}
                        </div>
                        <button className="btn btn-secondary btn-sm" onClick={exportCSV}>&#128190; 导出 CSV</button>
                    </div>
                </div>

                <div className="usage-table glass-card animate-fade-in-up">
                    <div className="table-wrapper">
                        <table className="data-table">
                            <thead>
                                <tr>
                                    <th>请求时间</th>
                                    <th>端点</th>
                                    <th>模型</th>
                                    <th>计量</th>
                                    <th>Input Token</th>
                                    <th>缓存读取</th>
                                    <th>缓存写入</th>
                                    <th>普通输入</th>
                                    <th>Output Token</th>
                                    <th>读取占比</th>
                                    <th>总 Token</th>
                                    <th>花费</th>
                                    <th>耗时</th>
                                    <th>状态</th>
                                </tr>
                            </thead>
                            <tbody>
                                {usage.data.map((log, i) => (
                                    <tr key={i}>
                                        <td>{formatLocalTime(log.created_at)}</td>
                                        <td><code className="endpoint-tag">{log.endpoint}</code></td>
                                        <td><span className="model-tag-sm">{log.model}</span></td>
                                        <td>
                                            <span className={`usage-pill ${log.usage_unit_type === 'images' ? 'images' : 'tokens'}`}>
                                                {log.usage_unit_type === 'images'
                                                    ? `${log.image_count || log.usage_unit_count || 0} images`
                                                    : `${(log.usage_unit_count || log.total_tokens || 0).toLocaleString()} tokens`}
                                            </span>
                                        </td>
                                        <td>{log.input_tokens.toLocaleString()}</td>
                                        <td>{getCacheReadTokens(log).toLocaleString()}</td>
                                        <td>{getCacheCreationTokens(log).toLocaleString()}</td>
                                        <td>{getRegularInputTokens(log).toLocaleString()}</td>
                                        <td>{log.output_tokens.toLocaleString()}</td>
                                        <td>
                                            {log.usage_unit_type === 'images'
                                                ? '-'
                                                : formatCacheHitRate(getCacheReadTokens(log), getTotalInputForCache(log))}
                                        </td>
                                        <td><strong>{log.total_tokens.toLocaleString()}</strong></td>
                                        <td className="cost-cell">${log.cost_usd.toFixed(2)}</td>
                                        <td>{formatDurationMs(log.duration_ms)}</td>
                                        <td><span className={`badge ${log.status_code === 200 ? 'badge-success' : 'badge-error'}`}>{log.status_code}</span></td>
                                    </tr>
                                ))}
                                {usage.data.length === 0 && (
                                    <tr><td colSpan="14" style={{ textAlign: 'center', padding: '2rem', color: 'var(--text-tertiary)' }}>暂无数据</td></tr>
                                )}
                            </tbody>
                        </table>
                    </div>
                    <div className="table-footer">
                        <span className="table-info">共 {usage.total} 条记录</span>
                        <div className="table-pagination">
                            <button className="btn btn-ghost btn-sm" onClick={() => setPage(p => Math.max(0, p - 1))} disabled={page === 0}>
                                &larr; 上一页
                            </button>
                            <span className="page-indicator">第 {page + 1} 页</span>
                            <button className="btn btn-ghost btn-sm" onClick={() => setPage(p => p + 1)} disabled={usage.data.length < limit}>
                                下一页 &rarr;
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        </AppShell>
    )
}
