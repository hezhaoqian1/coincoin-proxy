import { useEffect, useMemo, useState } from 'react'
import { getReferralInfo, updateReferralCode } from '../api/client'
import AppShell from '../components/AppShell'
import { formatLocalTime } from '../utils/time'
import './Referrals.css'

const WECHAT_GROUP_QR = '/wechat-group-coincoin.jpg'
const WECHAT_ID = 'birdsync'

function money(value) {
    return `$${Number(value || 0).toFixed(2)}`
}

function displayFriend(record) {
    if (record.username) return record.username
    if (record.email) {
        const [name, domain] = record.email.split('@')
        return `${name.slice(0, 1)}***@${domain || ''}`
    }
    return `用户 ${String(record.user_id || '').slice(-6)}`
}

function formatTime(value) {
    if (!value) return '刚刚'
    return formatLocalTime(value, {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
    })
}

export default function Referrals() {
    const [referral, setReferral] = useState(null)
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState('')
    const [copied, setCopied] = useState('')
    const [editing, setEditing] = useState(false)
    const [codeInput, setCodeInput] = useState('')
    const [saving, setSaving] = useState(false)
    const [saveError, setSaveError] = useState('')

    const load = async () => {
        setLoading(true)
        setError('')
        try {
            const data = await getReferralInfo()
            setReferral(data)
            setCodeInput(data.referral_code || '')
        } catch (err) {
            setError(err.message || '加载邀请信息失败')
        } finally {
            setLoading(false)
        }
    }

    useEffect(() => {
        load()
    }, [])

    const inviteUrl = useMemo(() => {
        if (!referral?.referral_code || typeof window === 'undefined') return ''
        return `${window.location.origin}/register?ref=${referral.referral_code}`
    }, [referral?.referral_code])

    const copyText = async (text, label) => {
        await navigator.clipboard.writeText(text)
        setCopied(label)
        setTimeout(() => setCopied(''), 1800)
    }

    const saveCode = async (event) => {
        event.preventDefault()
        const nextCode = codeInput.trim().toUpperCase()
        if (!nextCode) return
        setSaving(true)
        setSaveError('')
        try {
            const result = await updateReferralCode(nextCode)
            setReferral((prev) => ({ ...prev, referral_code: result.referral_code }))
            setCodeInput(result.referral_code)
            setEditing(false)
        } catch (err) {
            setSaveError(err.message || '邀请码更新失败')
        } finally {
            setSaving(false)
        }
    }

    if (loading) {
        return (
            <AppShell title="邀请朋友" description="邀请朋友注册，双方都能拿到 API 额度。">
                <div className="referrals-page">
                    <div className="loading-state">
                        <div className="loading-spinner"></div>
                        <p>加载邀请信息...</p>
                    </div>
                </div>
            </AppShell>
        )
    }

    if (error) {
        return (
            <AppShell title="邀请朋友" description="邀请朋友注册，双方都能拿到 API 额度。">
                <div className="referrals-page">
                    <section className="referrals-empty glass-card">
                        <h2>邀请信息暂不可用</h2>
                        <p>{error}</p>
                        <button className="btn btn-primary btn-sm" onClick={load}>重新加载</button>
                    </section>
                </div>
            </AppShell>
        )
    }

    const records = referral?.records || []

    return (
        <AppShell title="邀请朋友" description="朋友注册就能用，你也会拿到 API 额度奖励。">
            <div className="referrals-page">
                <section className="referrals-hero glass-card">
                    <div className="referrals-hero-copy">
                        <span className="referrals-kicker">Invite</span>
                        <h1>邀请朋友一起用 CoinCoin</h1>
                        <p>
                            朋友用你的链接注册，立刻得 $10 API 额度，你得 $5。
                            朋友开始调用 API 后，你再得 $5。
                            朋友首次充值后，再送朋友 $20；之后朋友每次充值，你都得到账额度 20% 的奖励。
                        </p>
                        <div className="referrals-actions">
                            <button className="btn btn-primary" onClick={() => copyText(inviteUrl, 'invite')}>
                                {copied === 'invite' ? '已复制' : '复制邀请链接'}
                            </button>
                            <button className="btn btn-secondary" onClick={() => setEditing((value) => !value)}>
                                修改邀请码
                            </button>
                        </div>
                    </div>
                    <div className="referrals-link-panel">
                        <span>你的邀请码</span>
                        <strong>{referral.referral_code}</strong>
                        <code>{inviteUrl}</code>
                    </div>
                </section>

                {editing && (
                    <section className="referrals-code-panel glass-card">
                        <form onSubmit={saveCode}>
                            <label>
                                <span>新的邀请码</span>
                                <input
                                    value={codeInput}
                                    onChange={(event) => {
                                        setCodeInput(event.target.value.toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 16))
                                        setSaveError('')
                                    }}
                                    placeholder="例如 JACK2026"
                                />
                            </label>
                            <button className="btn btn-primary btn-sm" disabled={saving}>
                                {saving ? '保存中...' : '保存邀请码'}
                            </button>
                        </form>
                        {saveError ? <p className="referrals-error">{saveError}</p> : <p>邀请码只能包含字母和数字，长度 4-16 位。</p>}
                    </section>
                )}

                <section className="referrals-stats">
                    <div className="referrals-stat glass-card">
                        <span>已邀请</span>
                        <strong>{referral.invited_count || 0}</strong>
                        <small>注册成功后进入记录</small>
                    </div>
                    <div className="referrals-stat glass-card">
                        <span>你已获得</span>
                        <strong>{money(referral.total_reward_usd)}</strong>
                        <small>已入账 API 额度</small>
                    </div>
                    <div className="referrals-stat glass-card">
                        <span>朋友已获得</span>
                        <strong>{money(referral.friend_reward_usd)}</strong>
                        <small>注册和首充奖励</small>
                    </div>
                    <div className="referrals-stat glass-card">
                        <span>待完成</span>
                        <strong>{referral.pending_count || 0}</strong>
                        <small>还没走到持续奖励</small>
                    </div>
                </section>

                <section className="referrals-community glass-card">
                    <div className="referrals-community-copy">
                        <span className="referrals-kicker">微信群</span>
                        <h2>进群再领 $30</h2>
                        <p>扫码进 CoinCoin 微信群，进群后联系管理员领取额外 $30 API 额度。二维码过期时，加微信 {WECHAT_ID}。</p>
                        <div className="referrals-wechat-id">
                            <span>微信号</span>
                            <button onClick={() => copyText(WECHAT_ID, 'wechat')}>{copied === 'wechat' ? '已复制' : WECHAT_ID}</button>
                        </div>
                    </div>
                    <img className="referrals-qr" src={WECHAT_GROUP_QR} alt="CoinCoin 微信群二维码" />
                </section>

                <section className="referrals-records glass-card">
                    <div className="referrals-section-header">
                        <div>
                            <h2>邀请记录</h2>
                            <p>朋友注册后会显示在这里。</p>
                        </div>
                        <button className="btn btn-secondary btn-sm" onClick={load}>刷新</button>
                    </div>

                    {records.length === 0 ? (
                        <div className="referrals-empty-state">
                            <h3>还没有邀请记录</h3>
                            <p>把邀请链接发给朋友。朋友注册后，你们都会拿到 API 额度。</p>
                            <button className="btn btn-primary btn-sm" onClick={() => copyText(inviteUrl, 'empty')}>
                                {copied === 'empty' ? '已复制' : '复制邀请链接'}
                            </button>
                        </div>
                    ) : (
                        <div className="referrals-table-wrap">
                            <table className="referrals-table">
                                <thead>
                                    <tr>
                                        <th>朋友</th>
                                        <th>状态</th>
                                        <th>你获得</th>
                                        <th>朋友获得</th>
                                        <th>下一步</th>
                                        <th>时间</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {records.map((record) => (
                                        <tr key={record.user_id}>
                                            <td>{displayFriend(record)}</td>
                                            <td><span className="referrals-status">{record.status}</span></td>
                                            <td>{money(record.referrer_reward_usd)}</td>
                                            <td>{money(record.referred_reward_usd)}</td>
                                            <td>{record.next_step}</td>
                                            <td>{formatTime(record.last_progress_at || record.created_at)}</td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    )}
                </section>
            </div>
        </AppShell>
    )
}
