const ISO_WITHOUT_TIMEZONE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?$/
const CHINA_DATE_FORMATTER = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
})

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

export function formatLocalTime(value, options = {}) {
    const { emptyText = '未知', ...dateTimeOptions } = options
    const date = parseBackendTimestamp(value)
    if (!date) return emptyText

    return date.toLocaleString('zh-CN', {
        hour12: false,
        ...dateTimeOptions,
    })
}

export function getChinaIsoDate(value = new Date()) {
    const date = parseBackendTimestamp(value)
    if (!date) return ''
    return CHINA_DATE_FORMATTER.format(date)
}

export function getLocalIsoDate(value = new Date()) {
    const date = parseBackendTimestamp(value)
    if (!date) return ''
    const year = date.getFullYear()
    const month = String(date.getMonth() + 1).padStart(2, '0')
    const day = String(date.getDate()).padStart(2, '0')
    return `${year}-${month}-${day}`
}

function parseLocalDateInput(value) {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || '').trim())
    if (!match) return null
    const year = Number(match[1])
    const monthIndex = Number(match[2]) - 1
    const day = Number(match[3])
    const date = new Date(year, monthIndex, day)
    if (
        date.getFullYear() !== year ||
        date.getMonth() !== monthIndex ||
        date.getDate() !== day
    ) {
        return null
    }
    return date
}

export function getLocalDateRangeIso(dateString) {
    const start = parseLocalDateInput(dateString)
    if (!start) return null

    const exclusiveEnd = new Date(start)
    exclusiveEnd.setDate(exclusiveEnd.getDate() + 1)

    return {
        start: start.toISOString(),
        end: exclusiveEnd.toISOString(),
    }
}

export function getLocalTodayRangeIso(value = new Date()) {
    return getLocalDateRangeIso(getLocalIsoDate(value))
}

export function getRecentLocalIsoDates(days = 7, value = new Date()) {
    const count = Math.max(1, Number(days) || 1)
    const end = parseBackendTimestamp(value)
    if (!end) return []

    const dates = []
    for (let index = count - 1; index >= 0; index -= 1) {
        const date = new Date(end)
        date.setDate(date.getDate() - index)
        dates.push(getLocalIsoDate(date))
    }
    return dates
}
