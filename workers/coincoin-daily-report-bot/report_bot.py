#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from PIL import Image, ImageDraw, ImageFont


API_BASE = os.environ.get("COINCOIN_API_BASE", "https://clawfather.up.railway.app").rstrip("/")
TARGET_CHANNEL = os.environ.get("SLOCK_REPORT_CHANNEL", "#coincoin数据")
OUTPUT_DIR = Path(os.environ.get("REPORT_OUTPUT_DIR", Path(__file__).resolve().parent / "output"))


def money(cents: Optional[int]) -> str:
    cents = int(cents or 0)
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def intfmt(value: Optional[int]) -> str:
    return f"{int(value or 0):,}"


def compact_int(value: Optional[int]) -> str:
    value = int(value or 0)
    if abs(value) >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def pct(value: float) -> str:
    return f"{value:.2f}%"


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size, index=1 if bold and path.endswith(".ttc") else 0)
            except Exception:
                continue
    return ImageFont.load_default()


FONT_TITLE = load_font(44, True)
FONT_H1 = load_font(28, True)
FONT_H2 = load_font(22, True)
FONT_BODY = load_font(20)
FONT_SMALL = load_font(16)
FONT_MONO = load_font(18)


def fetch_json(path: str, token: str, timeout: int = 20) -> Any:
    url = f"{API_BASE}{path}"
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            response = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
            break
        except requests.RequestException as exc:
            last_error = exc
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))
    else:
        raise RuntimeError(f"GET {path} failed: {last_error}")
    content_type = response.headers.get("content-type", "")
    if response.status_code != 200:
        raise RuntimeError(f"GET {path} failed: HTTP {response.status_code} {response.text[:200]}")
    if "application/json" not in content_type:
        raise RuntimeError(f"GET {path} returned non-JSON content-type {content_type}")
    return response.json()


def fetch_daily_usage(day: datetime, token: str) -> Optional[Dict[str, Any]]:
    day_s = day.strftime("%Y-%m-%d")
    try:
        rows = fetch_json(f"/admin/usage/daily?day={day_s}", token, timeout=10)
        if not isinstance(rows, list):
            return None
        return {
            "day": day_s,
            "requests_total": sum(int(row.get("requests_total") or 0) for row in rows),
            "user_charge_cents": sum(int(row.get("cost_cents") or 0) for row in rows),
            "tokens_total": sum(int(row.get("tokens_total") or 0) for row in rows),
            "images_total": sum(int(row.get("images_total") or 0) for row in rows),
            "active_users": len({row.get("user_id") for row in rows if row.get("user_id")}),
        }
    except Exception:
        return None


def fetch_trend_data(token: str, days: int = 7) -> List[Dict[str, Any]]:
    today = datetime.now()
    items: List[Dict[str, Any]] = []
    for offset in range(days - 1, -1, -1):
        item = fetch_daily_usage(today - timedelta(days=offset), token)
        if item:
            items.append(item)
    return items


def fetch_report_data(token: str) -> Dict[str, Any]:
    return {
        "overview": fetch_json("/admin/analytics/overview", token),
        "top_users": fetch_json("/admin/analytics/top-users?period=7d&metric=cost_cents&limit=10", token),
        "low_balance": fetch_json("/admin/analytics/low-balance-users?period=7d&limit=10", token),
        "errors": fetch_json("/admin/analytics/errors?period=today&limit=10", token),
        "trend": fetch_trend_data(token),
    }


def positive_balance_users_label(overview: Dict[str, Any]) -> Tuple[str, str]:
    for key in ("positive_balance_users", "users_with_balance", "balance_positive_users"):
        if key in overview:
            return intfmt(overview.get(key)), "当前有可用余额的用户"
    return "待补", "当前接口仅有总注册用户，需补有余额用户聚合"


