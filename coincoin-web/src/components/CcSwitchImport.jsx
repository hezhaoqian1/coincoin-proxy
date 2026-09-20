import { useEffect, useId, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { buildCcSwitchLink, CC_SWITCH_CLIENTS, CC_SWITCH_DOWNLOAD_URL, getCcSwitchEndpoint, isFullDeveloperKey } from '../utils/ccSwitch'
import './CcSwitchImport.css'

export default function CcSwitchImport({ apiKey, keyName = '', initialApp = 'codex', disabledReason = '', label = '导入 CC Switch' }) {
    const [open, setOpen] = useState(false)
    const reason = disabledReason || (!isFullDeveloperKey(apiKey) ? '没有完整 Key，请在 API 密钥页获取或新建开发者 Key' : '')

    return (
        <>
            <button type="button" className="btn btn-primary btn-sm" disabled={!!reason} title={reason || '将此 Key 配置到 Codex 或 Claude Code'} onClick={() => setOpen(true)}>
                {label}
            </button>
            {open && <ImportDialog apiKey={apiKey} keyName={keyName} initialApp={initialApp} disabledReason={reason} onClose={() => setOpen(false)} />}
        </>
    )
}

function ImportDialog({ apiKey, keyName, initialApp, disabledReason, onClose }) {
    const dialog = useRef(null)
    const titleId = useId()
    const [app, setApp] = useState(initialApp)
    const [message, setMessage] = useState('')
    const [error, setError] = useState('')
    const client = CC_SWITCH_CLIENTS[app]
    const baseUrl = window.location.origin
    const endpoint = getCcSwitchEndpoint(baseUrl, app)

    useEffect(() => {
        const element = dialog.current
        element.showModal()
        return () => element.close()
    }, [])

    const launch = () => {
        setError('')
        if (disabledReason) return
        try {
            const link = buildCcSwitchLink({ app, apiKey, baseUrl, keyName })
            window.open(link, '_self')
            setMessage('已请求打开 CC Switch。请在客户端确认导入，再启用该供应商；网页无法判断导入是否完成。')
        } catch {
            setError('未能打开 CC Switch。请确认已安装并启动客户端，或使用下方手动配置。')
        }
    }

    const copy = async (value, name) => {
        try {
            await navigator.clipboard.writeText(value)
            setError('')
            setMessage(`已复制${name}`)
        } catch {
            setError('复制失败，请允许浏览器访问剪贴板后重试。')
        }
    }

    return (
        <dialog ref={dialog} className="cc-switch-dialog" aria-labelledby={titleId} onCancel={onClose} onClose={(event) => { if (!event.currentTarget.open) onClose() }}>
            <div className="cc-switch-dialog-head">
                <div><span className="cc-switch-eyebrow">CC SWITCH</span><h2 id={titleId}>把 Key 配置到客户端</h2></div>
                <button type="button" className="btn btn-ghost btn-sm" aria-label="关闭配置窗口" onClick={onClose}>关闭</button>
            </div>
            <p className="cc-switch-muted">选择工具，地址、完整 Key 和默认模型会一起带入 CC Switch。</p>
            <fieldset className="cc-switch-client-options">
                <legend>选择客户端</legend>
                {Object.entries(CC_SWITCH_CLIENTS).map(([value, option]) => (
                    <label key={value} className={app === value ? 'is-selected' : ''}>
                        <input type="radio" name={titleId} value={value} checked={app === value} onChange={() => { setApp(value); setMessage(''); setError('') }} />
                        <span><strong>{option.label}</strong><small>{option.model}</small></span>
                    </label>
                ))}
            </fieldset>
            <dl className="cc-switch-preview">
                <div><dt>供应商</dt><dd>ClawFather{keyName ? ` · ${keyName}` : ''}</dd></div>
                <div><dt>Base URL</dt><dd><code>{endpoint}</code></dd></div>
                <div><dt>API Key</dt><dd><code>{apiKey.slice(0, 8)}••••{apiKey.slice(-4)}</code></dd></div>
                <div><dt>模型</dt><dd><code>{client.model}</code></dd></div>
            </dl>
            <div className="cc-switch-actions">
                <button type="button" className="btn btn-primary" disabled={!!disabledReason} onClick={launch}>一键配置到 {client.label}</button>
                <a className="btn btn-secondary" href={CC_SWITCH_DOWNLOAD_URL} target="_blank" rel="noopener noreferrer">下载 CC Switch</a>
            </div>
            {disabledReason && <p className="cc-switch-error" role="alert">{disabledReason}</p>}
            {message && <p className="cc-switch-feedback" role="status">{message}</p>}
            {error && <p className="cc-switch-error" role="alert">{error}</p>}
            <details className="cc-switch-fallback">
                <summary>点击后没反应？手动配置</summary>
                <p>先安装并打开 CC Switch，再重试并允许浏览器打开应用。也可以在 CC Switch 的 {client.label} 页新建供应商，填写上面的地址和模型。</p>
                <div className="cc-switch-actions">
                    <button type="button" className="btn btn-secondary btn-sm" onClick={() => copy(endpoint, '地址')}>复制地址</button>
                    <button type="button" className="btn btn-secondary btn-sm" disabled={!!disabledReason} onClick={() => copy(apiKey, '完整 Key')}>复制完整 Key</button>
                    <Link className="btn btn-ghost btn-sm" to={client.guide} onClick={onClose}>查看完整教程</Link>
                </div>
            </details>
            <p className="cc-switch-note">配置会把此 Key 交给本机 CC Switch。确认导入后，启用供应商并重新启动 {client.label}。</p>
        </dialog>
    )
}
