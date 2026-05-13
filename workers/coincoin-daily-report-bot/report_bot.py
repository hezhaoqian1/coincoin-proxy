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
from urllib.parse import urlparse

import pymysql
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
    return f"{float(value or 0) * 100:.1f}%"


def status_text(value: Any, fallback: str = "-") -> str:
    if value is None:
        return fallback
    text = str(value)
    return text if text else fallback


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


def database_url() -> str:
    url = os.environ.get("COINCOIN_DATABASE_URL") or os.environ.get("DATABASE_URL") or os.environ.get("MYSQL_URL") or ""
    if url:
        return url
    host = os.environ.get("COINCOIN_DB_HOST")
    name = os.environ.get("COINCOIN_DB_NAME")
    user = os.environ.get("COINCOIN_DB_USER")
    password = os.environ.get("COINCOIN_DB_PASSWORD")
    port = os.environ.get("COINCOIN_DB_PORT", "3306")
    if host and name and user and password:
        return f"mysql://{user}:{password}@{host}:{port}/{name}"
    return ""


def fetch_positive_balance_users_from_db() -> Optional[int]:
    url = database_url()
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("mysql", "mysql+pymysql", "mysql+asyncmy"):
        return None
    conn = pymysql.connect(
        host=parsed.hostname or "localhost",
        port=parsed.port or 3306,
        user=parsed.username or "",
        password=parsed.password or "",
        database=(parsed.path or "/").lstrip("/"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=20,
        write_timeout=20,
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS count
                FROM coincoin_users u
                LEFT JOIN (
                    SELECT
                        s.user_id,
                        GREATEST(s.quota_cents - s.used_cents, 0)
                        + COALESCE(tp.remaining_cents, 0) AS balance_cents
                    FROM coincoin_user_subscriptions s
                    LEFT JOIN (
                        SELECT user_id, SUM(remaining_cents) AS remaining_cents
                        FROM coincoin_traffic_pack_balances
                        WHERE status = 'active'
                          AND remaining_cents > 0
                          AND expires_at > UTC_TIMESTAMP()
                        GROUP BY user_id
                    ) tp ON tp.user_id = s.user_id
                    WHERE s.status = 'active'
                      AND s.paid_until IS NOT NULL
                      AND s.paid_until > UTC_TIMESTAMP()
                ) b ON b.user_id = u.id
                WHERE COALESCE(u.balance, 0) + COALESCE(b.balance_cents, 0) > 0
                """
            )
            row = cursor.fetchone() or {}
            return int(row.get("count") or 0)
    finally:
        conn.close()


def fill_positive_balance_users(overview: Dict[str, Any]) -> None:
    if any(key in overview for key in ("positive_balance_users", "users_with_balance", "balance_positive_users")):
        return
    try:
        count = fetch_positive_balance_users_from_db()
    except Exception as exc:
        print(f"positive balance DB fallback failed: {exc}", file=sys.stderr)
        return
    if count is not None:
        overview["positive_balance_users"] = count
        overview["users_with_balance"] = count
        overview["positive_balance_users_source"] = "db_fallback"


def fetch_report_data(token: str) -> Dict[str, Any]:
    dashboard = fetch_json("/admin/analytics/operating-dashboard?period=today", token)
    overview = dashboard.get("overview") or {}
    fill_positive_balance_users(overview)
    dashboard["overview"] = overview
    dashboard["top_users"] = fetch_json("/admin/analytics/top-users?period=7d&metric=cost_cents&limit=10", token)
    dashboard["trend"] = fetch_trend_data(token)
    return dashboard


def fetch_report_data_from_snapshot(path: str) -> Dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "trend" not in data:
        growth_daily = ((data.get("growth") or {}).get("daily") or [])
        data["trend"] = [
            {
                "day": item.get("day"),
                "requests_total": item.get("requests_total", 0),
                "user_charge_cents": item.get("user_charge_cents", 0),
                "tokens_total": item.get("tokens_total", 0),
                "active_users": item.get("active_users", 0),
            }
            for item in growth_daily[-7:]
        ]
    return data


def is_test_identity(*values: Any) -> bool:
    text = " ".join(str(value or "").lower() for value in values)
    markers = ("test", "smoke", "demo", "dummy", "internal", "codex_", "probe")
    return any(marker in text for marker in markers)


def curated_actions(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    actions = list(((data.get("action_items") or {}).get("items") or []))
    usage_items = (data.get("usage_structure") or {}).get("data") or []
    revenue = data.get("revenue_margin") or {}
    channel = data.get("channel_health") or {}
    growth = data.get("growth") or {}
    low_balance = (data.get("low_balance") or {}).get("data") or []

    curated: List[Dict[str, Any]] = []
    seen_types = set()

    def add(item: Dict[str, Any]) -> None:
        key = item.get("type") or item.get("title")
        if key in seen_types:
            return
        seen_types.add(key)
        curated.append(item)

    account_pool = channel.get("account_pool") or {}
    if account_pool and not (revenue.get("source_quality") or {}).get("upstream_cost_available"):
        add({
            "severity": "high",
            "type": "account_pool_margin_gap",
            "owner": "tech",
            "title": f"号池占比 {pct(account_pool.get('request_share'))}，但缺真实成本，无法判断是否赚钱",
            "evidence": {
                "account_pool_share": account_pool.get("request_share"),
                "account_pool_requests": account_pool.get("requests"),
                "user_charge_cents": revenue.get("user_charge_cents"),
            },
            "suggested_action": "今天补 upstream_cost 写入/回填，至少让 account_pool 能算收入、成本和毛利。",
        })

    if int(growth.get("new_users") or 0) == 0 and int(growth.get("first_call_users") or 0) > 0:
        add({
            "severity": "high",
            "type": "growth_gap",
            "owner": "product",
            "title": "今日没有新增接入，消耗来自存量用户",
            "evidence": {
                "new_users": growth.get("new_users"),
                "new_api_key_users": growth.get("new_api_key_users"),
                "first_call_users": growth.get("first_call_users"),
                "first_paid_users": growth.get("first_paid_users"),
            },
            "suggested_action": "检查注册到 API Key/首充漏斗，今天明确拉新或转化动作。",
        })

    high_latency = next(
        (item for item in usage_items if int(item.get("avg_latency_ms") or 0) >= 12000 and int(item.get("requests") or 0) >= 5),
        None,
    )
    if high_latency:
        avg_latency_ms = int(high_latency.get("avg_latency_ms") or 0)
        add({
            "severity": "high" if avg_latency_ms >= 30000 else "medium",
            "type": "high_latency_model",
            "owner": "tech",
            "title": f"{high_latency.get('model')} 平均延迟 {avg_latency_ms // 1000}s，影响可用性",
            "evidence": {
                "model": high_latency.get("model"),
                "billable_sku": high_latency.get("billable_sku"),
                "requests": high_latency.get("requests"),
                "avg_latency_ms": avg_latency_ms,
            },
            "suggested_action": "检查高延迟模型/图片任务，必要时路由降级或转异步。",
        })

    real_low_balance = next(
        (
            user for user in low_balance
            if not is_test_identity(user.get("display_name"), user.get("username"), user.get("email"), user.get("external_id"))
            and user.get("estimated_days_remaining") is not None
            and float(user.get("estimated_days_remaining") or 0) <= 2
        ),
        None,
    )
    if real_low_balance:
        add({
            "severity": "high",
            "type": "real_low_balance_user",
            "owner": "bd",
            "title": f"{real_low_balance.get('display_name') or real_low_balance.get('user_id')} 余额预计不足 2 天",
            "evidence": {
                "user_id": real_low_balance.get("user_id"),
                "balance_cents": real_low_balance.get("balance_cents"),
                "estimated_days_remaining": real_low_balance.get("estimated_days_remaining"),
            },
            "suggested_action": "今天联系真实高消耗用户充值或确认企业方案。",
        })

    for item in actions:
        title = item.get("title", "")
        if is_test_identity(title, json.dumps(item.get("evidence", {}), ensure_ascii=False)):
            continue
        add(item)
        if len(curated) >= 5:
            break

    return curated[:5]


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


def wrapped_height(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> int:
    if not text:
        return font.size + 6
    line = ""
    lines = 1
    for char in str(text):
        candidate = line + char
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            line = candidate
        else:
            lines += 1
            line = char
    return lines * (font.size + 6)


def conclusion_row(draw: ImageDraw.ImageDraw, box, title: str, text: str, fill="#ffffff", outline="#e5e7eb"):
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(box, radius=16, fill=fill, outline=outline, width=2)
    draw_text(draw, (x1 + 18, y1 + 18), title, FONT_H2, fill="#111827")
    draw_text(draw, (x1 + 110, y1 + 18), text, FONT_BODY, fill="#111827", max_width=x2 - x1 - 136)


def card(draw: ImageDraw.ImageDraw, box, title: str, value: str, subtitle: str, accent="#2563eb"):
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(box, radius=18, fill="#ffffff", outline="#e5e7eb", width=2)
    draw.rectangle((x1, y1, x1 + 7, y2), fill=accent)
    draw_text(draw, (x1 + 26, y1 + 22), title, FONT_SMALL, fill="#6b7280")
    draw_text(draw, (x1 + 26, y1 + 58), value, FONT_H1, fill="#111827")
    draw_text(draw, (x1 + 26, y1 + 98), subtitle, FONT_SMALL, fill="#6b7280", max_width=x2 - x1 - 52)


def table(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    w: int,
    title: str,
    headers: List[str],
    rows: List[List[str]],
    col_widths: List[int],
    row_height: int = 44,
):
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
        draw.rounded_rectangle((x, y, x + w, y + row_height), radius=8, fill=bg)
        cx = x + 16
        for i, cell in enumerate(row):
            draw_text(draw, (cx, y + 11), str(cell), FONT_SMALL, fill="#111827", max_width=col_widths[i] - 12)
            cx += col_widths[i]
        y += row_height + 2


def judgement_box(draw: ImageDraw.ImageDraw, box, title: str, text: str, fill="#eef2ff", outline="#c7d2fe"):
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(box, radius=16, fill=fill, outline=outline, width=2)
    draw_text(draw, (x1 + 18, y1 + 16), title, FONT_SMALL, fill="#374151")
    draw_text(draw, (x1 + 18, y1 + 46), text, FONT_BODY, fill="#111827", max_width=x2 - x1 - 36)


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
    overview = data.get("overview") or {}
    growth = data.get("growth") or {}
    revenue = data.get("revenue_margin") or {}
    usage = data.get("usage_structure") or {}
    channel = data.get("channel_health") or {}
    top_users = (data.get("top_users") or {}).get("data", [])[:6]
    low_balance = (data.get("low_balance") or {}).get("data", [])[:6]
    errors = data.get("errors") or {}
    trend = data.get("trend") or []
    judgement = data.get("judgement") or {}
    balance_user_value, balance_user_subtitle = positive_balance_users_label(overview)
    margin_available = bool((revenue.get("source_quality") or {}).get("upstream_cost_available"))
    account_pool = channel.get("account_pool") or {}
    action_rows_data = curated_actions(data)
    account_pool_share_text = pct(account_pool.get("request_share")) if account_pool else "缺字段"
    if int(growth.get("new_users") or 0) == 0 and int(growth.get("first_call_users") or 0) > 0:
        growth_text = "今日没有新增接入，消耗来自存量用户，需看拉新/转化。"
    else:
        growth_text = status_text(judgement.get("growth"))
    channel_text = (
        f"号池占比 {account_pool_share_text}，但缺真实成本，今天无法判断是否赚钱。"
        if account_pool and not margin_available
        else status_text(judgement.get("channel"))
    )

    img = Image.new("RGB", (1600, 3500), "#f3f4f6")
    draw = ImageDraw.Draw(img)

    draw.rectangle((0, 0, 1600, 180), fill="#111827")
    draw_text(draw, (64, 42), "CoinCoin 公司经营驾驶舱", FONT_TITLE, fill="#ffffff")
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    period = f"{overview.get('start_day', '')} 至 {overview.get('end_day', '')}"
    draw_text(draw, (66, 108), f"数据周期：{period} · 生成时间：{generated}", FONT_BODY, fill="#cbd5e1")
    draw_text(draw, (1240, 58), "Asia/Singapore 08:00", FONT_BODY, fill="#93c5fd")

    draw.rounded_rectangle((64, 210, 1536, 620), radius=18, fill="#ffffff", outline="#dbeafe", width=2)
    draw_text(draw, (96, 238), "今日经营结论", FONT_H2, fill="#111827")
    draw_text(draw, (96, 278), status_text(judgement.get("overall")), FONT_BODY, fill="#111827", max_width=1340)
    conclusion_row(draw, (96, 332, 1504, 390), "增长", growth_text, "#eef2ff", "#c7d2fe")
    conclusion_row(draw, (96, 402, 1504, 460), "收入", status_text(judgement.get("revenue")), "#ecfdf5", "#bbf7d0")
    conclusion_row(draw, (96, 472, 1504, 530), "号池", channel_text, "#fff7ed", "#fed7aa")
    conclusion_row(draw, (96, 542, 1504, 600), "风险", status_text(judgement.get("risk")), "#fef2f2", "#fecaca")

    cards = [
        ("有余额用户", balance_user_value, balance_user_subtitle, "#7c3aed"),
        ("新增 / Key / 首调", f"{intfmt(growth.get('new_users'))} / {intfmt(growth.get('new_api_key_users'))} / {intfmt(growth.get('first_call_users'))}", "注册、接入意图、真实激活", "#2563eb"),
        ("实付入账", money(revenue.get("paid_cents") or overview.get("paid_cents")), "今日真实支付入账", "#059669"),
        ("用户侧消耗", money(revenue.get("user_charge_cents") or overview.get("user_charge_cents")), "按 CoinCoin API 价格扣费", "#dc2626"),
        ("真实毛利", money(revenue.get("gross_margin_cents")) if margin_available else "无法判断", "缺 upstream_cost 时不能算利润", "#ea580c"),
        ("号池占比", pct(account_pool.get("request_share")) if account_pool else "缺字段", "由 route_reason 推断，需补 channel_type", "#0f766e"),
        ("错误率", pct(float(errors.get("error_rate") or 0)), f"失败 {intfmt(errors.get('failed_requests'))} / {intfmt(errors.get('total_requests'))}", "#0f766e"),
    ]
    cards = cards[:8]
    x0, y0 = 64, 660
    cw, ch, gap = 464, 142, 38
    for i, item in enumerate(cards[:6]):
        row, col = divmod(i, 3)
        card(draw, (x0 + col * (cw + gap), y0 + row * (ch + 28), x0 + col * (cw + gap) + cw, y0 + row * (ch + 28) + ch), *item)

    y = 1010
    draw_text(draw, (64, y), "今日必须处理", FONT_H2)
    action_rows = []
    for idx, item in enumerate(action_rows_data[:4], start=1):
        action_rows.append([
            str(idx),
            item.get("severity", "-"),
            item.get("owner", "-"),
            item.get("title", "-"),
            item.get("suggested_action", "-"),
        ])
    table(draw, 64, y + 44, 1472, "", ["#", "级别", "Owner", "问题", "动作"], action_rows, [54, 90, 120, 520, 650], row_height=58)

    y = 1370
    if len(trend) >= 2:
        draw_text(draw, (64, y), "7 日基础趋势", FONT_H2)
        draw_mini_chart(draw, (64, y + 46, 390, y + 250), "请求数", trend, "requests_total", "#2563eb", compact_int)
        draw_mini_chart(draw, (430, y + 46, 756, y + 250), "Token 消耗", trend, "tokens_total", "#7c3aed", compact_int)
        draw_mini_chart(draw, (796, y + 46, 1122, y + 250), "用户侧消耗", trend, "user_charge_cents", "#dc2626", money)
        draw_mini_chart(draw, (1162, y + 46, 1536, y + 250), "活跃用户", trend, "active_users", "#059669")
        y += 300
    else:
        draw.rounded_rectangle((64, y, 1536, y + 96), radius=18, fill="#fffbeb", outline="#fde68a", width=2)
        draw_text(draw, (96, y + 22), "7 日趋势暂降级", FONT_H2, fill="#92400e")
        draw_text(draw, (310, y + 25), "当前 snapshot 只有单日趋势数据，先不占用大面积展示；正式线上 API 会补 7 日 daily 后恢复折线。", FONT_BODY, fill="#78350f", max_width=1100)
        y += 140

    usage_rows = [
        [
            item.get("model") or "-",
            item.get("billable_sku") or "-",
            money(item.get("user_charge_cents")),
            money(item.get("upstream_cost_cents")),
            pct(item.get("failure_rate")),
            f"{int(item.get('avg_latency_ms') or 0)}ms",
        ]
        for item in (usage.get("data") or [])[:6]
    ]
    table(
        draw,
        64,
        y,
        1472,
        "产品使用结构：Top 模型 / SKU",
        ["模型", "SKU", "消耗", "成本", "失败率", "均延迟"],
        usage_rows,
        [310, 390, 170, 170, 150, 150],
    )

    y += 440
    channel_rows = [
        [
            item.get("channel_type") or "-",
            intfmt(item.get("requests")),
            pct(item.get("request_share")),
            money(item.get("user_charge_cents")),
            money(item.get("gross_margin_cents")) if item.get("upstream_cost_cents") else "无法判断",
            pct(item.get("failure_rate")),
        ]
        for item in (channel.get("data") or [])[:5]
    ]
    table(
        draw,
        64,
        y,
        1472,
        "号池健康：通道收入 / 成本 / 稳定性",
        ["通道", "请求", "占比", "消耗", "毛利", "失败率"],
        channel_rows,
        [310, 170, 150, 190, 190, 150],
    )
    y += 260
    if not (channel.get("source_quality") or {}).get("channel_type_available"):
        draw.rounded_rectangle((64, y, 1536, y + 110), radius=18, fill="#fff7ed", outline="#fed7aa", width=2)
        draw_text(draw, (96, y + 26), "号池字段缺口影响经营判断", FONT_H2, fill="#9a3412")
        draw_text(draw, (96, y + 66), "缺稳定 channel_type / provider_pool_id / provider_account_fingerprint，不能准确看号池请求占比、单账号异常和 fallback 压力。", FONT_BODY, fill="#7c2d12", max_width=1340)

    y += 130
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
        y,
        1472,
        "余额风险（7天消耗估算）",
        ["#", "用户", "余额", "周期消耗", "预计天数", "风险"],
        risk_rows,
        [70, 520, 210, 240, 220, 170],
    )

    y += 420
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

    draw_text(draw, (64, 3450), "数据源：CoinCoin admin analytics operating-dashboard API · 自动生成，不含敏感 token / API Key", FONT_SMALL, fill="#6b7280")
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
    overview = data.get("overview") or {}
    errors = data.get("errors") or {}
    revenue = data.get("revenue_margin") or {}
    actions = (data.get("action_items") or {}).get("items") or []
    judgement = data.get("judgement") or {}
    balance_user_value, _ = positive_balance_users_label(overview)
    prefix = "【测试】" if dry_run else "【自动日报】"
    action_text = "；".join(
        f"{item.get('owner', '-')}: {item.get('title', '-')}" for item in actions[:3]
    ) or "暂无高优先级动作"
    margin_text = money(revenue.get("gross_margin_cents")) if (revenue.get("source_quality") or {}).get("upstream_cost_available") else "无法判断"
    return (
        f"{prefix}CoinCoin 公司经营驾驶舱\\n"
        f"周期：{overview.get('start_day')} 至 {overview.get('end_day')}\\n"
        f"判断：{judgement.get('overall', '-')}\\n"
        f"实付入账：{money(revenue.get('paid_cents') or overview.get('paid_cents'))}；用户侧消耗：{money(revenue.get('user_charge_cents') or overview.get('user_charge_cents'))}；真实毛利：{margin_text}；"
        f"有余额用户：{balance_user_value}\\n"
        f"请求数：{intfmt(overview.get('requests_total'))}；今日活跃：{intfmt(overview.get('active_users_period'))}；"
        f"错误率：{pct(float(errors.get('error_rate') or 0))}\\n"
        f"今日动作：{action_text}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true", help="Upload and send the PNG to Slock")
    parser.add_argument("--target", default=TARGET_CHANNEL)
    parser.add_argument("--output", default="")
    parser.add_argument("--dry-run-label", action="store_true", help="Use test label in sent summary")
    parser.add_argument("--input-json", default="", help="Render from a prepared operating-dashboard JSON snapshot")
    args = parser.parse_args()

    if args.input_json:
        data = fetch_report_data_from_snapshot(args.input_json)
    else:
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