def draw_text(draw: ImageDraw.ImageDraw, xy, text: str, font, fill="#111827", max_width: Optional[int] = None):
    if not max_width:
        draw.text(xy, text, font=font, fill=fill)
        return
    words = list(text)
    lines: List[str] = []
    line = ""
    for word in words:
        candidate = line + word
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            line = candidate
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    x, y = xy
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += font.size + 6


def card(draw: ImageDraw.ImageDraw, box, title: str, value: str, subtitle: str, accent="#2563eb"):
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(box, radius=18, fill="#ffffff", outline="#e5e7eb", width=2)
    draw.rectangle((x1, y1, x1 + 7, y2), fill=accent)
    draw_text(draw, (x1 + 26, y1 + 22), title, FONT_SMALL, fill="#6b7280")
    draw_text(draw, (x1 + 26, y1 + 58), value, FONT_H1, fill="#111827")
    draw_text(draw, (x1 + 26, y1 + 98), subtitle, FONT_SMALL, fill="#6b7280", max_width=x2 - x1 - 52)


def table(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, title: str, headers: List[str], rows: List[List[str]], col_widths: List[int]):
    draw_text(draw, (x, y), title, FONT_H2, fill="#111827")
    y += 38
    draw.rounded_rectangle((x, y, x + w, y + 48), radius=12, fill="#eef2ff")
    cx = x + 16
    for i, header in enumerate(headers):
        draw_text(draw, (cx, y + 13), header, FONT_SMALL, fill="#374151")
        cx += col_widths[i]
    y += 54
    for idx, row in enumerate(rows):
        bg = "#ffffff" if idx % 2 == 0 else "#f9fafb"
        draw.rounded_rectangle((x, y, x + w, y + 44), radius=8, fill=bg)
        cx = x + 16
        for i, cell in enumerate(row):
            draw_text(draw, (cx, y + 11), str(cell), FONT_SMALL, fill="#111827", max_width=col_widths[i] - 12)
            cx += col_widths[i]
        y += 46


def draw_line_chart(
    draw: ImageDraw.ImageDraw,
    box: Tuple[int, int, int, int],
    title: str,
    series: List[Dict[str, Any]],
    keys: List[Tuple[str, str, str]],
    value_formatter=intfmt,
) -> None:
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(box, radius=18, fill="#ffffff", outline="#e5e7eb", width=2)
    draw_text(draw, (x1 + 24, y1 + 18), title, FONT_H2)
    if len(series) < 2:
        draw_text(draw, (x1 + 24, y1 + 82), "趋势数据不足，暂不展示折线。", FONT_BODY, fill="#9a3412", max_width=x2 - x1 - 48)
        return

    px1, py1, px2, py2 = x1 + 62, y1 + 78, x2 - 36, y2 - 58
    draw.line((px1, py2, px2, py2), fill="#d1d5db", width=2)
    draw.line((px1, py1, px1, py2), fill="#d1d5db", width=2)

    all_values: List[float] = []
    for key, _, _ in keys:
        all_values.extend(float(item.get(key) or 0) for item in series)
    max_v = max(all_values) if all_values else 0
    min_v = min(all_values) if all_values else 0
    if max_v == min_v:
        max_v += 1
        min_v = 0

    def point(idx: int, value: float) -> Tuple[float, float]:
        x = px1 + (px2 - px1) * idx / (len(series) - 1)
        y = py2 - (py2 - py1) * ((value - min_v) / (max_v - min_v))
        return x, y

    for key, _, color in keys:
        points = [point(i, float(item.get(key) or 0)) for i, item in enumerate(series)]
        for i in range(len(points) - 1):
            draw.line((*points[i], *points[i + 1]), fill=color, width=4)
        for p in points:
            draw.ellipse((p[0] - 4, p[1] - 4, p[0] + 4, p[1] + 4), fill=color)

    for i, item in enumerate(series):
        x, _ = point(i, min_v)
        draw_text(draw, (int(x) - 24, py2 + 12), item.get("day", "")[5:], FONT_SMALL, fill="#6b7280")

    legend_x = x1 + 24
    legend_y = y2 - 36
    for key, label, color in keys:
        draw.rectangle((legend_x, legend_y + 4, legend_x + 18, legend_y + 18), fill=color)
        draw_text(draw, (legend_x + 26, legend_y), f"{label} {value_formatter(series[-1].get(key))}", FONT_SMALL, fill="#374151")
        legend_x += 330


