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
                        <div className="footer-logo">
                            <div className="logo-icon">CF</div>
                            <span className="logo-text">ClawFather</span>
                        </div>
                        <p className="footer-desc">
                            开发者控制台<br />
                            模型目录 · 统一余额 · OpenAI 与 Claude Code
                        </p>
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
                        <Link to="/#faq">常见问题</Link>
                        <Link to="/docs">接口说明</Link>
                    </div>

                    <div className="footer-col">
                        <h4>模型</h4>
                        <span className="footer-model">gpt-5.5 / claude-opus-4-7 / gemini-fast</span>
                        <span className="footer-model-note">文本与图片统一接入</span>
                    </div>
                </div>

                <div className="footer-bottom">
                    <span>© 2026 ClawFather. All rights reserved.</span>
                    <div className="footer-bottom-links">
                        <a href="#">服务条款</a>
                        <a href="#">隐私政策</a>
                    </div>
                </div>
            </div>
        </footer>
    )
}
