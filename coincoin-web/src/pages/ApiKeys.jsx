import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import AppShell from '../components/AppShell'
import {
    clearGeneratedKey,
    createDeveloperKey,
    listDeveloperKeys,
    setGeneratedKey,
    updateDeveloperKey,
} from '../api/client'
import { useAuth } from '../hooks/useAuth'
import './ApiKeys.css'

function formatDate(value) {
    if (!value) return '未使用'
    const date = new Date(value)
    if (Number.isNaN(date.getTime())) return '未知'
    return date.toLocaleString('zh-CN', { hour12: false })
}

function KeyStatusPill({ status }) {
    const normalized = status === 'active' ? 'active' : 'disabled'
    return (
        <span className={`api-key-status api-key-status-${normalized}`}>
            {normalized === 'active' ? '可用' : '已禁用'}
        </span>
    )
}

export default function ApiKeys() {
    const { authMode, generatedApiKey, username } = useAuth()
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState('')
    const [creating, setCreating] = useState(false)
    const [filterStatus, setFilterStatus] = useState('all')
    const [search, setSearch] = useState('')
    const [keysState, setKeysState] = useState({ total: 0, active: 0, disabled: 0, data: [] })
    const [revealedKey, setRevealedKey] = useState(generatedApiKey || '')
    const [revealedMaskedKey, setRevealedMaskedKey] = useState('')
    const [copied, setCopied] = useState('')
    const [pendingKeyId, setPendingKeyId] = useState('')

    const loadKeys = async () => {
        setLoading(true)
        setError('')
        try {
            const data = await listDeveloperKeys()
            setKeysState(data)
        } catch (err) {
            setError(err.message || '加载 API 密钥失败')
        } finally {
            setLoading(false)
        }
    }

    useEffect(() => {
        loadKeys()
    }, [])

    useEffect(() => {
        setRevealedKey(generatedApiKey || '')
    }, [generatedApiKey])

    const filteredKeys = useMemo(() => {
        return (keysState.data || []).filter((item) => {
            if (filterStatus !== 'all' && item.status !== filterStatus) return false
            if (search.trim() && !item.masked_key.toLowerCase().includes(search.trim().toLowerCase())) return false
            return true
        })
    }, [filterStatus, keysState.data, search])

    const handleCreate = async () => {
        setCreating(true)
        setError('')
        try {
            const data = await createDeveloperKey()
            setGeneratedKey(data.api_key)
            setRevealedKey(data.api_key)
            setRevealedMaskedKey(data.masked_key)
            window.dispatchEvent(new Event('coincoin-auth-changed'))
            await loadKeys()
        } catch (err) {
            setError(err.message || '创建 API 密钥失败')
        } finally {
            setCreating(false)
        }
    }

    const handleCopy = async (value, label) => {
        if (!value) return
        await navigator.clipboard.writeText(value)
        setCopied(label)
        setTimeout(() => setCopied(''), 2000)
    }

    const handleDisable = async (keyId) => {
        setPendingKeyId(keyId)
        setError('')
        try {
            await updateDeveloperKey(keyId, { status: 'disabled' })
            await loadKeys()
        } catch (err) {
            setError(err.message || '禁用 API 密钥失败')
        } finally {
            setPendingKeyId('')
        }
    }

    const handleDismissReveal = () => {
        clearGeneratedKey()
        setRevealedKey('')
        setRevealedMaskedKey('')
        window.dispatchEvent(new Event('coincoin-auth-changed'))
    }

    const latestUsed = keysState.data.find((item) => item.last_used_at)?.last_used_at || null

    return (
        <AppShell
            title="API 密钥"
            description="管理开发者 API 密钥。控制台 session key 不会出现在这里。"
            actions={
                <button className="btn btn-primary btn-sm" onClick={handleCreate} disabled={creating || authMode === 'api'}>
                    {creating ? '创建中...' : '创建密钥'}
                </button>
            }
        >
            <div className="api-keys-page">
                {authMode === 'api' && (
                    <div className="glass-card api-keys-alert">
                        <h3>当前是开发者 Key 直登</h3>
                        <p>你现在正用一把开发者 Key 登录。创建、禁用或轮换其他 Key 时，建议回控制台账号操作，避免把当前会话用的 Key 一并处理掉。</p>
                    </div>
                )}

                {revealedKey && (
                    <div className="glass-card api-keys-reveal animate-fade-in-up">
                        <div>
                            <span className="meta-pill">仅本次明文展示</span>
                            <h3>新开发者 Key 已创建</h3>
                            <p>完整值也可以从下方列表重新复制。请妥善保存，不要贴到公开文档或聊天记录里。</p>
                        </div>
                        <code className="api-keys-secret">{revealedKey}</code>
                        <div className="api-keys-reveal-actions">
                            <button className="btn btn-primary btn-sm" onClick={() => handleCopy(revealedKey, 'revealed')}>
                                {copied === 'revealed' ? '已复制' : '复制完整 Key'}
                            </button>
                            <button className="btn btn-ghost btn-sm" onClick={handleDismissReveal}>
                                我已保存
                            </button>
                        </div>
                    </div>
                )}

                <div className="api-keys-stats">
                    <div className="glass-card api-keys-stat">
                        <span className="api-keys-stat-label">总数</span>
                        <strong>{keysState.total}</strong>
                        <span className="api-keys-stat-hint">当前账号下的开发者 Key</span>
                    </div>
                    <div className="glass-card api-keys-stat">
                        <span className="api-keys-stat-label">可用</span>
                        <strong>{keysState.active}</strong>
                        <span className="api-keys-stat-hint">可以继续发请求</span>
                    </div>
                    <div className="glass-card api-keys-stat">
                        <span className="api-keys-stat-label">已禁用</span>
                        <strong>{keysState.disabled}</strong>
                        <span className="api-keys-stat-hint">不会再通过认证</span>
                    </div>
                    <div className="glass-card api-keys-stat">
                        <span className="api-keys-stat-label">最近使用</span>
                        <strong>{latestUsed ? formatDate(latestUsed) : '暂无'}</strong>
                        <span className="api-keys-stat-hint">按最后使用时间自动更新</span>
                    </div>
                </div>

                <div className="glass-card api-keys-toolbar">
                    <div>
                        <span className="api-keys-kicker">开发者 Key 管理</span>
                        <h3>{username || '当前账户'} 的开发者 Key</h3>
                        <p>可以直接复制完整 Key；禁用前建议先到使用记录确认它是否仍在发请求。</p>
                    </div>
                    <div className="api-keys-toolbar-actions">
                        <input
                            className="api-keys-search"
                            value={search}
                            onChange={(e) => setSearch(e.target.value)}
                            placeholder="搜索脱敏 Key..."
                        />
                        <select className="api-keys-filter" value={filterStatus} onChange={(e) => setFilterStatus(e.target.value)}>
                            <option value="all">全部状态</option>
                            <option value="active">仅可用</option>
                            <option value="disabled">仅已禁用</option>
                        </select>
                        <button className="btn btn-secondary btn-sm" onClick={loadKeys} disabled={loading}>
                            {loading ? '刷新中...' : '刷新'}
                        </button>
                    </div>
                </div>

                <div className="glass-card api-keys-table-wrap">
                    {error && <p className="api-keys-error">{error}</p>}
                    <table className="data-table api-keys-table">
                        <thead>
                            <tr>
                                <th>API 密钥</th>
                                <th>状态</th>
                                <th>最后使用时间</th>
                                <th>创建时间</th>
                                <th>操作</th>
                            </tr>
                        </thead>
                        <tbody>
                            {!loading && filteredKeys.length === 0 && (
                                <tr>
                                    <td colSpan="5" className="api-keys-empty-cell">
                                        当前没有符合条件的开发者 Key。
                                    </td>
                                </tr>
                            )}
                            {filteredKeys.map((item) => (
                                <tr key={item.key_id}>
                                    <td>
                                        <div className="api-keys-key-cell">
                                            <code>{item.masked_key}</code>
                                            {revealedMaskedKey && item.masked_key === revealedMaskedKey && (
                                                <span className="meta-pill">本次新建</span>
                                            )}
                                        </div>
                                    </td>
                                    <td><KeyStatusPill status={item.status} /></td>
                                    <td>{formatDate(item.last_used_at)}</td>
                                    <td>{formatDate(item.created_at)}</td>
                                    <td>
                                        <div className="api-keys-row-actions">
                                            <button
                                                className="btn btn-ghost btn-sm"
                                                onClick={() => handleCopy(item.api_key || item.masked_key, item.key_id)}
                                            >
                                                {copied === item.key_id ? '已复制密钥' : '复制密钥'}
                                            </button>
                                            <Link className="btn btn-ghost btn-sm" to={`/usage?api_key_id=${encodeURIComponent(item.key_id)}`}>
                                                查看记录
                                            </Link>
                                            {item.status === 'active' ? (
                                                <button
                                                    className="btn btn-secondary btn-sm"
                                                    onClick={() => handleDisable(item.key_id)}
                                                    disabled={pendingKeyId === item.key_id}
                                                >
                                                    {pendingKeyId === item.key_id ? '处理中...' : '禁用'}
                                                </button>
                                            ) : (
                                                <span className="api-keys-disabled-label">已禁用</span>
                                            )}
                                        </div>
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            </div>
        </AppShell>
    )
}
