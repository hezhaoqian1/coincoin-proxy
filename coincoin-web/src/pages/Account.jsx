import { useEffect, useState } from 'react'
import { changeAccountPassword, getAuthProfile, sendAccountEmailCode, verifyAccountEmail } from '../api/client'
import { useAuth } from '../hooks/useAuth'
import AppShell from '../components/AppShell'
import './Settings.css'

function EmailBindingCard({ isConsoleSession }) {
    const [profile, setProfile] = useState(null)
    const [email, setEmail] = useState('')
    const [code, setCode] = useState('')
    const [loading, setLoading] = useState(false)
    const [message, setMessage] = useState('')
    const [error, setError] = useState('')

    useEffect(() => {
        if (!isConsoleSession) return
        let active = true
        getAuthProfile()
            .then((data) => {
                if (!active) return
                setProfile(data)
                setEmail(data.email || '')
            })
            .catch(() => {
                if (!active) return
                setError('邮箱状态读取失败')
            })
        return () => { active = false }
    }, [isConsoleSession])

    if (!isConsoleSession) return null

    const verified = !!profile?.email_verified_at
    const hasEmail = !!profile?.email
    const savedEmail = profile?.email || ''
    const normalizedEmail = email.trim().toLowerCase()
    const normalizedSavedEmail = savedEmail.trim().toLowerCase()
    const emailChanged = !!normalizedEmail && normalizedEmail !== normalizedSavedEmail
    const sendButtonLabel = verified
        ? (emailChanged ? '发送到新邮箱' : '重新验证邮箱')
        : hasEmail
            ? (emailChanged ? '发送到新邮箱' : '重新发送验证码')
            : '发送验证码'

    const handleSend = async (event) => {
        event.preventDefault()
        if (!email.trim()) { setError('请输入邮箱'); return }
        setLoading(true)
        setError('')
        setMessage('')
        try {
            const targetEmail = email.trim()
            const data = await sendAccountEmailCode(targetEmail)
            setProfile(data)
            setEmail(data.email || targetEmail)
            setMessage(`验证码已发送到 ${data.email || targetEmail}`)
        } catch (err) {
            setError(err.message || '发送失败')
        } finally {
            setLoading(false)
        }
    }

    const handleVerify = async (event) => {
        event.preventDefault()
        if (!code.trim()) { setError('请输入验证码'); return }
        setLoading(true)
        setError('')
        setMessage('')
        try {
            const data = await verifyAccountEmail(code.trim())
            setProfile(data)
            setCode('')
            setMessage('邮箱已验证')
        } catch (err) {
            setError(err.message || '验证失败')
        } finally {
            setLoading(false)
        }
    }

    return (
        <div className={`glass-card settings-section email-binding-card animate-fade-in-up ${verified ? 'settings-alert-success' : 'settings-alert-warning'}`}>
            <div className="settings-section-head">
                <div>
                    <h3>邮箱验证</h3>
                    <p className="settings-subtitle">
                        {verified ? '当前账号已绑定邮箱。修改地址后，需要重新验证新邮箱。' : hasEmail ? `验证码会发送到 ${savedEmail}。如果要换邮箱，先改上面的地址再发送。` : '老账号可以继续使用，建议补一个邮箱。'}
                    </p>
                </div>
                <span className="meta-pill">{verified ? '已验证' : hasEmail ? '待验证' : '未绑定'}</span>
            </div>
            <form className="email-binding-form" onSubmit={handleSend}>
                <label className="email-field-label" htmlFor="account-email">
                    邮箱地址{hasEmail ? '（可修改）' : ''}
                </label>
                <div className="email-binding-row">
                    <input
                        id="account-email"
                        type="email"
                        className="input-field"
                        placeholder="name@example.com"
                        value={email}
                        onChange={(event) => { setEmail(event.target.value); setError(''); setMessage('') }}
                        disabled={loading}
                    />
                    <button className="btn btn-secondary btn-sm" type="submit" disabled={loading}>
                        {sendButtonLabel}
                    </button>
                </div>
                {emailChanged && (
                    <p className="email-change-note">
                        将把验证邮件发送到新地址：{email.trim()}
                    </p>
                )}
            </form>
            {!verified && hasEmail && (
                <form className="email-binding-form" onSubmit={handleVerify}>
                    <div className="email-binding-row">
                        <input
                            type="text"
                            inputMode="numeric"
                            className="input-field email-code-field"
                            placeholder="6 位验证码"
                            value={code}
                            onChange={(event) => { setCode(event.target.value.replace(/\D/g, '').slice(0, 6)); setError(''); setMessage('') }}
                            disabled={loading}
                        />
                        <button className="btn btn-primary btn-sm" type="submit" disabled={loading}>
                            {loading ? '处理中...' : '验证邮箱'}
                        </button>
                    </div>
                </form>
            )}
            {message && <p className="settings-form-message success">{message}</p>}
            {error && <p className="settings-form-message error">{error}</p>}
        </div>
    )
}

