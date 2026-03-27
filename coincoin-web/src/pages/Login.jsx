import { useState } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { useAuth } from '../hooks/useAuth'
import './Auth.css'

export default function Login() {
    const [tab, setTab] = useState('password')
    const [username, setUsername] = useState('')
    const [password, setPassword] = useState('')
    const [key, setKey] = useState('')
    const [error, setError] = useState('')
    const { login, loginWithPassword, loading } = useAuth()
    const navigate = useNavigate()

    const handlePasswordLogin = async (e) => {
        e.preventDefault()
        if (!username.trim()) { setError('请输入用户名'); return }
        if (!password) { setError('请输入密码'); return }
        const result = await loginWithPassword(username.trim(), password)
        if (result.success) {
            navigate('/dashboard')
        } else {
            setError(result.error)
        }
    }

    const handleKeyLogin = async (e) => {
        e.preventDefault()
        if (!key.trim()) { setError('请输入 API Key'); return }
        const result = await login(key.trim())
        if (result.success) {
            navigate('/dashboard')
        } else {
            setError(result.error)
        }
    }

    const clearError = () => setError('')

    return (
        <div className="auth-page page-wrapper">
            <div className="auth-bg">
                <div className="hero-orb hero-orb-1"></div>
                <div className="hero-orb hero-orb-2"></div>
            </div>
            <div className="auth-card glass-card animate-fade-in-up">
                <div className="auth-header">
                    <div className="logo-icon" style={{ width: 48, height: 48, fontSize: '1.1rem', borderRadius: 14 }}>CC</div>
                    <h1>登录控制台</h1>
                    <p>默认入口只有一个：先登录控制台账号。只有你已经拿到开发者 API Key 时，才需要用下面的直登入口。</p>
                </div>

                <div className="auth-flow-note">
                    <div className="auth-flow-item auth-flow-item-primary">
                        <span className="auth-flow-index">01</span>
                        <div>
                            <strong>登录控制台</strong>
                            <p>查看余额、充值、请求日志和账户状态。</p>
                        </div>
                    </div>
                    <div className="auth-flow-item">
                        <span className="auth-flow-index">02</span>
                        <div>
                            <strong>生成开发者 API Key</strong>
                            <p>真正给 Codex、OpenClaw、cURL 和服务端程序使用。</p>
                        </div>
                    </div>
                </div>

                <div className="auth-tabs">
                    <button
                        className={`auth-tab ${tab === 'password' ? 'active' : ''}`}
                        onClick={() => { setTab('password'); clearError() }}
                    >控制台登录</button>
                    <button
                        className={`auth-tab ${tab === 'key' ? 'active' : ''}`}
                        onClick={() => { setTab('key'); clearError() }}
                    >已有开发者 Key</button>
                </div>

                {tab === 'password' ? (
                    <form onSubmit={handlePasswordLogin} className="auth-form">
                        <div className="auth-callout">
                            <strong>适合需要站内管理的人</strong>
                            <p>控制台登录可以看余额、充值、查看用量，并在仪表盘生成开发者 API Key。</p>
                        </div>
                        <div className="input-group">
                            <label>用户名</label>
                            <input
                                type="text"
                                className="input-field"
                                placeholder="输入控制台用户名"
                                value={username}
                                onChange={(e) => { setUsername(e.target.value); clearError() }}
                                autoFocus
                            />
                        </div>
                        <div className="input-group">
                            <label>密码</label>
                            <input
                                type="password"
                                className="input-field"
                                placeholder="输入密码"
                                value={password}
                                onChange={(e) => { setPassword(e.target.value); clearError() }}
                            />
                            {error && <span className="input-error">{error}</span>}
                        </div>
                        <button type="submit" className="btn btn-primary" style={{ width: '100%' }} disabled={loading}>
                            {loading ? '登录中...' : '进入控制台'}
                        </button>
                        <p className="auth-helper-text">
                            还没有控制台账号？注册后先进入控制台，再在仪表盘里生成开发者 API Key。
                        </p>
                    </form>
                ) : (
                    <form onSubmit={handleKeyLogin} className="auth-form">
                        <div className="auth-callout auth-callout-muted">
                            <strong>适合已经拿到开发者 Key 的人</strong>
                            <p>这个入口只验证你的开发者 API Key 是否可用，不会自动拥有控制台账号管理能力，也不适合第一次来站的新用户。</p>
                        </div>
                        <div className="input-group">
                            <label>开发者 API Key</label>
                            <input
                                type="password"
                                className="input-field"
                                placeholder="sk_cc_xxxxxxxxxx"
                                value={key}
                                onChange={(e) => { setKey(e.target.value); clearError() }}
                                autoFocus
                            />
                            {error && <span className="input-error">{error}</span>}
                        </div>
                        <button type="submit" className="btn btn-primary" style={{ width: '100%' }} disabled={loading}>
                            {loading ? '验证中...' : '验证并进入'}
                        </button>
                        <p className="auth-helper-text">
                            如果你还没有开发者 API Key，请不要走这条路，先注册或登录控制台账号。
                        </p>
                    </form>
                )}

                <p className="auth-footer-text">
                    还没有控制台账号？<Link to="/register">立即注册</Link>
                </p>
            </div>
        </div>
    )
}
