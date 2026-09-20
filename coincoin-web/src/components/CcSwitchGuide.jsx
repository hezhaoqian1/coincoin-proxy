import { Link } from 'react-router-dom'
import CcSwitchImport from './CcSwitchImport'
import { CC_SWITCH_CLIENTS, CC_SWITCH_DOWNLOAD_URL, getCcSwitchEndpoint } from '../utils/ccSwitch'
import './CcSwitchGuide.css'

export default function CcSwitchGuide({ app, apiKey, loading }) {
    const client = CC_SWITCH_CLIENTS[app]
    return (
        <section className="cc-switch-guide glass-card" id="cc-switch">
            <div className="cc-switch-guide-heading">
                <div><span className="cc-switch-eyebrow">推荐方式 · CC SWITCH</span><h2>点一下，把 {client.label} 配好</h2><p>用 CC Switch 管理供应商，省去手动填写地址和 Key。</p></div>
                <CcSwitchImport apiKey={apiKey} initialApp={app} disabledReason={loading ? '正在加载开发者 Key' : ''} label={`一键配置 ${client.label}`} />
            </div>
            <ol className="cc-switch-guide-steps">
                <li><strong>安装并打开</strong><p><a href={CC_SWITCH_DOWNLOAD_URL} target="_blank" rel="noopener noreferrer">下载 CC Switch</a>，选择适合你的 macOS、Windows 或 Linux 安装包。还需按官方指引安装 <a href={app === 'codex' ? 'https://github.com/openai/codex#quickstart' : 'https://code.claude.com/docs/en/setup'} target="_blank" rel="noopener noreferrer">{client.label}</a> 本身。</p></li>
                <li><strong>一键带入配置</strong><p>点击上方按钮，选择 {client.label}，允许浏览器打开 CC Switch。要使用特定 Key，可在 <Link to="/api-keys">API 密钥</Link> 页面从对应条目导入。</p></li>
                <li><strong>确认并启用</strong><p>检查供应商、地址和模型，确认导入，再在 {client.label} 页启用 ClawFather。重新启动终端中的 {app === 'codex' ? 'codex' : 'claude'}。</p></li>
            </ol>
            <div className="cc-switch-guide-check">
                <div><strong>检查配置</strong><p>Base URL：<code>{getCcSwitchEndpoint(window.location.origin, app)}</code><br />默认模型：<code>{client.model}</code></p></div>
                <div><strong>验证是否成功</strong><p>发送「只回复 OK」，收到回复后到 <Link to="/usage">使用记录</Link> 核对模型和请求。正常请求按站点规则计费。</p></div>
            </div>
            <details className="cc-switch-fallback">
                <summary>常见问题</summary>
                <ul>
                    <li>没有弹出客户端：安装并启动 CC Switch，允许浏览器打开外部应用，然后重试；配置窗口也提供手动复制。</li>
                    <li>导入后仍使用旧供应商：在 CC Switch 中启用刚导入的配置，并重启 {client.label}。已有终端环境变量或项目配置可能覆盖客户端设置。</li>
                    <li>提示 401：确认选择了可用的完整开发者 Key；额度不足或 Key 过期时先在 API 密钥页处理。</li>
                    {app === 'claude' && <li>Claude Code 的地址不要加 /v1；如果仍走原来的托管登录，先退出旧登录，再启用供应商并重启。</li>}
                </ul>
            </details>
        </section>
    )
}
