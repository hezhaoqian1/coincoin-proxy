import { useEffect, useState } from 'react'
import { changeAccountPassword, getAuthProfile } from '../api/client'
import { useAuth } from '../hooks/useAuth'
import AppShell from '../components/AppShell'
import './Settings.css'

function AccountEmailCard({ isConsoleSession }) {
    const [profile, setProfile] = useState(null)
    const [error, setError] = useState('')

    useEffect(() => {
        if (!isConsoleSession) return
        let active = true
        getAuthProfile()
            .then((data) => {
                if (!active) return
                setProfile(data)
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

    return (
        <div className="glass-card settings-section account-email-card animate-fade-in-up">
            <div className="settings-section-head">
                <div>
                    <h3>账号邮箱</h3>
                    <p className="settings-subtitle">
                        邮箱仅用于显示当前控制台账号，不在这里修改。
                    </p>
                </div>
                <span className="meta-pill">{verified ? '已验证' : hasEmail ? '未验证' : '未绑定'}</span>
            </div>
            <div className="email-readonly-box">
                <span>邮箱地址</span>
                <code>{hasEmail ? savedEmail : '未绑定邮箱'}</code>
            </div>
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

                <AccountEmailCard isConsoleSession={isConsoleSession} />
                <PasswordCard isConsoleSession={isConsoleSession} />
            </div>
        </AppShell>
    )
}
