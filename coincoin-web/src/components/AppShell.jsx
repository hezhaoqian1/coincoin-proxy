import { useEffect, useMemo, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { getStationApplication } from '../api/client'
import { useAuth } from '../hooks/useAuth'
import { useTheme } from '../hooks/useTheme'
import './AppShell.css'

function ShellIcon({ kind }) {
    const common = {
        viewBox: '0 0 24 24',
        fill: 'none',
        stroke: 'currentColor',
        strokeWidth: '1.85',
        strokeLinecap: 'round',
        strokeLinejoin: 'round',
        'aria-hidden': 'true',
    }

    switch (kind) {
        case 'access':
            return (
                <svg {...common}>
                    <path d="M12 3v18" />
                    <path d="M7 8h10" />
                    <path d="M7 16h10" />
                    <path d="M5 12h14" />
                </svg>
            )
        case 'logs':
            return (
                <svg {...common}>
                    <path d="M5 6h14" />
                    <path d="M5 12h14" />
                    <path d="M5 18h10" />
                </svg>
            )
        case 'playground':
            return (
                <svg {...common}>
                    <path d="M5 19.5 19.5 12 5 4.5v6l10 1.5L5 13.5z" />
                </svg>
            )
        case 'docs':
            return (
                <svg {...common}>
                    <path d="M7 4.5h8a3 3 0 0 1 3 3v12H10a3 3 0 0 0-3 3z" />
                    <path d="M7 4.5a3 3 0 0 0-3 3v12h6" />
                </svg>
            )
        case 'billing':
            return (
                <svg {...common}>
                    <path d="M12 2v20" />
                    <path d="M17 6.5c0-1.7-2.2-3-5-3s-5 1.3-5 3 1.4 2.5 5 3 5 1.3 5 3-2.2 3-5 3-5-1.3-5-3" />
                </svg>
            )
        case 'key':
            return (
                <svg {...common}>
                    <path d="M14.5 10.5a3.5 3.5 0 1 0-3.3-4.7L3 14v4h4l1.8-1.8H11v-2.2l1.8-1.8a3.5 3.5 0 0 0 1.7.3Z" />
                    <path d="M16.5 7.5h.01" />
                </svg>
            )
        case 'pricing':
            return (
                <svg {...common}>
                    <path d="M7 7h10" />
                    <path d="M7 12h10" />
                    <path d="M7 17h6" />
                    <path d="M4 4h16v16H4z" />
                </svg>
            )
        case 'order':
            return (
                <svg {...common}>
                    <path d="M7 4h10l2 3v13H5V7z" />
                    <path d="M7 4v3h10V4" />
                    <path d="M9 12h6" />
                    <path d="M9 16h4" />
                </svg>
            )
        case 'redeem':
            return (
                <svg {...common}>
                    <path d="M20 12v7H4v-7" />
                    <path d="M12 4v15" />
                    <path d="M7 9.5 12 4l5 5.5" />
                </svg>
            )
        case 'terminal':
            return (
                <svg {...common}>
                    <path d="M4.5 6.5 9 11l-4.5 4.5" />
                    <path d="M11.5 16.5H19.5" />
                </svg>
            )
        case 'station':
            return (
                <svg {...common}>
                    <path d="M4 10.5 12 4l8 6.5" />
                    <path d="M6.5 9.5V19h11V9.5" />
                    <path d="M10 19v-5h4v5" />
                </svg>
            )
        case 'moon':
            return (
                <svg {...common}>
                    <path d="M18 14.5A7.5 7.5 0 1 1 9.5 6 6 6 0 0 0 18 14.5z" />
                </svg>
            )
        case 'sun':
            return (
                <svg {...common}>
                    <circle cx="12" cy="12" r="4" />
                    <path d="M12 2.5v2.5M12 19v2.5M21.5 12H19M5 12H2.5M18.7 5.3l-1.8 1.8M7.1 16.9l-1.8 1.8M18.7 18.7l-1.8-1.8M7.1 7.1 5.3 5.3" />
                </svg>
            )
        case 'logout':
            return (
                <svg {...common}>
                    <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
                    <path d="M16 17l5-5-5-5" />
                    <path d="M21 12H9" />
                </svg>
            )
        case 'dashboard':
        default:
            return (
                <svg {...common}>
                    <path d="M4 5h7v6H4z" />
                    <path d="M13 5h7v10h-7z" />
                    <path d="M4 13h7v6H4z" />
                    <path d="M13 17h7v2h-7z" />
                </svg>
            )
    }
}

function isNavItemActive(item, location) {
    if (location.pathname !== item.pathname) return false
    if (item.search) {
        const params = new URLSearchParams(location.search)
        return Object.entries(item.search).every(([key, value]) => params.get(key) === value)
    }
    if (item.hash) {
        return location.hash === item.hash
    }
    return !location.search || location.pathname === '/dashboard' || location.pathname === '/usage' || location.pathname === '/station'
}

function ShellGroup({ title, items, location }) {
    return (
        <div className="shell-group">
            <div className="shell-group-title">{title}</div>
            <div className="shell-group-items">
                {items.map((item) => (
                    <Link
                        key={item.to}
                        to={item.to}
                        className={`shell-link ${isNavItemActive(item, location) ? 'active' : ''}`}
                    >
                        <span className="shell-link-icon"><ShellIcon kind={item.icon} /></span>
                        <span className="shell-link-copy">
                            <span className="shell-link-label">{item.label}</span>
                            {item.caption ? <span className="shell-link-caption">{item.caption}</span> : null}
                        </span>
                    </Link>
                ))}
            </div>
        </div>
    )
}

export default function AppShell({ title, description, actions, children }) {
    const { authMode, hasDeveloperKey, logout, username } = useAuth()
    const { theme, toggleTheme } = useTheme()
    const navigate = useNavigate()
    const location = useLocation()
    const [hasStation, setHasStation] = useState(false)

    useEffect(() => {
        let active = true
        getStationApplication()
            .then((data) => {
                if (!active) return
                setHasStation(Boolean(data?.station && data.station.status === 'active'))
            })
            .catch(() => {
                if (!active) return
                setHasStation(false)
            })
        return () => {
            active = false
        }
    }, [])

    const accountLabel = authMode === 'api' ? '开发者 Key 会话' : (username || '控制台账号')
    const accountSub = hasDeveloperKey ? '已具备真实调用权限' : '先生成开发者 Key'
    const navGroups = useMemo(() => {
        const groups = [
            {
                title: '工作台',
                items: [
                    { to: '/dashboard', pathname: '/dashboard', label: '控制台', caption: '余额、密钥、最近请求', icon: 'dashboard' },
                    { to: '/settings?panel=keys', pathname: '/settings', search: { panel: 'keys' }, label: 'API 密钥', caption: '复制密钥，确认统一入口', icon: 'key' },
                    { to: '/usage', pathname: '/usage', label: '使用记录', caption: '状态码、计量、路由日志', icon: 'logs' },
                    { to: '/docs?tab=models', pathname: '/docs', search: { tab: 'models' }, label: '模型价格', caption: '公开目录、计费和默认模型', icon: 'pricing' },
                ],
            },
            {
                title: '资金',
                items: [
                    { to: '/recharge?section=recharge', pathname: '/recharge', search: { section: 'recharge' }, label: '充值', caption: '套餐、自定义金额、支付', icon: 'billing' },
                    { to: '/recharge?section=orders', pathname: '/recharge', search: { section: 'orders' }, label: '我的订单', caption: '最近订单、到账状态、回跳', icon: 'order' },
                    { to: '/recharge?section=redeem', pathname: '/recharge', search: { section: 'redeem' }, label: '兑换', caption: '活动码、内部码、补额度', icon: 'redeem' },
                ],
            },
            {
                title: '教程',
                items: [
                    { to: '/docs?tab=quickstart', pathname: '/docs', search: { tab: 'quickstart' }, label: '默认 API 教程', caption: '先跑通第一条请求', icon: 'docs' },
                    { to: '/settings?snippet=Codex%20CLI%20(config.toml)&panel=snippets', pathname: '/settings', search: { snippet: 'Codex CLI (config.toml)', panel: 'snippets' }, label: 'Codex 配置', caption: 'config.toml 与环境变量', icon: 'terminal' },
                    { to: '/settings?snippet=Claude%20Code&panel=snippets', pathname: '/settings', search: { snippet: 'Claude Code', panel: 'snippets' }, label: 'Claude Code 配置', caption: 'ANTHROPIC_BASE_URL 与模型名', icon: 'access' },
                ],
            },
        ]

        if (hasStation) {
            groups.push({
                title: '分发',
                items: [
                    { to: '/station', pathname: '/station', label: '站长中心', caption: '下游用户、分润、结算', icon: 'station' },
                ],
            })
        }

        return groups
    }, [hasStation])

    const handleLogout = () => {
        logout()
        navigate('/')
    }

    return (
        <div className="app-shell">
            <aside className="app-sidebar">
                <div className="app-sidebar-top">
                    <Link to="/dashboard" className="app-sidebar-brand">
                        <div className="logo-icon">CF</div>
                        <div className="app-sidebar-brand-copy">
                            <strong>ClawFather</strong>
                            <span>中转站控制台</span>
                        </div>
                    </Link>

                    <div className="app-sidebar-badge-row">
                        <span className="app-sidebar-badge">OpenAI / Anthropic 兼容</span>
                    </div>

                    <div className="app-sidebar-session">
                        <div className="app-sidebar-session-eyebrow">当前会话</div>
                        <div className="app-sidebar-session-title">{accountLabel}</div>
                        <div className="app-sidebar-session-sub">{accountSub}</div>
                    </div>
                </div>

                <nav className="app-sidebar-nav">
                    {navGroups.map((group) => (
                        <ShellGroup key={group.title} title={group.title} items={group.items} location={location} />
                    ))}
                </nav>

                <div className="app-sidebar-footer">
                    <button onClick={toggleTheme} className="shell-action-btn" title={theme === 'dark' ? '切换到浅色' : '切换到深色'}>
                        <span className="shell-action-icon"><ShellIcon kind={theme === 'dark' ? 'sun' : 'moon'} /></span>
                        <span>{theme === 'dark' ? '浅色模式' : '深色模式'}</span>
                    </button>
                    <button onClick={handleLogout} className="shell-action-btn shell-action-btn-muted">
                        <span className="shell-action-icon"><ShellIcon kind="logout" /></span>
                        <span>退出登录</span>
                    </button>
                </div>
            </aside>

            <div className="app-main">
                <main className="app-main-content">
                    {(title || description || actions) ? (
                        <section className="app-page-intro">
                            <div className="app-page-intro-copy">
                                {title ? <h1 className="app-page-intro-title">{title}</h1> : null}
                                {description ? <p className="app-page-intro-desc">{description}</p> : null}
                            </div>
                            {actions ? <div className="app-page-intro-actions">{actions}</div> : null}
                        </section>
                    ) : null}
                    {children}
                </main>
            </div>
        </div>
    )
}
