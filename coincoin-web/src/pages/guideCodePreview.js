export function getGuideCodePreview(code, secret) {
    if (!secret) return code
    return code.split(secret).join('YOUR_DEVELOPER_API_KEY')
}
