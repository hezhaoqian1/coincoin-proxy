import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { getPublicStation, setStationContext } from '../api/client'
import './StationPortal.css'

const SITE = typeof window !== 'undefined' ? window.location.origin : ''

function formatUsdCents(cents, precision = 2) {
    const value = Number(cents || 0)
    if (!value) return '$0.00'
    return `$${(value / 100).toFixed(precision)}`
}

function aliasCapabilityLabel(alias) {
    const caps = alias?.coincoin_capabilities || []
    if (caps.includes('images/generations') || caps.includes('images/edits')) return 'Images'
    if (caps.includes('embeddings')) return 'Embeddings'
    return 'Chat / Responses'
}

export default function StationPortal() {
    const { slug } = useParams()
    const [stationState, setStationState] = useState(null)
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState('')

    useEffect(() => {
        let alive = true
        setLoading(true)
        setError('')
        getPublicStation(slug)
            .then((data) => {
                if (alive) setStationState(data)
                if (data?.station?.slug) setStationContext(data.station)
            })
            .catch((err) => {
                if (alive) setError(err.message || '站点不可用')
            })
            .finally(() => {
                if (alive) setLoading(false)
            })
        return () => {
            alive = false
        }
    }, [slug])

    const station = stationState?.station || {}
    const branding = stationState?.branding || {}
    const aliases = stationState?.aliases || []
    const displayName = branding.display_name || station.display_name || slug
    const textAliases = useMemo(
        () => aliases.filter((alias) => (alias.coincoin_capabilities || []).some((cap) => ['chat/completions', 'responses'].includes(cap))),
        [aliases],
    )
    const primaryAlias = textAliases[0] || aliases[0]
    const apiBaseUrl = station.api_base_url || `${SITE}/v1`

    if (loading) {
        return (
            <main className="station-portal">
                <div className="station-portal-loading">加载站点...</div>
            </main>
        )
    }

    if (error) {
        return (
            <main className="station-portal">
                <section className="station-portal-empty">
                    <span>Station</span>
                    <h1>站点不可用</h1>
                    <p>{error}</p>
                    <Link className="btn btn-secondary" to="/">回到 CoinCoin</Link>
                </section>
            </main>
        )
    }

    return (
        <main className="station-portal">
            <section className="station-portal-hero">
                <div className="station-portal-brand">
                    {branding.logo_url ? <img src={branding.logo_url} alt="" /> : <span>{displayName.slice(0, 1).toUpperCase()}</span>}
                    <div>
                        <strong>{displayName}</strong>
                        <small>{station.portal_url || `${SITE}/s/${station.slug}`}</small>
                    </div>
                </div>
                <div className="station-portal-copy">
                    <span className="station-portal-kicker">Hosted API Station</span>
                    <h1>{displayName}</h1>
                    <p>{branding.docs_intro || '使用本站点分发的 API Key，通过 OpenAI 兼容接口调用站长发布的模型别名。'}</p>
                    <div className="station-portal-actions">
                        <Link className="btn btn-primary" to={`/register?station=${encodeURIComponent(station.slug || slug)}`}>注册并绑定本站</Link>
                        <Link className="btn btn-secondary" to={`/login?station=${encodeURIComponent(station.slug || slug)}`}>已有账号登录</Link>
                        {branding.support_link && <a className="btn btn-secondary" href={branding.support_link} target="_blank" rel="noreferrer">联系支持</a>}
                    </div>
                </div>
                <div className="station-portal-endpoints">
                    <div>
                        <span>API Base URL</span>
                        <code>{apiBaseUrl}</code>
                    </div>
                    <div>
                        <span>模型发现</span>
                        <code>GET {apiBaseUrl}/models</code>
                    </div>
                    {primaryAlias && (
                        <div>
                            <span>默认示例模型</span>
                            <code>{primaryAlias.id}</code>
                        </div>
                    )}
                </div>
            </section>

            <section className="station-portal-section">
                <div className="station-portal-section-head">
                    <div>
                        <span>Catalog</span>
                        <h2>可用别名与价格</h2>
                    </div>
                    <p>价格为站长零售配置。实际可用范围以 API Key 所属站点和平台模型目录为准。</p>
                </div>
                <div className="station-portal-models">
                    {aliases.map((alias) => (
                        <article className="station-portal-model" key={alias.id}>
                            <div>
                                <strong>{alias.id}</strong>
                                <span>{aliasCapabilityLabel(alias)}</span>
                            </div>
                            <p>目标模型由平台托管，调用时仍使用本站 alias。</p>
                            <dl>
                                <div>
                                    <dt>Input</dt>
                                    <dd>{formatUsdCents(alias.coincoin_price_input_per_million)} / 1M</dd>
                                </div>
                                <div>
                                    <dt>Output</dt>
                                    <dd>{formatUsdCents(alias.coincoin_price_output_per_million)} / 1M</dd>
                                </div>
                                <div>
                                    <dt>Image</dt>
                                    <dd>{formatUsdCents(alias.coincoin_price_per_image_cents, 3)} / image</dd>
                                </div>
                            </dl>
                        </article>
                    ))}
                    {aliases.length === 0 && (
                        <div className="station-portal-no-models">站长还没有发布模型别名。</div>
                    )}
                </div>
            </section>

            {primaryAlias && (
                <section className="station-portal-section station-portal-code">
                    <div className="station-portal-section-head">
                        <div>
                            <span>Quickstart</span>
                            <h2>OpenAI 兼容调用</h2>
                        </div>
                    </div>
                    <pre>{`curl ${apiBaseUrl}/chat/completions \\
  -H "Authorization: Bearer sk_cc_xxxxx" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${primaryAlias.id}",
    "messages": [{"role": "user", "content": "Reply with only: OK"}],
    "stream": false
  }'`}</pre>
                </section>
            )}

            <footer className="station-portal-footer">
                <span>Powered by CoinCoin</span>
                <div>
                    {branding.support_email && <a href={`mailto:${branding.support_email}`}>{branding.support_email}</a>}
                    {branding.terms_url && <a href={branding.terms_url} target="_blank" rel="noreferrer">Terms</a>}
                </div>
            </footer>
        </main>
    )
}