def draw_mini_chart(
    draw: ImageDraw.ImageDraw,
    box: Tuple[int, int, int, int],
    title: str,
    series: List[Dict[str, Any]],
    key: str,
    color: str,
    value_formatter=intfmt,
) -> None:
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(box, radius=18, fill="#ffffff", outline="#e5e7eb", width=2)
    draw_text(draw, (x1 + 22, y1 + 18), title, FONT_SMALL, fill="#6b7280")
    latest = series[-1].get(key) if series else None
    draw_text(draw, (x1 + 22, y1 + 48), value_formatter(latest), FONT_H2, fill="#111827")
    if len(series) < 2:
        draw_text(draw, (x1 + 22, y1 + 92), "趋势不足", FONT_SMALL, fill="#9a3412")
        return

    values = [float(item.get(key) or 0) for item in series]
    max_v = max(values)
    min_v = min(values)
    if max_v == min_v:
        max_v += 1
        min_v = 0
    px1, py1, px2, py2 = x1 + 66, y1 + 96, x2 - 24, y2 - 34
    for frac in (0, 0.5, 1):
        y = py2 - (py2 - py1) * frac
        value = min_v + (max_v - min_v) * frac
        draw.line((px1 - 8, y, px2, y), fill="#eef2f7", width=1)
        draw_text(draw, (x1 + 12, int(y) - 9), value_formatter(int(value)), FONT_SMALL, fill="#6b7280")
    draw.line((px1, py2, px2, py2), fill="#e5e7eb", width=2)
    draw.line((px1, py1, px1, py2), fill="#e5e7eb", width=2)

    def point(idx: int, value: float) -> Tuple[float, float]:
        x = px1 + (px2 - px1) * idx / (len(values) - 1)
        y = py2 - (py2 - py1) * ((value - min_v) / (max_v - min_v))
        return x, y

    points = [point(i, value) for i, value in enumerate(values)]
    for i in range(len(points) - 1):
        draw.line((*points[i], *points[i + 1]), fill=color, width=4)
    for p in points:
        draw.ellipse((p[0] - 4, p[1] - 4, p[0] + 4, p[1] + 4), fill=color)
    draw_text(draw, (px1, y2 - 24), series[0].get("day", "")[5:], FONT_SMALL, fill="#6b7280")
    draw_text(draw, (px2 - 44, y2 - 24), series[-1].get("day", "")[5:], FONT_SMALL, fill="#6b7280")


