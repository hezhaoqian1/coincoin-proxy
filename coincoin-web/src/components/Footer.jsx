import { Link } from 'react-router-dom'
import './Footer.css'

export default function Footer() {
    return (
        <footer className="footer">
            <div className="container footer-inner">
                <div className="footer-grid">
                    <div className="footer-brand">
                        <div className="footer-logo">
                            <div className="logo-icon">CC</div>
                            <span className="logo-text">CoinCoin</span>
                        </div>
                        <p className="footer-desc">
                            高性能 AI API 中转站<br />
                            公开模型目录 · 按量计费 · OpenAI 兼容
                        </p>
                    </div>

                    <div className="footer-col">
                        <h4>产品</h4>
                        <Link to="/docs">接入文档</Link>
                        <Link to="/recharge">定价方案</Link>
                        <Link to="/docs">API 文档</Link>
                    </div>

                    <div className="footer-col">
                        <h4>支持</h4>
                        <Link to="/docs">快速开始</Link>
                        <a href="#faq">常见问题</a>
                        <a href="mailto:support@coincoin.ai">联系我们</a>
                    </div>

                    <div className="footer-col">
                        <h4>模型</h4>
                        <span className="footer-model">gpt-5.2-codex / gemini-fast</span>
                        <span className="footer-model-note">支持 Gemini 文本与生图</span>
                    </div>
                </div>

                <div className="footer-bottom">
                    <span>© 2026 CoinCoin. All rights reserved.</span>
                    <div className="footer-bottom-links">
                        <a href="#">服务条款</a>
                        <a href="#">隐私政策</a>
                    </div>
                </div>
            </div>
        </footer>
    )
}
