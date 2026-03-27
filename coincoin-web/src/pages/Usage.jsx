import { useState, useEffect, useCallback } from 'react'
import { MOCK_USAGE, getApiKey, getUsageLogs } from '../api/client'
import './Usage.css'

export default function Usage() {
    const [usage, setUsage] = useState(null)
    const [page, setPage] = useState(0)
    const [filters, setFilters] = useState({ endpoint: '', status_code: '', start_date: '', end_date: '' })
    const limit = 15

    const load = useCallback(async () => {
        try {
            const params = new URLSearchParams({ limit, offset: page * limit })
            if (filters.endpoint) params.set('endpoint', filters.endpoint)
            if (filters.status_code) params.set('status_code', filters.status_code)
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

    const exportCSV = () => {
        if (!usage?.data?.length) return
        const headers = ['时间', '端点', '公开模型', '上游模型', '计量类型', '计量值', 'Input Token', 'Output Token', '总 Token', '花费($)', '耗时(ms)', '状态码']
        const rows = usage.data.map(d => [
            d.created_at, d.endpoint, d.model, d.provider_model,
            d.usage_unit_type, d.usage_unit_type === 'images' ? (d.image_count || d.usage_unit_count) : d.usage_unit_count,
            d.input_tokens, d.output_tokens, d.total_tokens,
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
        setPage(0)
    }

    if (!usage) {
        return (
            <div className="page-wrapper"><div className="container">
                <div className="loading-state"><div className="loading-spinner"></div><p>加载中...</p></div>
            </div></div>
        )
    }

    const totalCost = usage.data.reduce((s, d) => s + d.cost_usd, 0)
    const totalTokens = usage.data.reduce((s, d) => s + d.total_tokens, 0)
    const totalImages = usage.data.reduce((s, d) => s + (d.image_count || 0), 0)

    return (
        <div className="page-wrapper">
            <div className="container">
                <div className="page-header">
                    <h1 className="page-title">使用明细</h1>
                    <p className="page-desc">详细查看每次 API 请求的模型 alias、上游模型、Token / 图片计量和费用</p>
                </div>

                <div className="usage-overview glass-card animate-fade-in-up">
                    <div className="usage-overview-copy">
                        <span className="usage-kicker">Request Analytics</span>
                        <h2>这页不是流水账，而是你的请求分析台</h2>
                        <p>当你要确认某次调用到底用了哪个公开模型、落到哪个上游、花了多少钱、为什么失败时，优先来这里看，而不是先猜客户端配置。</p>
                    </div>
                    <div className="usage-overview-points">
                        <div className="usage-overview-item">
                            <strong>看模型路由</strong>
                            <p>同时看公开模型 alias 和上游模型，确认是不是走错了提供方。</p>
                        </div>
                        <div className="usage-overview-item">
                            <strong>看计费单位</strong>
                            <p>文本请求重点看 tokens，图片请求重点看 images 和记费变化。</p>
                        </div>
                        <div className="usage-overview-item">
                            <strong>看错误状态</strong>
                            <p>403、402、429 这些状态码在这里最容易和上下文一起判断。</p>
                        </div>
                    </div>
                </div>

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
                            <span className="stat-label">页内花费</span>
                            <span className="stat-value">${totalCost.toFixed(2)}</span>
                        </div>
                    </div>
                    <div className="stat-card glass-card animate-fade-in-up">
                        <div className="stat-icon" style={{ background: 'rgba(6,182,212,0.12)' }}>
                            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="var(--accent-cyan)" strokeWidth="2"><path d="M22 12h-4l-3 9L9 3l-3 9H2" /></svg>
                        </div>
                        <div className="stat-info">
                            <span className="stat-label">页内 Token</span>
                            <span className="stat-value">{totalTokens.toLocaleString()}</span>
                        </div>
                    </div>
                    <div className="stat-card glass-card animate-fade-in-up">
                        <div className="stat-icon" style={{ background: 'rgba(245,158,11,0.12)' }}>
                            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="var(--accent-amber)" strokeWidth="2"><path d="M4 7h16M4 17h16M7 4v16M17 4v16" /></svg>
                        </div>
                        <div className="stat-info">
                            <span className="stat-label">页内图片</span>
                            <span className="stat-value">{totalImages}</span>
                        </div>
                    </div>
                </div>

                {/* Filters */}
                <div className="usage-filters glass-card animate-fade-in-up">
                    <div className="usage-filters-header">
                        <div>
                            <h3>筛选与导出</h3>
                            <p>按端点、状态码和时间区间缩小范围，再导出 CSV 交给自己或同事排查。</p>
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
                                    <th>公开模型</th>
                                    <th>上游模型</th>
                                    <th>计量</th>
                                    <th>Input Token</th>
                                    <th>Output Token</th>
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
                                        <td><span className="provider-tag-sm">{log.provider_model || '-'}</span></td>
                                        <td>
                                            <span className={`usage-pill ${log.usage_unit_type === 'images' ? 'images' : 'tokens'}`}>
                                                {log.usage_unit_type === 'images'
                                                    ? `${log.image_count || log.usage_unit_count || 0} images`
                                                    : `${(log.usage_unit_count || log.total_tokens || 0).toLocaleString()} tokens`}
                                            </span>
                                        </td>
                                        <td>{log.input_tokens.toLocaleString()}</td>
                                        <td>{log.output_tokens.toLocaleString()}</td>
                                        <td><strong>{log.total_tokens.toLocaleString()}</strong></td>
                                        <td className="cost-cell">${log.cost_usd.toFixed(2)}</td>
                                        <td>{(log.duration_ms / 1000).toFixed(1)}s</td>
                                        <td><span className={`badge ${log.status_code === 200 ? 'badge-success' : 'badge-error'}`}>{log.status_code}</span></td>
                                    </tr>
                                ))}
                                {usage.data.length === 0 && (
                                    <tr><td colSpan="11" style={{ textAlign: 'center', padding: '2rem', color: 'var(--text-tertiary)' }}>暂无数据</td></tr>
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
        </div>
    )
}
