import { useState } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { clearGeneratedKey, registerUser, setApiKey, setUserId, setUsername as storeUsername } from '../api/client'
import './Auth.css'

export default function Register() {
    const [username, setUsername] = useState('')
    const [password, setPassword] = useState('')
    const [confirmPw, setConfirmPw] = useState('')
    const [referralCode, setReferralCode] = useState(() => {
        const params = new URLSearchParams(window.location.search)
        return params.get('ref') || ''
    })
    const [error, setError] = useState('')
    const [loading, setLoading] = useState(false)
    const navigate = useNavigate()

    const handleSubmit = async (e) => {
        e.preventDefault()
        if (!username.trim()) { setError('请输入用户名'); return }
        if (password.length < 6) { setError('密码至少 6 位'); return }
        if (password !== confirmPw) { setError('两次密码不一致'); return }

        setLoading(true)
        setError('')
        try {
            const data = await registerUser(username.trim(), password, referralCode.trim() || undefined)
            clearGeneratedKey()
            setApiKey(data.session_key)
            setUserId(data.user_id)
            storeUsername(data.username)
            navigate('/dashboard')
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
                    <p>注册完成后进入控制台。开发者 Key 在概览页生成。</p>
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
                        {error && <span className="input-error">{error}</span>}
                    </div>

                    <button type="submit" className="btn btn-primary" style={{ width: '100%' }} disabled={loading}>
                        {loading ? '创建中...' : '创建账号并进入控制台'}
                    </button>

                    <p className="auth-helper-text">
                        如果你已经有开发者 Key，可以直接回登录页走 Key 直登。
                    </p>

                    <p className="auth-footer-text">
                        已有控制台账号？<Link to="/login">去登录</Link>
                    </p>
                </form>
            </div>
        </div>
    )
}
