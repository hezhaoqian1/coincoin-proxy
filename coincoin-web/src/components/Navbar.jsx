import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../hooks/useAuth'
import { useTheme } from '../hooks/useTheme'
import './Navbar.css'

export default function Navbar() {
    const { isLoggedIn, logout } = useAuth()
    const { theme, toggleTheme } = useTheme()
    const location = useLocation()
    const navigate = useNavigate()

    const handleLogout = () => {
        logout()
        navigate('/')
    }

    const isActive = (path) => location.pathname === path

    return (
        <nav className="navbar">
            <div className="navbar-inner container">
                <Link to="/" className="navbar-logo">
                    <div className="logo-icon">CC</div>
                    <span className="logo-text">CoinCoin</span>
                </Link>

                <div className="navbar-links">
                    {isLoggedIn ? (
                        <>
                            <Link to="/dashboard" className={`nav-link ${isActive('/dashboard') ? 'active' : ''}`}>
                                仪表盘
                            </Link>
                            <Link to="/usage" className={`nav-link ${isActive('/usage') ? 'active' : ''}`}>
                                使用明细
                            </Link>
                            <Link to="/recharge" className={`nav-link ${isActive('/recharge') ? 'active' : ''}`}>
                                充值
                            </Link>
                            <Link to="/playground" className={`nav-link ${isActive('/playground') ? 'active' : ''}`}>
                                测试
                            </Link>
                            <Link to="/docs" className={`nav-link ${isActive('/docs') ? 'active' : ''}`}>
                                文档
                            </Link>
                            <button onClick={toggleTheme} className="theme-toggle" title={theme === 'dark' ? '切换到浅色' : '切换到深色'}>
                                {theme === 'dark' ? '☀️' : '🌙'}
                            </button>
                            <button onClick={handleLogout} className="btn btn-ghost btn-sm">退出</button>
                        </>
                    ) : (
                        <>
                            <Link to="/docs" className={`nav-link ${isActive('/docs') ? 'active' : ''}`}>
                                文档
                            </Link>
                            <a href="#pricing" className="nav-link">定价</a>
                            <button onClick={toggleTheme} className="theme-toggle" title={theme === 'dark' ? '切换到浅色' : '切换到深色'}>
                                {theme === 'dark' ? '☀️' : '🌙'}
                            </button>
                            <Link to="/login" className="btn btn-secondary btn-sm">登录</Link>
                            <Link to="/register" className="btn btn-primary btn-sm">注册</Link>
                        </>
                    )}
                </div>
            </div>
        </nav>
    )
}
