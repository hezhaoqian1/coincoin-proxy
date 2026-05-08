import { useEffect, useMemo, useState } from 'react'
import {
    createStationAlias,
    createStationCustomer,
    getStationAliases,
    getStationApplication,
    getStationAliasTargets,
    getStationBranding,
    getStationCommissionLedger,
    getStationCustomers,
    getStationPayoutBatches,
    getStationSummary,
    updateStationAlias,
    updateStationBranding,
    updateStationPricebook,
    updateStationSettlement,
} from '../api/client'
import AppShell from '../components/AppShell'
import { formatLocalTime } from '../utils/time'
import './Station.css'

function formatMoney(cents) {
    return `¥${((cents || 0) / 100).toFixed(2)}`
}

function formatUsdCents(cents, precision = 2) {
    const value = Number(cents || 0)
    if (!value) return '$0.00'
    return `$${(value / 100).toFixed(precision)}`
}

function formatTime(value) {
    if (!value) return '未发生'
    return formatLocalTime(value)
}

function targetSupportsCapability(target, capability) {
    const caps = target?.capabilities || []
    if (caps.includes(capability)) return true
    if (['chat/completions', 'responses'].includes(capability)) {
        return caps.includes('chat/completions') || caps.includes('responses')
    }
    return false
}

function aliasPriceDraft(pricebook) {
    return {
        retail_input_per_million_cents: String(pricebook?.retail_input_per_million_cents ?? ''),
        retail_output_per_million_cents: String(pricebook?.retail_output_per_million_cents ?? ''),
        retail_price_per_image_cents: String(pricebook?.retail_price_per_image_cents ?? ''),
    }
}

function targetPriceDraft(target) {
    return {
        retail_input_per_million_cents: String(target?.price_input_per_million ?? ''),
        retail_output_per_million_cents: String(target?.price_output_per_million ?? ''),
        retail_price_per_image_cents: String(target?.price_per_image_cents ?? ''),
    }
}

const EMPTY_BRANDING_FORM = {
    display_name: '',
    logo_url: '',
    favicon_url: '',
    support_email: '',
    support_link: '',
    docs_intro: '',
    terms_url: '',
}

const EMPTY_ALIAS_FORM = {
    alias: '',
    target_public_model_id: '',
    capability: 'chat/completions',
    retail_input_per_million_cents: '',
    retail_output_per_million_cents: '',
    retail_price_per_image_cents: '',
    is_default_text: false,
    is_default_image: false,
}

