import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import AppShell from '../components/AppShell'
import {
    centsToDollars,
    clearGeneratedKey,
    createDeveloperKey,
    dollarsToCents,
    listDeveloperKeys,
    setGeneratedKey,
    updateDeveloperKey,
} from '../api/client'
import { useAuth } from '../hooks/useAuth'
import { formatLocalTime } from '../utils/time'
import './ApiKeys.css'

const blankForm = {
    name: '',
    purpose: '',
    monthlyQuotaUsd: '',
    totalQuotaUsd: '',
    expiresAt: '',
    ipAllowlist: '',
}

function formatDate(value, empty = '未设置') {
    if (!value) return empty
    return formatLocalTime(value)
}

function formatMoney(cents) {
    const value = Number(cents || 0)
    return `$${(value / 100).toFixed(2)}`
}

function formatLimit(used, quota) {
    if (!quota) return `${formatMoney(used)} / 不限`
    return `${formatMoney(used)} / ${formatMoney(quota)}`
}

function progressPercent(used, quota) {
    if (!quota) return 0
    return Math.min(100, Math.round((Number(used || 0) / Number(quota || 1)) * 100))
}

function toLocalInputValue(value) {
    if (!value) return ''
    const date = new Date(value)
    if (Number.isNaN(date.getTime())) return ''
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000)
    return local.toISOString().slice(0, 16)
}

function fromLocalInputValue(value) {
    if (!value) return null
    const date = new Date(value)
    return Number.isNaN(date.getTime()) ? null : date.toISOString()
}

function ipTextToList(value) {
    return String(value || '')
        .split(/[\n,]+/)
        .map((item) => item.trim())
        .filter(Boolean)
}

function keyToForm(item) {
    return {
        name: item.name || '',
        purpose: item.purpose || '',
        monthlyQuotaUsd: item.monthly_quota_cents ? String(centsToDollars(item.monthly_quota_cents)) : '',
        totalQuotaUsd: item.total_quota_cents ? String(centsToDollars(item.total_quota_cents)) : '',
        expiresAt: toLocalInputValue(item.expires_at),
        ipAllowlist: (item.ip_allowlist || []).join('\n'),
    }
}

function formToPayload(form) {
    return {
        name: form.name.trim(),
        purpose: form.purpose.trim(),
        monthly_quota_cents: dollarsToCents(form.monthlyQuotaUsd),
        total_quota_cents: dollarsToCents(form.totalQuotaUsd),
        expires_at: fromLocalInputValue(form.expiresAt),
        ip_allowlist: ipTextToList(form.ipAllowlist),
    }
}

function KeyStatusPill({ status }) {
    const normalized = status === 'active' ? 'active' : 'disabled'
    return (
        <span className={`api-key-status api-key-status-${normalized}`}>
            {normalized === 'active' ? '可用' : '已禁用'}
        </span>
    )
}

function QuotaLine({ label, used, quota }) {
    const pct = progressPercent(used, quota)
    const tone = quota && pct >= 100 ? 'danger' : quota && pct >= 80 ? 'warn' : 'ok'
    return (
        <div className="api-key-quota-line">
            <div className="api-key-quota-head">
                <span>{label}</span>
                <strong>{formatLimit(used, quota)}</strong>
            </div>
            {quota ? (
                <div className="api-key-progress" aria-hidden="true">
                    <span className={`api-key-progress-bar ${tone}`} style={{ width: `${pct}%` }} />
                </div>
            ) : null}
        </div>
    )
}

