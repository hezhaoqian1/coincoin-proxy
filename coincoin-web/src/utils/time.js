const ISO_WITHOUT_TIMEZONE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?$/

export function parseBackendTimestamp(value) {
    if (!value) return null
    if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value

    const raw = String(value).trim()
    if (!raw) return null

    const normalized = ISO_WITHOUT_TIMEZONE.test(raw) ? `${raw}Z` : raw
    const date = new Date(normalized)
    return Number.isNaN(date.getTime()) ? null : date
}

export function formatChinaTime(value, options = {}) {
    const { emptyText = '未知', ...dateTimeOptions } = options
    const date = parseBackendTimestamp(value)
    if (!date) return emptyText

    return date.toLocaleString('zh-CN', {
        timeZone: 'Asia/Shanghai',
        hour12: false,
        ...dateTimeOptions,
    })
}
