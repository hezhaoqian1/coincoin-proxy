import { useEffect, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../hooks/useAuth'
import { getStationApplication } from '../api/client'
import './Navbar.css'

export default function Navbar() {
    const { isLoggedIn } = useAuth()
    const location = useLocation()
    const pricingTarget = isLoggedIn ? '/recharge' : '/recharge'
    const isLanding = location.pathname === '/'
    const [mobileMenuOpen, setMobileMenuOpen] = useState(false)

    const isActive = (path) => location.pathname === path
    const isPublicPage = ['/', '/docs', '/login', '/register', '/pay/return'].includes(location.pathname)

    useEffect(() => {
        setMobileMenuOpen(false)
    }, [location.pathname, location.search])

    if (!isPublicPage) return null

    return (
        <nav className={`navbar ${isLanding ? 'navbar-landing' : ''}`}>
            <div className="navbar-inner container">
                <Link to="/" className="navbar-logo">
                    <div className="logo-icon">CF</div>
                    <div className="navbar-brand-copy">
                        <span className="logo-text">ClawFather</span>
                        <span className="navbar-brand-sub">API platform for builders</span>
                    </div>
                </Link>

                <div className="navbar-mobile-primary">
                    <Link to={isLoggedIn ? '/dashboard' : '/register'} className="btn btn-primary btn-sm">
                        {isLoggedIn ? '控制台' : '开始'}
                    </Link>
                    <button
                        type="button"
                        className="navbar-menu-btn"
                        aria-label={mobileMenuOpen ? '关闭导航菜单' : '打开导航菜单'}
                        aria-expanded={mobileMenuOpen}
                        onClick={() => setMobileMenuOpen((open) => !open)}
                    >
                        <span />
                        <span />
                    </button>
                </div>

                <div className="navbar-links">
                    <Link to="/docs" className={`nav-link ${isActive('/docs') ? 'active' : ''}`}>
                        文档
                    </Link>
                    <Link to={pricingTarget} className={`nav-link ${isActive('/recharge') ? 'active' : ''}`}>
                        定价
                    </Link>
                    {isLoggedIn ? (
                        <Link to="/dashboard" className="btn btn-primary btn-sm">进入控制台</Link>
                    ) : (
                        <>
                            <Link to="/login" className="btn btn-secondary btn-sm">登录</Link>
                            <Link to="/register" className="btn btn-primary btn-sm">注册</Link>
                        </>
                    )}
                </div>
            </div>
            <div className={`navbar-mobile-menu ${mobileMenuOpen ? 'open' : ''}`}>
                <div className="container navbar-mobile-menu-inner">
                    <Link to="/docs" className={`nav-link ${isActive('/docs') ? 'active' : ''}`}>文档</Link>
                    <Link to={pricingTarget} className={`nav-link ${isActive('/recharge') ? 'active' : ''}`}>定价</Link>
                    {isLoggedIn ? (
                        <Link to="/dashboard" className="nav-link">进入控制台</Link>
                    ) : (
                        <Link to="/login" className="nav-link">登录</Link>
                    )}
                </div>
            </div>
        </nav>
    )
}