function KeyForm({ form, setForm, mode }) {
    return (
        <div className="api-key-form-grid">
            <label>
                <span>名称</span>
                <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder={mode === 'create' ? '生产服务' : '未命名'} maxLength="100" />
            </label>
            <label>
                <span>用途</span>
                <input value={form.purpose} onChange={(e) => setForm({ ...form, purpose: e.target.value })} placeholder="Railway 后端 / Aider / 客户项目" maxLength="255" />
            </label>
            <label>
                <span>月额度 USD</span>
                <input type="number" min="0" step="0.01" value={form.monthlyQuotaUsd} onChange={(e) => setForm({ ...form, monthlyQuotaUsd: e.target.value })} placeholder="不限" />
            </label>
            <label>
                <span>总额度 USD</span>
                <input type="number" min="0" step="0.01" value={form.totalQuotaUsd} onChange={(e) => setForm({ ...form, totalQuotaUsd: e.target.value })} placeholder="不限" />
            </label>
            <label>
                <span>过期时间</span>
                <input type="datetime-local" value={form.expiresAt} onChange={(e) => setForm({ ...form, expiresAt: e.target.value })} />
            </label>
            <label className="api-key-form-wide">
                <span>服务端 IP</span>
                <textarea value={form.ipAllowlist} onChange={(e) => setForm({ ...form, ipAllowlist: e.target.value })} placeholder="留空不限。支持单 IP 或 CIDR，每行一个。" rows="3" />
            </label>
        </div>
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
    const [showCreate, setShowCreate] = useState(false)
    const [createForm, setCreateForm] = useState(blankForm)
    const [editingId, setEditingId] = useState('')
    const [editForm, setEditForm] = useState(blankForm)

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
        const needle = search.trim().toLowerCase()
        return (keysState.data || []).filter((item) => {
            if (filterStatus !== 'all' && item.status !== filterStatus) return false
            if (!needle) return true
            return [item.masked_key, item.name, item.purpose].some((value) => String(value || '').toLowerCase().includes(needle))
        })
    }, [filterStatus, keysState.data, search])

    const limitedCount = (keysState.data || []).filter((item) => item.monthly_quota_cents || item.total_quota_cents || (item.ip_allowlist || []).length || item.expires_at).length
    const latestUsed = keysState.data.find((item) => item.last_used_at)?.last_used_at || null

    const handleCreate = async () => {
        setCreating(true)
        setError('')
        try {
            const data = await createDeveloperKey(showCreate ? formToPayload(createForm) : {})
            setGeneratedKey(data.api_key)
            setRevealedKey(data.api_key)
            setRevealedMaskedKey(data.masked_key)
            setCreateForm(blankForm)
            setShowCreate(false)
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

    const updateKey = async (keyId, payload, fallbackMessage) => {
        setPendingKeyId(keyId)
        setError('')
        try {
            await updateDeveloperKey(keyId, payload)
            await loadKeys()
        } catch (err) {
            setError(err.message || fallbackMessage)
        } finally {
            setPendingKeyId('')
        }
    }

    const handleDisable = (keyId) => updateKey(keyId, { status: 'disabled' }, '禁用 API 密钥失败')
    const handleEnable = (keyId) => updateKey(keyId, { status: 'active' }, '启用 API 密钥失败')

    const startEdit = (item) => {
        setEditingId(item.key_id)
        setEditForm(keyToForm(item))
    }

    const saveEdit = async (keyId) => {
        await updateKey(keyId, formToPayload(editForm), '保存 API 密钥失败')
        setEditingId('')
    }

    const handleDismissReveal = () => {
        clearGeneratedKey()
        setRevealedKey('')
        setRevealedMaskedKey('')
        window.dispatchEvent(new Event('coincoin-auth-changed'))
    }

    return (
        <AppShell
            title="API 密钥"
            description="给不同项目分配 Key，按需限制额度、过期时间和服务端 IP。"
            actions={
                authMode === 'api' ? (
                    <div className="api-keys-header-actions">
                        <span className="api-keys-action-note">开发者 Key 直登不可新建</span>
                        <button
                            className="btn btn-primary btn-sm"
                            type="button"
                            disabled
                            aria-disabled="true"
                            title="请退出后使用控制台账号登录，再创建新的开发者 Key"
                        >
                            仅控制台可新建
                        </button>
                    </div>
                ) : (
                    <button className="btn btn-primary btn-sm" type="button" onClick={() => setShowCreate((value) => !value)}>
                        {showCreate ? '收起' : '新建 Key'}
                    </button>
                )
            }
        >
            <div className="api-keys-page">
                {authMode === 'api' && (
                    <div className="glass-card api-keys-alert">
                        <h3>当前是开发者 Key 直登</h3>
                        <p>创建和轮换 Key 建议回控制台账号操作。</p>
                    </div>
                )}

                {showCreate && (
                    <div className="glass-card api-keys-create">
                        <div className="api-keys-section-head">
                            <div>
                                <span className="api-keys-kicker">New Key</span>
                                <h3>新建开发者 Key</h3>
                            </div>
                            <button className="btn btn-primary btn-sm" onClick={handleCreate} disabled={creating || authMode === 'api'}>
                                {creating ? '创建中...' : '创建'}
                            </button>
                        </div>
                        <KeyForm form={createForm} setForm={setCreateForm} mode="create" />
                    </div>
                )}

                {revealedKey && (
                    <div className="glass-card api-keys-reveal animate-fade-in-up">
                        <div>
                            <span className="meta-pill">本次明文</span>
                            <h3>Key 已创建</h3>
                        </div>
                        <code className="api-keys-secret">{revealedKey}</code>
                        <div className="api-keys-reveal-actions">
                            <button className="btn btn-primary btn-sm" onClick={() => handleCopy(revealedKey, 'revealed')}>
                                {copied === 'revealed' ? '已复制' : '复制完整 Key'}
                            </button>
                            <button className="btn btn-ghost btn-sm" onClick={handleDismissReveal}>
                                收起
                            </button>
                        </div>
                    </div>
                )}

                <div className="api-keys-stats">
                    <div className="glass-card api-keys-stat">
                        <span className="api-keys-stat-label">总数</span>
                        <strong>{keysState.total}</strong>
                        <span className="api-keys-stat-hint">当前账号</span>
                    </div>
                    <div className="glass-card api-keys-stat">
                        <span className="api-keys-stat-label">可用</span>
                        <strong>{keysState.active}</strong>
                        <span className="api-keys-stat-hint">可发请求</span>
                    </div>
                    <div className="glass-card api-keys-stat">
                        <span className="api-keys-stat-label">限制中</span>
                        <strong>{limitedCount}</strong>
                        <span className="api-keys-stat-hint">额度 / IP / 过期</span>
                    </div>
                    <div className="glass-card api-keys-stat">
                        <span className="api-keys-stat-label">最近使用</span>
                        <strong>{latestUsed ? formatDate(latestUsed) : '暂无'}</strong>
                        <span className="api-keys-stat-hint">按最后调用更新</span>
                    </div>
                </div>

                <div className="glass-card api-keys-toolbar">
                    <div>
                        <span className="api-keys-kicker">Developer Keys</span>
                        <h3>{username || '当前账户'}</h3>
                    </div>
                    <div className="api-keys-toolbar-actions">
                        <input
                            className="api-keys-search"
                            value={search}
                            onChange={(e) => setSearch(e.target.value)}
                            placeholder="搜索名称、用途或 Key..."
                        />
                        <select className="api-keys-filter" value={filterStatus} onChange={(e) => setFilterStatus(e.target.value)}>
                            <option value="all">全部</option>
                            <option value="active">可用</option>
                            <option value="disabled">已禁用</option>
                        </select>
                        <button className="btn btn-secondary btn-sm" onClick={loadKeys} disabled={loading}>
                            {loading ? '刷新中...' : '刷新'}
                        </button>
                    </div>
                </div>

                <div className="api-keys-list">
                    {error && <p className="api-keys-error">{error}</p>}
                    {!loading && filteredKeys.length === 0 && (
                        <div className="glass-card api-keys-empty">暂无符合条件的 Key。</div>
                    )}
                    {filteredKeys.map((item) => {
                        const isEditing = editingId === item.key_id
                        return (
                            <article className="glass-card api-key-card" key={item.key_id}>
                                <div className="api-key-card-top">
                                    <div className="api-key-title">
                                        <div>
                                            <h3>{item.name || '未命名 Key'}</h3>
                                            <code>{item.masked_key}</code>
                                        </div>
                                        {revealedMaskedKey && item.masked_key === revealedMaskedKey && <span className="meta-pill">本次新建</span>}
                                    </div>
                                    <KeyStatusPill status={item.status} />
                                </div>

                                {isEditing ? (
                                    <KeyForm form={editForm} setForm={setEditForm} mode="edit" />
                                ) : (
                                    <>
                                        <div className="api-key-meta-grid">
                                            <div>
                                                <span>用途</span>
                                                <strong>{item.purpose || '未填写'}</strong>
                                            </div>
                                            <div>
                                                <span>过期</span>
                                                <strong>{formatDate(item.expires_at, '长期')}</strong>
                                            </div>
                                            <div>
                                                <span>服务端 IP</span>
                                                <strong>{(item.ip_allowlist || []).length ? `${item.ip_allowlist.length} 条` : '不限'}</strong>
                                            </div>
                                            <div>
                                                <span>最后使用</span>
                                                <strong>{formatDate(item.last_used_at, '从未使用')}</strong>
                                            </div>
                                        </div>
                                        {(item.ip_allowlist || []).length ? (
                                            <div className="api-key-ip-list">
                                                {item.ip_allowlist.map((ip) => <code key={ip}>{ip}</code>)}
                                            </div>
                                        ) : null}
                                        <div className="api-key-quota-grid">
                                            <QuotaLine label="本月" used={item.monthly_used_cents} quota={item.monthly_quota_cents} />
                                            <QuotaLine label="累计" used={item.total_used_cents} quota={item.total_quota_cents} />
                                        </div>
                                    </>
                                )}

                                <div className="api-keys-row-actions">
                                    {isEditing ? (
                                        <>
                                            <button className="btn btn-primary btn-sm" onClick={() => saveEdit(item.key_id)} disabled={pendingKeyId === item.key_id}>
                                                {pendingKeyId === item.key_id ? '保存中...' : '保存'}
                                            </button>
                                            <button className="btn btn-ghost btn-sm" onClick={() => setEditingId('')}>取消</button>
                                        </>
                                    ) : (
                                        <>
                                            <button className="btn btn-ghost btn-sm" onClick={() => handleCopy(item.api_key || item.masked_key, item.key_id)}>
                                                {copied === item.key_id ? '已复制' : '复制'}
                                            </button>
                                            <button className="btn btn-ghost btn-sm" onClick={() => startEdit(item)}>编辑</button>
                                            <Link className="btn btn-ghost btn-sm" to={`/usage?api_key_id=${encodeURIComponent(item.key_id)}`}>记录</Link>
                                            {item.status === 'active' ? (
                                                <button className="btn btn-secondary btn-sm" onClick={() => handleDisable(item.key_id)} disabled={pendingKeyId === item.key_id}>
                                                    {pendingKeyId === item.key_id ? '处理中...' : '禁用'}
                                                </button>
                                            ) : (
                                                <button className="btn btn-secondary btn-sm" onClick={() => handleEnable(item.key_id)} disabled={pendingKeyId === item.key_id}>
                                                    {pendingKeyId === item.key_id ? '处理中...' : '启用'}
                                                </button>
                                            )}
                                        </>
                                    )}
                                </div>
                            </article>
                        )
                    })}
                </div>
            </div>
        </AppShell>
    )
}