export default function Station() {
    const [applicationState, setApplicationState] = useState(null)
    const [summary, setSummary] = useState(null)
    const [customers, setCustomers] = useState([])
    const [ledger, setLedger] = useState([])
    const [payouts, setPayouts] = useState([])
    const [aliasTargets, setAliasTargets] = useState([])
    const [aliases, setAliases] = useState([])
    const [priceDrafts, setPriceDrafts] = useState({})
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState('')
    const [createError, setCreateError] = useState('')
    const [createSuccess, setCreateSuccess] = useState(null)
    const [aliasError, setAliasError] = useState('')
    const [aliasSaved, setAliasSaved] = useState('')
    const [savingAlias, setSavingAlias] = useState(false)
    const [savingPriceId, setSavingPriceId] = useState('')
    const [brandingSaved, setBrandingSaved] = useState(false)
    const [savingBranding, setSavingBranding] = useState(false)
    const [copiedUrl, setCopiedUrl] = useState('')
    const [savingSettlement, setSavingSettlement] = useState(false)
    const [settlementSaved, setSettlementSaved] = useState(false)
    const [customerForm, setCustomerForm] = useState({ username: '', create_api_key: true })
    const [aliasForm, setAliasForm] = useState(EMPTY_ALIAS_FORM)
    const [brandingForm, setBrandingForm] = useState(EMPTY_BRANDING_FORM)
    const [settlementForm, setSettlementForm] = useState({
        settlement_method: 'alipay_manual',
        settlement_payee_name: '',
        settlement_payee_account: '',
        settlement_qr_url: '',
    })

    const load = async () => {
        setLoading(true)
        setError('')
        try {
            const appState = await getStationApplication()
            setApplicationState(appState)
            if (!appState?.station || appState.station.status !== 'active') {
                setSummary(null)
                setCustomers([])
                setLedger([])
                setPayouts([])
                setAliasTargets([])
                setAliases([])
                return
            }

            const [summaryData, customerData, ledgerData, payoutData, targetData, aliasData, brandingData] = await Promise.all([
                getStationSummary(),
                getStationCustomers(),
                getStationCommissionLedger(),
                getStationPayoutBatches(),
                getStationAliasTargets(),
                getStationAliases(),
                getStationBranding(),
            ])
            setSummary(summaryData)
            setCustomers(customerData.data || [])
            setLedger(ledgerData.data || [])
            setPayouts(payoutData.data || [])
            setAliasTargets(targetData.data || [])
            const nextAliases = aliasData.data || []
            setAliases(nextAliases)
            setPriceDrafts(Object.fromEntries(nextAliases
                .filter((item) => item.pricebook?.id)
                .map((item) => [item.pricebook.id, aliasPriceDraft(item.pricebook)])
            ))
            const branding = brandingData.branding || {}
            setBrandingForm({
                ...EMPTY_BRANDING_FORM,
                ...Object.fromEntries(Object.keys(EMPTY_BRANDING_FORM).map((key) => [key, branding[key] || ''])),
            })
            const firstTextTarget = (targetData.data || []).find((target) => targetSupportsCapability(target, 'chat/completions'))
            if (firstTextTarget) {
                setAliasForm((prev) => ({
                    ...prev,
                    target_public_model_id: prev.target_public_model_id || firstTextTarget.id,
                    ...targetPriceDraft(firstTextTarget),
                }))
            }
            setSettlementForm({
                settlement_method: summaryData.station?.settlement_method || 'alipay_manual',
                settlement_payee_name: summaryData.station?.settlement_payee_name || '',
                settlement_payee_account: summaryData.station?.settlement_payee_account || '',
                settlement_qr_url: summaryData.station?.settlement_qr_url || '',
            })
        } catch (err) {
            setError(err.message || '加载站长中心失败')
        } finally {
            setLoading(false)
        }
    }

    useEffect(() => {
        load()
    }, [])

    const station = summary?.station || applicationState?.station || null
    const commission = summary?.commission_summary || {}
    const payoutSummary = summary?.payout_summary || {}

    const recentLedger = useMemo(() => ledger.slice(0, 8), [ledger])
    const recentPayouts = useMemo(() => payouts.slice(0, 6), [payouts])
    const selectedAliasTarget = useMemo(
        () => aliasTargets.find((target) => target.id === aliasForm.target_public_model_id) || null,
        [aliasTargets, aliasForm.target_public_model_id],
    )

    const handleCopyUrl = async (value, label) => {
        if (!value) return
        try {
            await navigator.clipboard.writeText(value)
            setCopiedUrl(label)
            setTimeout(() => {
                setCopiedUrl((current) => (current === label ? '' : current))
            }, 1400)
        } catch (err) {
            setCopiedUrl('')
        }
    }

    const reloadAliases = async () => {
        const aliasData = await getStationAliases()
        const nextAliases = aliasData.data || []
        setAliases(nextAliases)
        setPriceDrafts(Object.fromEntries(nextAliases
            .filter((item) => item.pricebook?.id)
            .map((item) => [item.pricebook.id, aliasPriceDraft(item.pricebook)])
        ))
    }

    const handleAliasTargetChange = (targetId) => {
        const target = aliasTargets.find((item) => item.id === targetId)
        setAliasForm((prev) => ({
            ...prev,
            target_public_model_id: targetId,
            ...targetPriceDraft(target),
        }))
    }

    const handleAliasSubmit = async (event) => {
        event.preventDefault()
        setAliasError('')
        setAliasSaved('')
        setSavingAlias(true)
        try {
            await createStationAlias({
                ...aliasForm,
                retail_input_per_million_cents: Number(aliasForm.retail_input_per_million_cents || 0),
                retail_output_per_million_cents: Number(aliasForm.retail_output_per_million_cents || 0),
                retail_price_per_image_cents: Number(aliasForm.retail_price_per_image_cents || 0),
            })
            await reloadAliases()
            setAliasForm({
                ...EMPTY_ALIAS_FORM,
                target_public_model_id: selectedAliasTarget?.id || '',
                ...targetPriceDraft(selectedAliasTarget),
            })
            setAliasSaved('别名已发布，下游可在 /v1/models 看到。')
        } catch (err) {
            setAliasError(err.message || '创建别名失败')
        } finally {
            setSavingAlias(false)
        }
    }

    const handlePriceSave = async (aliasItem) => {
        const pricebook = aliasItem.pricebook
        if (!pricebook?.id) return
        setAliasError('')
        setAliasSaved('')
        setSavingPriceId(pricebook.id)
        const draft = priceDrafts[pricebook.id] || {}
        try {
            await updateStationPricebook(pricebook.id, {
                retail_input_per_million_cents: Number(draft.retail_input_per_million_cents || 0),
                retail_output_per_million_cents: Number(draft.retail_output_per_million_cents || 0),
                retail_price_per_image_cents: Number(draft.retail_price_per_image_cents || 0),
            })
            await reloadAliases()
            setAliasSaved(`${aliasItem.alias} 价格已保存。`)
        } catch (err) {
            setAliasError(err.message || '保存价格失败')
        } finally {
            setSavingPriceId('')
        }
    }

    const handleToggleAliasDefault = async (aliasItem, fieldName) => {
        setAliasError('')
        setAliasSaved('')
        try {
            await updateStationAlias(aliasItem.id, { [fieldName]: true })
            const [aliasData, summaryData] = await Promise.all([getStationAliases(), getStationSummary()])
            const nextAliases = aliasData.data || []
            setAliases(nextAliases)
            setSummary(summaryData)
            setAliasSaved(`${aliasItem.alias} 已设为默认别名。`)
        } catch (err) {
            setAliasError(err.message || '更新默认别名失败')
        }
    }

    const handleBrandingSubmit = async (event) => {
        event.preventDefault()
        setSavingBranding(true)
        setBrandingSaved(false)
        setError('')
        try {
            const result = await updateStationBranding(brandingForm)
            setSummary((prev) => prev ? { ...prev, station: result.station } : prev)
            setApplicationState((prev) => prev ? { ...prev, station: result.station } : prev)
            setBrandingForm((prev) => ({ ...prev, ...(result.branding || {}) }))
            setBrandingSaved(true)
        } catch (err) {
            setError(err.message || '保存品牌资料失败')
        } finally {
            setSavingBranding(false)
        }
    }

    const handleCustomerSubmit = async (event) => {
        event.preventDefault()
        setCreateError('')
        setCreateSuccess(null)
        try {
            const result = await createStationCustomer(customerForm)
            setCreateSuccess(result)
            setCustomerForm({ username: '', create_api_key: true })
            const [customerData, summaryData] = await Promise.all([getStationCustomers(), getStationSummary()])
            setCustomers(customerData.data || [])
            setSummary(summaryData)
        } catch (err) {
            setCreateError(err.message || '创建下游用户失败')
        }
    }

    const handleSettlementSubmit = async (event) => {
        event.preventDefault()
        setSavingSettlement(true)
        setSettlementSaved(false)
        setError('')
        try {
            const result = await updateStationSettlement(settlementForm)
            setSummary((prev) => prev ? { ...prev, station: result.station } : prev)
            setApplicationState((prev) => prev ? { ...prev, station: result.station } : prev)
            setSettlementSaved(true)
        } catch (err) {
            setError(err.message || '保存结算信息失败')
        } finally {
            setSavingSettlement(false)
        }
    }

    if (loading) {
        return (
            <AppShell title="站长中心" description="管理下游用户、分润账本和结算资料。">
                    <div className="loading-state">
                        <div className="loading-spinner"></div>
                        <p>加载站长中心...</p>
                    </div>
            </AppShell>
        )
    }

    if (error && !station) {
        return (
            <AppShell title="站长中心" description="管理下游用户、分润账本和结算资料。">
                    <div className="station-empty glass-card">
                        <h1>站长中心暂不可用</h1>
                        <p>{error}</p>
                    </div>
            </AppShell>
        )
    }

    if (!station) {
        const application = applicationState?.application
        return (
            <AppShell title="站长中心" description="管理下游用户、分润账本和结算资料。">
                <div className="station-page">
                    <section className="station-hero glass-card">
                        <span className="station-kicker">Station Center</span>
                        <h1>你还没有开通站长资格</h1>
                        <p>站长中心只对审核通过后的站长开放。先回概览页提交申请，平台统一收款后再按账本人工打款给你。</p>
                    </section>
                    <section className="station-empty glass-card">
                        <h2>{application ? '当前申请状态' : '下一步'}</h2>
                        <p>{application ? `当前状态：${application.status}` : '先到概览页提交“申请成为站长”。'}</p>
                    </section>
                </div>
            </AppShell>
        )
    }

    return (
        <AppShell title="站长中心" description="管理下游用户、分润账本和结算资料。">
            <div className="station-page">
                <section className="station-hero glass-card">
                    <div>
                        <span className="station-kicker">Station Center</span>
                        <h1>{station.display_name}</h1>
                        <p>平台统一收款，站长配置支付宝收款信息，后台按可结算账本手动打款。这里负责下游开户、分润追踪和结算资料。</p>
                    </div>
                    <div className="station-hero-meta">
                        <span className="badge badge-success">已开通</span>
                        <span className="station-meta-pill">Slug: {station.slug}</span>
                        <span className="station-meta-pill">佣金比例 {(Number(station.commission_rate || 0) * 100).toFixed(0)}%</span>
                    </div>
                </section>

                {error && <div className="station-inline-error glass-card">{error}</div>}

                <section className="station-stats">
                    <div className="station-stat glass-card">
                        <span>下游用户</span>
                        <strong>{summary?.customer_count || 0}</strong>
                        <small>已归属到你的站点</small>
                    </div>
                    <div className="station-stat glass-card">
                        <span>待结算佣金</span>
                        <strong>{formatMoney(commission.pending_rmb_cents)}</strong>
                        <small>{commission.pending_count || 0} 笔还在冻结期或待出批次</small>
                    </div>
                    <div className="station-stat glass-card">
                        <span>待打款批次</span>
                        <strong>{formatMoney(payoutSummary.pending_batch_total_rmb_cents)}</strong>
                        <small>{payoutSummary.pending_batch_count || 0} 个批次待后台转账</small>
                    </div>
                    <div className="station-stat glass-card">
                        <span>累计已打款</span>
                        <strong>{formatMoney(commission.paid_rmb_cents)}</strong>
                        <small>最近打款：{formatTime(payoutSummary.last_paid_at)}</small>
                    </div>
                </section>

                <section className="station-panel station-access-panel glass-card">
                    <div className="station-panel-header">
                        <div>
                            <h2>站点访问地址</h2>
                            <p>给下游用户使用你的站点入口；API Base URL 仍然走 CoinCoin 公网，不暴露内部上游。</p>
                        </div>
                    </div>
                    <div className="station-access-list">
                        <div className="station-access-row">
                            <span>客户入口</span>
                            {station.portal_url ? <a href={station.portal_url} target="_blank" rel="noreferrer">{station.portal_url}</a> : <code>未配置</code>}
                            <button className="btn btn-ghost btn-sm" type="button" onClick={() => handleCopyUrl(station.portal_url, 'portal')} disabled={!station.portal_url}>
                                {copiedUrl === 'portal' ? '已复制' : '复制'}
                            </button>
                        </div>
                        <div className="station-access-row">
                            <span>API Base URL</span>
                            <code>{station.api_base_url || '未配置'}</code>
                            <button className="btn btn-ghost btn-sm" type="button" onClick={() => handleCopyUrl(station.api_base_url, 'api')} disabled={!station.api_base_url}>
                                {copiedUrl === 'api' ? '已复制' : '复制'}
                            </button>
                        </div>
                    </div>
                    {(station.portal_url_mode === 'path' || station.api_url_mode === 'shared') && (
                        <p className="station-access-note">当前使用共享域名兜底。Cloudflare 通配域名启用后，这里会自动切换为专属子域名。</p>
                    )}
                </section>

                <section className="station-panel glass-card">
                    <div className="station-panel-header station-panel-header-row">
                        <div>
                            <h2>模型别名与定价</h2>
                            <p>站长只配置面向下游的 alias 和零售价格；真实上游仍由 CoinCoin 平台目录托管。</p>
                        </div>
                        <div className="station-alias-count">
                            <strong>{aliases.length}</strong>
                            <span>个别名</span>
                        </div>
                    </div>
                    {aliasError && <div className="station-error-banner">{aliasError}</div>}
                    {aliasSaved && <div className="station-success-banner">{aliasSaved}</div>}
                    <form className="station-alias-form" onSubmit={handleAliasSubmit}>
                        <label className="station-field">
                            <span>Alias</span>
                            <input value={aliasForm.alias} onChange={(e) => setAliasForm((prev) => ({ ...prev, alias: e.target.value }))} placeholder="fast / gpt-best / image" required />
                        </label>
                        <label className="station-field">
                            <span>能力</span>
                            <select value={aliasForm.capability} onChange={(e) => setAliasForm((prev) => ({ ...prev, capability: e.target.value }))}>
                                <option value="chat/completions">Chat</option>
                                <option value="responses">Responses</option>
                                <option value="embeddings">Embeddings</option>
                                <option value="images/generations">Images</option>
                            </select>
                        </label>
                        <label className="station-field station-field-wide">
                            <span>平台目标模型</span>
                            <select value={aliasForm.target_public_model_id} onChange={(e) => handleAliasTargetChange(e.target.value)} required>
                                <option value="">选择平台模型</option>
                                {aliasTargets
                                    .filter((target) => targetSupportsCapability(target, aliasForm.capability))
                                    .map((target) => (
                                        <option key={target.id} value={target.id}>
                                            {target.id} · floor {formatUsdCents(Math.max(Number(target.price_input_per_million || 0), Number(target.price_output_per_million || 0)))}
                                        </option>
                                    ))}
                            </select>
                        </label>
                        <label className="station-field">
                            <span>输入 / 1M</span>
                            <input type="number" min="0" value={aliasForm.retail_input_per_million_cents} onChange={(e) => setAliasForm((prev) => ({ ...prev, retail_input_per_million_cents: e.target.value }))} />
                        </label>
                        <label className="station-field">
                            <span>输出 / 1M</span>
                            <input type="number" min="0" value={aliasForm.retail_output_per_million_cents} onChange={(e) => setAliasForm((prev) => ({ ...prev, retail_output_per_million_cents: e.target.value }))} />
                        </label>
                        <label className="station-field">
                            <span>图片 / 张</span>
                            <input type="number" min="0" step="0.01" value={aliasForm.retail_price_per_image_cents} onChange={(e) => setAliasForm((prev) => ({ ...prev, retail_price_per_image_cents: e.target.value }))} />
                        </label>
                        <label className="station-checkbox station-inline-check">
                            <input type="checkbox" checked={aliasForm.is_default_text} onChange={(e) => setAliasForm((prev) => ({ ...prev, is_default_text: e.target.checked }))} />
                            <span>默认文本</span>
                        </label>
                        <label className="station-checkbox station-inline-check">
                            <input type="checkbox" checked={aliasForm.is_default_image} onChange={(e) => setAliasForm((prev) => ({ ...prev, is_default_image: e.target.checked }))} />
                            <span>默认图片</span>
                        </label>
                        <div className="station-form-actions">
                            <button className="btn btn-primary btn-sm" type="submit" disabled={savingAlias || !aliasForm.target_public_model_id}>
                                {savingAlias ? '发布中...' : '发布别名'}
                            </button>
                        </div>
                    </form>
                    <div className="station-table-wrap station-alias-table-wrap">
                        <table className="station-table station-alias-table">
                            <thead>
                                <tr>
                                    <th>Alias</th>
                                    <th>目标</th>
                                    <th>价格</th>
                                    <th>默认</th>
                                    <th>状态</th>
                                    <th>操作</th>
                                </tr>
                            </thead>
                            <tbody>
                                {aliases.map((item) => {
                                    const price = item.pricebook || {}
                                    const draft = priceDrafts[price.id] || aliasPriceDraft(price)
                                    return (
                                        <tr key={item.id}>
                                            <td><code>{item.alias}</code></td>
                                            <td>
                                                <div>{item.target_public_model_id}</div>
                                                <div className="station-subnote">{item.capability}</div>
                                            </td>
                                            <td>
                                                <div className="station-price-inputs">
                                                    <input type="number" min="0" value={draft.retail_input_per_million_cents} onChange={(e) => setPriceDrafts((prev) => ({ ...prev, [price.id]: { ...draft, retail_input_per_million_cents: e.target.value } }))} />
                                                    <input type="number" min="0" value={draft.retail_output_per_million_cents} onChange={(e) => setPriceDrafts((prev) => ({ ...prev, [price.id]: { ...draft, retail_output_per_million_cents: e.target.value } }))} />
                                                    <input type="number" min="0" step="0.01" value={draft.retail_price_per_image_cents} onChange={(e) => setPriceDrafts((prev) => ({ ...prev, [price.id]: { ...draft, retail_price_per_image_cents: e.target.value } }))} />
                                                </div>
                                                <div className="station-subnote">input / output / image，单位是美分</div>
                                            </td>
                                            <td>
                                                <div className="station-default-actions">
                                                    <button className={`btn btn-sm ${item.is_default_text ? 'btn-primary' : 'btn-ghost'}`} type="button" onClick={() => handleToggleAliasDefault(item, 'is_default_text')}>文本</button>
                                                    <button className={`btn btn-sm ${item.is_default_image ? 'btn-primary' : 'btn-ghost'}`} type="button" onClick={() => handleToggleAliasDefault(item, 'is_default_image')}>图片</button>
                                                </div>
                                            </td>
                                            <td><span className={`badge ${item.status === 'active' ? 'badge-success' : 'badge-warning'}`}>{item.status}</span></td>
                                            <td>
                                                <button className="btn btn-secondary btn-sm" type="button" onClick={() => handlePriceSave(item)} disabled={!price.id || savingPriceId === price.id}>
                                                    {savingPriceId === price.id ? '保存中...' : '保存价格'}
                                                </button>
                                            </td>
                                        </tr>
                                    )
                                })}
                                {aliases.length === 0 && (
                                    <tr><td colSpan="6" className="station-empty-cell">还没有模型别名</td></tr>
                                )}
                            </tbody>
                        </table>
                    </div>
                </section>

                <section className="station-grid">
                    <div className="station-panel glass-card">
                        <div className="station-panel-header">
                            <div>
                                <h2>品牌资料</h2>
                                <p>这些资料会出现在公开站点页和下游接入说明里。</p>
                            </div>
                        </div>
                        <form className="station-form" onSubmit={handleBrandingSubmit}>
                            <label className="station-field">
                                <span>展示名称</span>
                                <input value={brandingForm.display_name} onChange={(e) => setBrandingForm((prev) => ({ ...prev, display_name: e.target.value }))} placeholder={station.display_name} />
                            </label>
                            <label className="station-field">
                                <span>Logo URL</span>
                                <input value={brandingForm.logo_url} onChange={(e) => setBrandingForm((prev) => ({ ...prev, logo_url: e.target.value }))} placeholder="https://cdn.example/logo.png" />
                            </label>
                            <label className="station-field">
                                <span>Support Email</span>
                                <input value={brandingForm.support_email} onChange={(e) => setBrandingForm((prev) => ({ ...prev, support_email: e.target.value }))} placeholder="support@example.com" />
                            </label>
                            <label className="station-field">
                                <span>Support Link</span>
                                <input value={brandingForm.support_link} onChange={(e) => setBrandingForm((prev) => ({ ...prev, support_link: e.target.value }))} placeholder="https://example.com/help" />
                            </label>
                            <label className="station-field">
                                <span>站点介绍</span>
                                <textarea value={brandingForm.docs_intro} onChange={(e) => setBrandingForm((prev) => ({ ...prev, docs_intro: e.target.value }))} rows="4" placeholder="给下游用户看的简短说明" />
                            </label>
                            <div className="station-form-actions">
                                <button className="btn btn-primary btn-sm" type="submit" disabled={savingBranding}>
                                    {savingBranding ? '保存中...' : '保存品牌资料'}
                                </button>
                                {brandingSaved && <span className="station-success">品牌资料已更新</span>}
                            </div>
                        </form>
                    </div>

                    <div className="station-panel glass-card">
                        <div className="station-panel-header">
                            <div>
                                <h2>结算信息</h2>
                                <p>用户付款仍进入平台支付宝。这里保存你收款用的支付宝信息，后台打款后再把批次标记为已打款。</p>
                            </div>
                        </div>
                        <form className="station-form" onSubmit={handleSettlementSubmit}>
                            <label className="station-field">
                                <span>结算方式</span>
                                <input value={settlementForm.settlement_method} onChange={(e) => setSettlementForm((prev) => ({ ...prev, settlement_method: e.target.value }))} />
                            </label>
                            <label className="station-field">
                                <span>支付宝姓名</span>
                                <input value={settlementForm.settlement_payee_name} onChange={(e) => setSettlementForm((prev) => ({ ...prev, settlement_payee_name: e.target.value }))} placeholder="收款人真实姓名" />
                            </label>
                            <label className="station-field">
                                <span>支付宝账号</span>
                                <input value={settlementForm.settlement_payee_account} onChange={(e) => setSettlementForm((prev) => ({ ...prev, settlement_payee_account: e.target.value }))} placeholder="手机号 / 邮箱 / UID" />
                            </label>
                            <label className="station-field">
                                <span>收款码地址</span>
                                <input value={settlementForm.settlement_qr_url} onChange={(e) => setSettlementForm((prev) => ({ ...prev, settlement_qr_url: e.target.value }))} placeholder="可公开访问的二维码图片 URL" />
                            </label>
                            <div className="station-form-actions">
                                <button className="btn btn-primary btn-sm" type="submit" disabled={savingSettlement}>
                                    {savingSettlement ? '保存中...' : '保存结算资料'}
                                </button>
                                {settlementSaved && <span className="station-success">已保存，后续新批次会使用最新资料</span>}
                            </div>
                        </form>
                    </div>

                    <div className="station-panel glass-card">
                        <div className="station-panel-header">
                            <div>
                                <h2>开通下游用户</h2>
                                <p>站长自己分发下游账号。平台仍统一充值收款，但订单会自动归属到站点，后续按账本结算佣金。</p>
                            </div>
                        </div>
                        <form className="station-form" onSubmit={handleCustomerSubmit}>
                            <label className="station-field">
                                <span>用户名</span>
                                <input value={customerForm.username} onChange={(e) => setCustomerForm((prev) => ({ ...prev, username: e.target.value }))} placeholder="支持字母数字、点、下划线和短横线" required />
                            </label>
                            <label className="station-checkbox">
                                <input type="checkbox" checked={customerForm.create_api_key} onChange={(e) => setCustomerForm((prev) => ({ ...prev, create_api_key: e.target.checked }))} />
                                <span>同时生成 API Key</span>
                            </label>
                            <div className="station-form-actions">
                                <button className="btn btn-primary btn-sm" type="submit">创建下游用户</button>
                                {createError && <span className="station-error-text">{createError}</span>}
                            </div>
                        </form>
                        {createSuccess && (
                            <div className="station-success-box">
                                <strong>{createSuccess.username}</strong>
                                <p>下游用户已创建。</p>
                                {createSuccess.api_key && <code>{createSuccess.api_key}</code>}
                            </div>
                        )}
                    </div>
                </section>

                <section className="station-grid station-grid-bottom">
                    <div className="station-panel glass-card">
                        <div className="station-panel-header">
                            <div>
                                <h2>最近佣金账本</h2>
                                <p>这里只显示与你站点相关的订单佣金。</p>
                            </div>
                        </div>
                        <div className="station-table-wrap">
                            <table className="station-table">
                                <thead>
                                    <tr>
                                        <th>订单</th>
                                        <th>用户</th>
                                        <th>状态</th>
                                        <th>订单金额</th>
                                        <th>佣金</th>
                                        <th>冻结到</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {recentLedger.map((entry) => (
                                        <tr key={entry.id}>
                                            <td><code>{entry.order_no}</code></td>
                                            <td>{entry.username}</td>
                                            <td><span className={`badge ${entry.status === 'paid' ? 'badge-success' : entry.status === 'batched' ? 'badge-warning' : ''}`}>{entry.status}</span></td>
                                            <td>{formatMoney(entry.gross_rmb_cents)}</td>
                                            <td>{formatMoney(entry.commission_rmb_cents)}</td>
                                            <td>{formatTime(entry.hold_until)}</td>
                                        </tr>
                                    ))}
                                    {recentLedger.length === 0 && (
                                        <tr><td colSpan="6" className="station-empty-cell">还没有佣金账本记录</td></tr>
                                    )}
                                </tbody>
                            </table>
                        </div>
                    </div>

                    <div className="station-panel glass-card">
                        <div className="station-panel-header">
                            <div>
                                <h2>最近打款批次</h2>
                                <p>后台创建批次并手动打款后，这里会看到状态变化。</p>
                            </div>
                        </div>
                        <div className="station-table-wrap">
                            <table className="station-table">
                                <thead>
                                    <tr>
                                        <th>批次</th>
                                        <th>状态</th>
                                        <th>金额</th>
                                        <th>笔数</th>
                                        <th>创建时间</th>
                                        <th>打款时间</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {recentPayouts.map((batch) => (
                                        <tr key={batch.id}>
                                            <td><code>{batch.id}</code></td>
                                            <td><span className={`badge ${batch.status === 'paid' ? 'badge-success' : 'badge-warning'}`}>{batch.status}</span></td>
                                            <td>{formatMoney(batch.total_commission_rmb_cents)}</td>
                                            <td>{batch.entry_count}</td>
                                            <td>{formatTime(batch.created_at)}</td>
                                            <td>
                                                <div>{formatTime(batch.paid_at)}</div>
                                                {batch.payment_reference && <div className="station-subnote">流水号: {batch.payment_reference}</div>}
                                                {batch.payment_note && <div className="station-subnote">{batch.payment_note}</div>}
                                                {batch.payment_screenshot_url && (
                                                    <div className="station-subnote">
                                                        <a href={batch.payment_screenshot_url} target="_blank" rel="noreferrer">查看打款凭证</a>
                                                    </div>
                                                )}
                                            </td>
                                        </tr>
                                    ))}
                                    {recentPayouts.length === 0 && (
                                        <tr><td colSpan="6" className="station-empty-cell">还没有生成打款批次</td></tr>
                                    )}
                                </tbody>
                            </table>
                        </div>
                    </div>
                </section>

                <section className="station-panel glass-card">
                    <div className="station-panel-header">
                        <div>
                            <h2>下游用户列表</h2>
                            <p>你创建的用户会自动和站点绑定，后续充值订单会归因到你的分润账本。</p>
                        </div>
                    </div>
                    <div className="station-table-wrap">
                        <table className="station-table">
                            <thead>
                                <tr>
                                    <th>用户名</th>
                                    <th>状态</th>
                                    <th>创建时间</th>
                                </tr>
                            </thead>
                            <tbody>
                                {customers.map((customer) => (
                                    <tr key={customer.link_id}>
                                        <td>{customer.username}</td>
                                        <td><span className={`badge ${customer.status === 'active' ? 'badge-success' : 'badge-warning'}`}>{customer.status}</span></td>
                                        <td>{formatTime(customer.created_at)}</td>
                                    </tr>
                                ))}
                                {customers.length === 0 && (
                                    <tr><td colSpan="3" className="station-empty-cell">还没有下游用户</td></tr>
                                )}
                            </tbody>
                        </table>
                    </div>
                </section>
            </div>
        </AppShell>
    )
}