def render_report(data: Dict[str, Any], output_path: Path) -> None:
    overview = data["overview"]
    top_users = data["top_users"].get("data", [])[:8]
    low_balance = data["low_balance"].get("data", [])[:8]
    errors = data["errors"]
    trend = data.get("trend") or []
    balance_user_value, balance_user_subtitle = positive_balance_users_label(overview)

    img = Image.new("RGB", (1600, 2600), "#f3f4f6")
    draw = ImageDraw.Draw(img)

    draw.rectangle((0, 0, 1600, 180), fill="#111827")
    draw_text(draw, (64, 42), "CoinCoin 每日经营看板", FONT_TITLE, fill="#ffffff")
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    period = f"{overview.get('start_day', '')} 至 {overview.get('end_day', '')}"
    draw_text(draw, (66, 108), f"数据周期：{period} · 生成时间：{generated}", FONT_BODY, fill="#cbd5e1")
    draw_text(draw, (1240, 58), "Asia/Singapore 08:00", FONT_BODY, fill="#93c5fd")

    cards = [
        ("实付入账", money(overview.get("paid_cents")), "今日真实支付入账", "#059669"),
        ("用户侧消耗", money(overview.get("user_charge_cents")), "按 CoinCoin API 价格扣费，不等于上游真实成本", "#dc2626"),
        ("有余额用户", balance_user_value, balance_user_subtitle, "#7c3aed"),
        ("今日请求", intfmt(overview.get("requests_total")), "总 API 请求数", "#2563eb"),
        ("今日活跃", intfmt(overview.get("active_users_period")), "今日有调用/使用的用户", "#ea580c"),
        ("错误率", pct(float(errors.get("error_rate") or 0)), f"失败 {intfmt(errors.get('failed_requests'))} / {intfmt(errors.get('total_requests'))}", "#0f766e"),
    ]
    x0, y0 = 64, 220
    cw, ch, gap = 464, 142, 38
    for i, item in enumerate(cards):
        row, col = divmod(i, 3)
        card(draw, (x0 + col * (cw + gap), y0 + row * (ch + 28), x0 + col * (cw + gap) + cw, y0 + row * (ch + 28) + ch), *item)

    draw_text(draw, (64, 590), "7 日趋势", FONT_H2)
    draw_mini_chart(draw, (64, 636, 390, 850), "请求数", trend, "requests_total", "#2563eb", compact_int)
    draw_mini_chart(draw, (430, 636, 756, 850), "Token 消耗", trend, "tokens_total", "#7c3aed", compact_int)
    draw_mini_chart(draw, (796, 636, 1122, 850), "用户侧消耗", trend, "user_charge_cents", "#dc2626", money)
    draw_mini_chart(draw, (1162, 636, 1536, 850), "活跃用户", trend, "active_users", "#059669")

    draw.rounded_rectangle((64, 884, 1536, 982), radius=18, fill="#fefce8", outline="#fde68a", width=2)
    draw_text(draw, (96, 912), "入账 / 毛利趋势", FONT_H2, fill="#854d0e")
    draw_text(
        draw,
        (96, 950),
        "当前接口未提供按日入账序列，也没有上游真实成本 upstream_cost；暂不计算净现金流、利润或亏损。补齐后再展示入账与毛利趋势。",
        FONT_BODY,
        fill="#713f12",
        max_width=1320,
    )

    top_rows = [
        [
            str(u.get("rank", "")),
            u.get("display_name") or u.get("username") or u.get("user_id", "")[-6:],
            intfmt(u.get("requests_total")),
            money(u.get("cost_cents")),
            money(u.get("balance_cents")),
        ]
        for u in top_users
    ]
    table(
        draw,
        64,
        1030,
        1472,
        "Top 用户消耗（7天）",
        ["#", "用户", "请求", "消耗", "余额"],
        top_rows,
        [70, 650, 230, 250, 220],
    )

    risk_rows = [
        [
            str(u.get("rank", "")),
            u.get("display_name") or u.get("username") or u.get("user_id", "")[-6:],
            money(u.get("balance_cents")),
            money(u.get("period_cost_cents")),
            str(u.get("estimated_days_remaining", "")),
            u.get("risk_level", ""),
        ]
        for u in low_balance
    ]
    table(
        draw,
        64,
        1500,
        1472,
        "余额风险（7天消耗估算）",
        ["#", "用户", "余额", "周期消耗", "预计天数", "风险"],
        risk_rows,
        [70, 520, 210, 240, 220, 170],
    )

    y = 1970
    draw_text(draw, (64, y), "错误概览", FONT_H2)
    y += 44
    draw.rounded_rectangle((64, y, 1536, y + 190), radius=18, fill="#ffffff", outline="#e5e7eb", width=2)
    draw_text(draw, (96, y + 30), f"今日总请求：{intfmt(errors.get('total_requests'))}", FONT_BODY)
    draw_text(draw, (96, y + 68), f"失败请求：{intfmt(errors.get('failed_requests'))}", FONT_BODY)
    draw_text(draw, (96, y + 106), f"错误率：{pct(float(errors.get('error_rate') or 0))}", FONT_BODY)
    recent = errors.get("recent") or []
    if recent:
        draw_text(draw, (520, y + 30), "最近失败请求", FONT_BODY)
        for idx, item in enumerate(recent[:3]):
            draw_text(draw, (520, y + 68 + idx * 34), json.dumps(item, ensure_ascii=False)[:90], FONT_SMALL, max_width=900)
    else:
        draw_text(draw, (520, y + 66), "最近失败请求：暂无", FONT_BODY, fill="#059669")

    y += 240
    draw.rounded_rectangle((64, y, 1536, y + 180), radius=18, fill="#fff7ed", outline="#fed7aa", width=2)
    draw_text(draw, (96, y + 28), "号池数据状态", FONT_H2, fill="#9a3412")
    draw_text(
        draw,
        (96, y + 74),
        "当前 analytics 接口尚未提供 channel_type、upstream_cost、gross_margin。用户侧消耗不是上游真实成本，因此暂不展示号池毛利、利润或亏损。字段补齐后可扩展为正式模块。",
        FONT_BODY,
        fill="#7c2d12",
        max_width=1340,
    )

    draw_text(draw, (64, 2476), "数据源：CoinCoin admin analytics API · 自动生成，不含敏感 token / API Key", FONT_SMALL, fill="#6b7280")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path)


