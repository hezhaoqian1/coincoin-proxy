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

    const isActive = (path) => location.pathname === path
    const isPublicPage = ['/', '/docs', '/login', '/register', '/pay/return'].includes(location.pathname)

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

                <div className="navbar-links">
                    {isLanding ? (
                        <a href="#faq" className="nav-link">
                            FAQ
                        </a>
                    ) : null}
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
        </nav>
    )
}
