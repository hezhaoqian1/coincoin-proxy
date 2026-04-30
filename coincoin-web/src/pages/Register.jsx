import { useMemo, useState } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import {
    checkRegisterEmailCode,
    clearGeneratedKey,
    registerUser,
    sendRegisterEmailCode,
    setApiKey,
    setUserId,
    setUsername as storeUsername,
} from '../api/client'
import './Auth.css'

export default function Register() {
    const [username, setUsername] = useState('')
    const [email, setEmail] = useState('')
    const [password, setPassword] = useState('')
    const [confirmPw, setConfirmPw] = useState('')
    const [referralCode, setReferralCode] = useState(() => {
        const params = new URLSearchParams(window.location.search)
        return params.get('ref') || ''
    })
    const [code, setCode] = useState('')
    const [verificationId, setVerificationId] = useState('')
    const [verifiedEmail, setVerifiedEmail] = useState('')
    const [emailSent, setEmailSent] = useState(false)
    const [emailVerified, setEmailVerified] = useState(false)
    const [message, setMessage] = useState('')
    const [error, setError] = useState('')
    const [loading, setLoading] = useState(false)
    const [sendingCode, setSendingCode] = useState(false)
    const [verifyingCode, setVerifyingCode] = useState(false)
    const navigate = useNavigate()

    const normalizedEmail = useMemo(() => email.trim().toLowerCase(), [email])
    const emailLocked = emailVerified && verifiedEmail === normalizedEmail

    const completeLogin = (data) => {
        clearGeneratedKey()
        setApiKey(data.session_key)
        setUserId(data.user_id)
        storeUsername(data.username)
        navigate('/dashboard')
    }

    const resetVerificationState = (nextEmail = '') => {
        const normalizedNextEmail = nextEmail.trim().toLowerCase()
        if (normalizedNextEmail === verifiedEmail && emailVerified) {
            return
        }
        setVerificationId('')
        setVerifiedEmail('')
        setEmailSent(false)
        setEmailVerified(false)
        setCode('')
    }

    const validateBeforeCode = () => {
        if (!normalizedEmail) {
            setError('请输入邮箱')
            return false
        }
        return true
    }

    const handleSendCode = async () => {
        if (!validateBeforeCode()) return

        setSendingCode(true)
        setError('')
        setMessage('')
        try {
            const data = await sendRegisterEmailCode(normalizedEmail)
            setVerificationId(data.verification_id)
            setVerifiedEmail('')
            setEmailSent(true)
            setEmailVerified(false)
            setCode('')
            setMessage('验证码已发送')
        } catch (err) {
            setError(err.message || '发送失败')
        } finally {
            setSendingCode(false)
        }
    }

    const handleVerifyCode = async () => {
        if (!verificationId) {
            setError('请先发送验证码')
            return
        }
        if (!code.trim()) {
            setError('请输入验证码')
            return
        }

        setVerifyingCode(true)
        setError('')
        setMessage('')
        try {
            const data = await checkRegisterEmailCode(verificationId, code.trim())
            setVerificationId(data.verification_id)
            setVerifiedEmail(data.email)
            setEmailVerified(true)
            setEmailSent(true)
            setMessage('邮箱已验证')
        } catch (err) {
            setError(err.message || '验证失败')
        } finally {
            setVerifyingCode(false)
        }
    }

    const handleSubmit = async (e) => {
        e.preventDefault()
        if (!username.trim()) { setError('请输入用户名'); return }
        if (!normalizedEmail) { setError('请输入邮箱'); return }
        if (!emailLocked) { setError('请先完成邮箱验证'); return }
        if (password.length < 6) { setError('密码至少 6 位'); return }
        if (password !== confirmPw) { setError('两次密码不一致'); return }

        setLoading(true)
        setError('')
        setMessage('')
        try {
            const data = await registerUser(
                username.trim(),
                normalizedEmail,
                password,
                referralCode.trim() || undefined,
                verificationId,
            )
            completeLogin(data)
        } catch (err) {
            setError(err.message || '注册失败，请重试')
        } finally {
            setLoading(false)
        }
    }

    return (
        <div className="auth-page page-wrapper">
            <div className="auth-bg">
                <div className="auth-grid"></div>
            </div>
            <div className="auth-card glass-card animate-fade-in-up">
                <div className="auth-header">
                    <div className="logo-icon" style={{ width: 48, height: 48, fontSize: '1.1rem', borderRadius: 14 }}>CF</div>
                    <h1>创建控制台账号</h1>
                    <p>先验证邮箱，再完成注册。开发者 Key 在概览页生成。</p>
                </div>

                <form onSubmit={handleSubmit} className="auth-form">
                    <div className="input-group">
                        <label>用户名</label>
                        <input
                            type="text"
                            className="input-field"
                            placeholder="字母、数字、下划线、连字符"
                            value={username}
                            onChange={(e) => { setUsername(e.target.value); setError('') }}
                            autoFocus
                        />
                    </div>

                    <div className="input-group">
                        <label>邮箱</label>
                        <div className="input-inline-group">
                            <input
                                type="email"
                                className="input-field"
                                placeholder="name@example.com"
                                value={email}
                                onChange={(e) => {
                                    setEmail(e.target.value)
                                    setError('')
                                    setMessage('')
                                    resetVerificationState(e.target.value)
                                }}
                                disabled={emailLocked}
                            />
                            <button
                                type="button"
                                className="btn btn-secondary auth-inline-button"
                                onClick={handleSendCode}
                                disabled={sendingCode || emailLocked}
                            >
                                {emailLocked ? '已验证' : sendingCode ? '发送中...' : (emailSent ? '重新发送' : '发送验证码')}
                            </button>
                        </div>
                    </div>

                    <div className="input-group">
                        <label>验证码</label>
                        <div className="input-inline-group">
                            <input
                                type="text"
                                inputMode="numeric"
                                className="input-field verification-code-input"
                                placeholder="6 位数字"
                                value={code}
                                onChange={(e) => {
                                    setCode(e.target.value.replace(/\D/g, '').slice(0, 6))
                                    setError('')
                                    setMessage('')
                                }}
                                disabled={emailLocked}
                            />
                            <button
                                type="button"
                                className="btn btn-secondary auth-inline-button"
                                onClick={handleVerifyCode}
                                disabled={verifyingCode || emailLocked || !emailSent}
                            >
                                {emailLocked ? '已通过' : verifyingCode ? '验证中...' : '验证'}
                            </button>
                        </div>
                    </div>

                    {emailLocked && (
                        <div className="auth-callout auth-callout-muted">
                            <strong>{verifiedEmail}</strong>
                            <p>邮箱已验证，可以继续创建账号。</p>
                        </div>
                    )}

                    <div className="input-group">
                        <label>密码</label>
                        <input
                            type="password"
                            className="input-field"
                            placeholder="至少 6 位"
                            value={password}
                            onChange={(e) => { setPassword(e.target.value); setError('') }}
                        />
                    </div>

                    <div className="input-group">
                        <label>确认密码</label>
                        <input
                            type="password"
                            className="input-field"
                            placeholder="再输入一次密码"
                            value={confirmPw}
                            onChange={(e) => { setConfirmPw(e.target.value); setError('') }}
                        />
                    </div>

                    <div className="input-group">
                        <label>邀请码 <span style={{ color: 'var(--text-tertiary)', fontWeight: 400 }}>（选填）</span></label>
                        <input
                            type="text"
                            className="input-field"
                            placeholder="有邀请码就填，没有可留空"
                            value={referralCode}
                            onChange={(e) => setReferralCode(e.target.value.toUpperCase())}
                            style={{ textTransform: 'uppercase', letterSpacing: '0.1em' }}
                        />
                    </div>

                    {(error || message) && (
                        <div className="auth-status-stack">
                            {error && <span className="input-error">{error}</span>}
                            {message && <span className="input-success">{message}</span>}
                        </div>
                    )}

                    <button type="submit" className="btn btn-primary" style={{ width: '100%' }} disabled={loading || !emailLocked}>
                        {loading ? '创建中...' : '创建账号'}
                    </button>

                    <p className="auth-helper-text">
                        使用主流邮箱，后续找回账号和风控都靠它。
                    </p>

                    <p className="auth-footer-text">
                        已有控制台账号？<Link to="/login">去登录</Link>
                    </p>
                </form>
            </div>
        </div>
    )
}