def upload_and_send(path: Path, summary: str, target: str) -> None:
    upload = subprocess.run(
        ["slock", "attachment", "upload", "--path", str(path.resolve()), "--channel", target, "--mime-type", "image/png"],
        check=True,
        capture_output=True,
        text=True,
    )
    attachment_id = None
    for token in upload.stdout.replace("(", " ").replace(")", " ").split():
        if len(token) == 36 and token.count("-") == 4:
            attachment_id = token
            break
    if not attachment_id:
        raise RuntimeError(f"Could not parse attachment id from upload output: {upload.stdout}")
    subprocess.run(
        ["slock", "message", "send", "--target", target, "--attachment-id", attachment_id],
        input=summary,
        check=True,
        text=True,
    )


def build_summary(data: Dict[str, Any], dry_run: bool) -> str:
    overview = data["overview"]
    errors = data["errors"]
    balance_user_value, _ = positive_balance_users_label(overview)
    prefix = "【测试】" if dry_run else "【自动日报】"
    return (
        f"{prefix}CoinCoin 每日经营看板\\n"
        f"周期：{overview.get('start_day')} 至 {overview.get('end_day')}\\n"
        f"实付入账：{money(overview.get('paid_cents'))}；用户侧消耗：{money(overview.get('user_charge_cents'))}；"
        f"有余额用户：{balance_user_value}\\n"
        f"请求数：{intfmt(overview.get('requests_total'))}；今日活跃：{intfmt(overview.get('active_users_period'))}；"
        f"错误率：{pct(float(errors.get('error_rate') or 0))}\\n"
        "号池收入/成本/毛利模块待 channel_type、upstream_cost、gross_margin 字段补齐后开启。"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true", help="Upload and send the PNG to Slock")
    parser.add_argument("--target", default=TARGET_CHANNEL)
    parser.add_argument("--output", default="")
    parser.add_argument("--dry-run-label", action="store_true", help="Use test label in sent summary")
    args = parser.parse_args()

    token = os.environ.get("COINCOIN_ADMIN_TOKEN")
    if not token:
        print("COINCOIN_ADMIN_TOKEN is required", file=sys.stderr)
        return 2

    data = fetch_report_data(token)
    today = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_path = Path(args.output) if args.output else OUTPUT_DIR / f"coincoin-daily-report-{today}.png"
    render_report(data, output_path)
    print(output_path)

    if args.send:
        upload_and_send(output_path, build_summary(data, args.dry_run_label), args.target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