function PasswordCard({ isConsoleSession }) {
    const [currentPassword, setCurrentPassword] = useState('')
    const [newPassword, setNewPassword] = useState('')
    const [confirmPassword, setConfirmPassword] = useState('')
    const [loading, setLoading] = useState(false)
    const [message, setMessage] = useState('')
    const [error, setError] = useState('')

    if (!isConsoleSession) return null

    const handleSubmit = async (event) => {
        event.preventDefault()
        setError('')
        setMessage('')
        if (!currentPassword) { setError('请输入当前密码'); return }
        if (newPassword.length < 6) { setError('新密码至少 6 位'); return }
        if (newPassword !== confirmPassword) { setError('两次输入的新密码不一致'); return }

        setLoading(true)
        try {
            await changeAccountPassword(currentPassword, newPassword)
            setCurrentPassword('')
            setNewPassword('')
            setConfirmPassword('')
            setMessage('密码已更新，下次登录请使用新密码。')
        } catch (err) {
            setError(err.message || '修改失败')
        } finally {
            setLoading(false)
        }
    }

    return (
        <div className="glass-card settings-section password-card animate-fade-in-up">
            <div className="settings-section-head">
                <div>
                    <h3>登录密码</h3>
                    <p className="settings-subtitle">修改控制台登录密码，不影响已经生成的开发者 Key。</p>
                </div>
                <span className="meta-pill">账号安全</span>
            </div>
            <form className="password-form" onSubmit={handleSubmit}>
                <div className="password-grid">
                    <label className="password-field">
                        <span>当前密码</span>
                        <input
                            type="password"
                            className="input-field"
                            value={currentPassword}
                            onChange={(event) => { setCurrentPassword(event.target.value); setError(''); setMessage('') }}
                            autoComplete="current-password"
                            disabled={loading}
                        />
                    </label>
                    <label className="password-field">
                        <span>新密码</span>
                        <input
                            type="password"
                            className="input-field"
                            value={newPassword}
                            onChange={(event) => { setNewPassword(event.target.value); setError(''); setMessage('') }}
                            autoComplete="new-password"
                            disabled={loading}
                        />
                    </label>
                    <label className="password-field">
                        <span>确认新密码</span>
                        <input
                            type="password"
                            className="input-field"
                            value={confirmPassword}
                            onChange={(event) => { setConfirmPassword(event.target.value); setError(''); setMessage('') }}
                            autoComplete="new-password"
                            disabled={loading}
                        />
                    </label>
                </div>
                <div className="password-actions">
                    <button className="btn btn-primary btn-sm" type="submit" disabled={loading}>
                        {loading ? '更新中...' : '更新密码'}
                    </button>
                </div>
            </form>
            {message && <p className="settings-form-message success">{message}</p>}
            {error && <p className="settings-form-message error">{error}</p>}
        </div>
    )
}

export default function Account() {
    const { isConsoleSession, username, authMode } = useAuth()

    return (
        <AppShell
            title="个人中心"
            description="账号邮箱和登录密码。"
        >
            <div className="settings-grid">
                {!isConsoleSession && (
                    <div className="glass-card settings-section settings-alert animate-fade-in-up">
                        <h3>当前是开发者 Key 直登</h3>
                        <p className="settings-text">
                            开发者 Key 直登只能调用 API。修改登录密码和邮箱需要使用控制台账号登录。
                        </p>
                        <div className="settings-inline-meta">
                            <span className="meta-pill">登录方式：{authMode === 'api' ? '开发者 Key' : '未登录或 Demo'}</span>
                        </div>
                    </div>
                )}

                {isConsoleSession && (
                    <div className="glass-card settings-section settings-alert animate-fade-in-up">
                        <h3>账号资料</h3>
                        <div className="settings-inline-meta">
                            <span className="meta-pill">账户：{username || '未命名用户'}</span>
                            <span className="meta-pill">登录方式：控制台账号</span>
                        </div>
                    </div>
                )}

                <EmailBindingCard isConsoleSession={isConsoleSession} />
                <PasswordCard isConsoleSession={isConsoleSession} />
            </div>
        </AppShell>
    )
}
