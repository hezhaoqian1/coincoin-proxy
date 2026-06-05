import { Link } from 'react-router-dom'
import { useAuth } from '../hooks/useAuth'
import './Footer.css'

export default function Footer() {
    const { isLoggedIn } = useAuth()
    const pricingTarget = '/recharge'

    return (
        <footer className="footer">
            <div className="container footer-inner">
                <div className="footer-grid">
                    <div className="footer-brand">
                        <span className="footer-kicker">ClawFather API</span>
                        <div className="footer-logo">
                            <div className="logo-icon">CF</div>
                            <div className="footer-brand-copy">
                                <span className="logo-text">ClawFather</span>
                                <span className="footer-brand-sub">One console for multi-model access</span>
                            </div>
                        </div>
                    </div>

                    <div className="footer-col">
                        <h4>产品</h4>
                        <Link to="/docs">接入文档</Link>
                        <Link to={pricingTarget}>定价方案</Link>
                        <Link to="/docs">API 文档</Link>
                    </div>

                    <div className="footer-col">
                        <h4>支持</h4>
                        <Link to="/docs">快速开始</Link>
                        <Link to="/docs">接口说明</Link>
                    </div>

                    <div className="footer-col">
                        <h4>模型</h4>
                        <span className="footer-model">opus / sonnet / haiku / gpt-image-2 / gemini-image</span>
                        <span className="footer-model-note">文本与图片统一接入</span>
                    </div>
                </div>

                <div className="footer-bottom">
                    <span>© 2026 ClawFather. API platform for builders.</span>
                    <div className="footer-bottom-links">
                        <a href="#">服务条款</a>
                        <a href="#">隐私政策</a>
                    </div>
                </div>
            </div>
        </footer>
    )
}
