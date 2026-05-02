import { useState, useEffect, useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import { MOCK_USAGE, getApiKey, listDeveloperKeys } from '../api/client'
import AppShell from '../components/AppShell'
import './Usage.css'

function formatCacheHitRate(cachedTokens, inputTokens) {
    if (!inputTokens) return '0%'
    return `${((cachedTokens / inputTokens) * 100).toFixed(1)}%`
}

export default function Usage() {
    const [searchParams, setSearchParams] = useSearchParams()
    const [usage, setUsage] = useState(null)
    const [keysState, setKeysState] = useState({ data: [] })
    const [page, setPage] = useState(0)
    const [filters, setFilters] = useState({
        endpoint: '',
        status_code: '',
        api_key_id: searchParams.get('api_key_id') || '',
        start_date: '',
        end_date: '',
    })
    const limit = 15

    const load = useCallback(async () => {
        try {
            const params = new URLSearchParams({ limit, offset: page * limit })
            if (filters.endpoint) params.set('endpoint', filters.endpoint)
            if (filters.status_code) params.set('status_code', filters.status_code)
            if (filters.api_key_id) params.set('api_key_id', filters.api_key_id)
            if (filters.start_date) params.set('start_date', filters.start_date)
            if (filters.end_date) params.set('end_date', filters.end_date)

            const res = await fetch(`/v1/usage?${params}`, {
                headers: { 'Authorization': `Bearer ${getApiKey()}` }
            })
            if (!res.ok) throw new Error()
            setUsage(await res.json())
        } catch {
            setUsage(MOCK_USAGE)
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

    const exportCSV = () => {
        if (!usage?.data?.length) return
        const headers = ['时间', '端点', '模型', '计量类型', '计量值', 'Input Token', '缓存输入 Token', '非缓存输入 Token', 'Output Token', '总 Token', '缓存命中率', '花费($)', '耗时(ms)', '状态码']
        const rows = usage.data.map(d => [
            d.created_at, d.endpoint, d.model,
            d.usage_unit_type, d.usage_unit_type === 'images' ? (d.image_count || d.usage_unit_count) : d.usage_unit_count,
            d.input_tokens, d.cached_tokens || 0, Math.max(0, (d.input_tokens || 0) - (d.cached_tokens || 0)), d.output_tokens, d.total_tokens,
            formatCacheHitRate(d.cached_tokens || 0, d.input_tokens || 0),
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
        if (key === 'api_key_id') {
            const next = new URLSearchParams(searchParams)
            if (value) next.set('api_key_id', value)
            else next.delete('api_key_id')
            setSearchParams(next, { replace: true })
        }
        setPage(0)
    }

    const selectedKey = (keysState.data || []).find(key => key.key_id === filters.api_key_id)

    if (!usage) {
        return (
            <AppShell title="请求日志" description="看每次请求的模型、计量、状态码和花费。">
                <div className="loading-state"><div className="loading-spinner"></div><p>加载中...</p></div>
            </AppShell>
        )
    }

    const totalCost = usage.data.reduce((s, d) => s + d.cost_usd, 0)
    const totalTokens = usage.data.reduce((s, d) => s + d.total_tokens, 0)
    const totalImages = usage.data.reduce((s, d) => s + (d.image_count || 0), 0)
    const totalCachedTokens = usage.data.reduce((s, d) => s + (d.cached_tokens || 0), 0)
    const totalInputTokens = usage.data.reduce((s, d) => s + (d.input_tokens || 0), 0)

    return (
        <AppShell
            title="请求日志"
            description="看每次请求的模型、缓存命中、计量和花费。"
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
                            <span className="stat-label">当前筛选缓存输入</span>
                            <span className="stat-value">{totalCachedTokens.toLocaleString()}</span>
                            <span className="stat-subvalue">{formatCacheHitRate(totalCachedTokens, totalInputTokens)} 命中率</span>
                        </div>
                    </div>
                </div>

                <div className="usage-guide glass-card animate-fade-in-up">
                    <div className="usage-guide-copy">
                        <span className="usage-kicker">Request Logs</span>
                        <p>排查模型路由、缓存命中、计量和状态码时，先看这里。</p>
                    </div>
                    <div className="usage-guide-pills">
                        <div className="usage-guide-pill">
                            <strong>路由</strong>
                            <span>endpoint / model</span>
                        </div>
                        <div className="usage-guide-pill">
                            <strong>缓存</strong>
                            <span>cached / non-cached / hit rate</span>
                        </div>
                        <div className="usage-guide-pill">
                            <strong>计量</strong>
                            <span>tokens / images / cost</span>
                        </div>
                    </div>
                </div>

                {/* Filters */}
                <div className="usage-filters glass-card animate-fade-in-up">
                    <div className="usage-filters-header">
                        <div>
                            <h3>筛选与导出</h3>
                            <p>先缩小范围，再导出 CSV。看问题会快很多。</p>
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
                        <input type="date" className="filter-input" value={filters.start_date} onChange={e => applyFilter('start_date', e.target.value)} placeholder="开始日期" />
                        <input type="date" className="filter-input" value={filters.end_date} onChange={e => applyFilter('end_date', e.target.value)} placeholder="结束日期" />
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
                                    <th>缓存输入</th>
                                    <th>非缓存输入</th>
                                    <th>Output Token</th>
                                    <th>缓存命中率</th>
                                    <th>总 Token</th>
                                    <th>花费</th>
                                    <th>耗时</th>
                                    <th>状态</th>
                                </tr>
                            </thead>
                            <tbody>
                                {usage.data.map((log, i) => (
                                    <tr key={i}>
                                        <td>{new Date(log.created_at).toLocaleString('zh-CN')}</td>
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
                                        <td>{(log.cached_tokens || 0).toLocaleString()}</td>
                                        <td>{Math.max(0, (log.input_tokens || 0) - (log.cached_tokens || 0)).toLocaleString()}</td>
                                        <td>{log.output_tokens.toLocaleString()}</td>
                                        <td>
                                            {log.usage_unit_type === 'images'
                                                ? '-'
                                                : formatCacheHitRate(log.cached_tokens || 0, log.input_tokens || 0)}
                                        </td>
                                        <td><strong>{log.total_tokens.toLocaleString()}</strong></td>
                                        <td className="cost-cell">${log.cost_usd.toFixed(2)}</td>
                                        <td>{(log.duration_ms / 1000).toFixed(1)}s</td>
                                        <td><span className={`badge ${log.status_code === 200 ? 'badge-success' : 'badge-error'}`}>{log.status_code}</span></td>
                                    </tr>
                                ))}
                                {usage.data.length === 0 && (
                                    <tr><td colSpan="13" style={{ textAlign: 'center', padding: '2rem', color: 'var(--text-tertiary)' }}>暂无数据</td></tr>
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
