import { useEffect, useMemo, useState } from 'react'
import {
    createStationCustomer,
    getStationApplication,
    getStationCommissionLedger,
    getStationCustomers,
    getStationPayoutBatches,
    getStationSummary,
    updateStationSettlement,
} from '../api/client'
import AppShell from '../components/AppShell'
import './Station.css'

function formatMoney(cents) {
    return `¥${((cents || 0) / 100).toFixed(2)}`
}

function formatTime(value) {
    if (!value) return '未发生'
    return new Date(value).toLocaleString('zh-CN')
}

export default function Station() {
    const [applicationState, setApplicationState] = useState(null)
    const [summary, setSummary] = useState(null)
    const [customers, setCustomers] = useState([])
    const [ledger, setLedger] = useState([])
    const [payouts, setPayouts] = useState([])
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState('')
    const [createError, setCreateError] = useState('')
    const [createSuccess, setCreateSuccess] = useState(null)
    const [savingSettlement, setSavingSettlement] = useState(false)
    const [settlementSaved, setSettlementSaved] = useState(false)
    const [customerForm, setCustomerForm] = useState({ username: '', create_api_key: true })
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
                return
            }

            const [summaryData, customerData, ledgerData, payoutData] = await Promise.all([
                getStationSummary(),
                getStationCustomers(),
                getStationCommissionLedger(),
                getStationPayoutBatches(),
            ])
            setSummary(summaryData)
            setCustomers(customerData.data || [])
            setLedger(ledgerData.data || [])
            setPayouts(payoutData.data || [])
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

                <section className="station-grid">
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
