#!/usr/bin/env python3
"""
SMIS 挂刀监控
- 打开 smis.club/exchange
- 主动设置筛选条件并点击「应用设置」
- 抓取表格，按挂刀比例 / 7日跌幅 / 成交量过滤
- 新发现发 Telegram，7 天后复核提醒
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "state.json"
CONFIG_FILE = BASE_DIR / "config.json"
UTC = timezone.utc


def now_utc():
    return datetime.now(UTC)


def parse_number(text):
    if text is None:
        return None
    s = str(text).strip().replace(",", "").replace("¥", "").replace("$", "").replace("￥", "")
    if not s or s in {"-", "--", "N/A", "暂无", "No Data"}:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def parse_percent(text):
    if text is None:
        return None
    s = str(text).strip().replace("%", "")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def normalize_ratio(text):
    if text is None:
        return None
    v = parse_number(text)
    if v is None:
        return None
    # 支持 0.68 或 68% 两种写法
    return v / 100 if v > 1.5 else v


def load_json(path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), "utf-8")


def get_config():
    cfg = load_json(CONFIG_FILE, {})
    env_override = os.getenv("SMIS_CONFIG_JSON")
    if env_override:
        cfg.update(json.loads(env_override))
    return cfg


def set_input_value(page, selector, value):
    """更可靠地设置 input 值（兼容受控组件）"""
    el = page.locator(selector).first
    el.wait_for(state="visible", timeout=10000)
    el.click()
    el.fill("")
    el.fill(str(value))
    # 触发 change / input 事件
    page.evaluate(
        """(sel) => {
            const el = document.querySelector(sel);
            if (el) {
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }
        }""",
        selector,
    )


def scrape_exchange(config):
    url = config.get("site", {}).get("url", "https://smis.club/exchange")
    wait_ms = int(config.get("site", {}).get("wait_ms", 8000))
    filters = config.get("filters", {})

    price_min = filters.get("price_min", 1)
    price_max = filters.get("price_max", 5000)
    # 页面筛选用较低的成交量，真正过滤在代码里做
    page_volume = max(10, min(int(filters.get("volume_min", 50)), 50))

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = browser.new_context(
            viewport={"width": 1600, "height": 1200},
            locale="zh-CN",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        print(f"[info] 打开 {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=90000)

        try:
            page.wait_for_load_state("networkidle", timeout=25000)
        except PlaywrightTimeoutError:
            pass

        page.wait_for_timeout(3000)

        # 尝试关闭可能的验证码/弹窗（尽力而为）
        for sel in [
            "text=关闭",
            "button:has-text('关闭')",
            ".aliyun-captcha-close",
            "[class*='close']",
        ]:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=1500)
            except Exception:
                pass

        # ---------- 设置筛选条件 ----------
        print(f"[info] 设置筛选: 价格 {price_min}~{price_max}, 成交量>={page_volume}")

        # 价格区间：两个相邻的 number input
        try:
            # 常见结构：两个 input 在「价格区间」附近
            inputs = page.locator("input[type='number'], input[type='text']").all()
            # 更稳：用 placeholder 或附近文字定位
            price_inputs = page.locator(
                "div:has-text('价格区间') input, "
                "div:has-text('价格') input[type='number'], "
                "input[placeholder*='价']"
            )
            if price_inputs.count() >= 2:
                price_inputs.nth(0).fill(str(price_min))
                price_inputs.nth(1).fill(str(price_max))
            else:
                # 兜底：按页面上所有数字输入框顺序
                all_num = page.locator("input[type='number']")
                if all_num.count() >= 2:
                    all_num.nth(0).fill(str(price_min))
                    all_num.nth(1).fill(str(price_max))
        except Exception as e:
            print(f"[warn] 设置价格失败: {e}")

        # 成交量
        try:
            vol_input = page.locator(
                "div:has-text('日成交量') input, "
                "div:has-text('成交量') input, "
                "input[placeholder*='成交']"
            ).first
            if vol_input.count() > 0:
                vol_input.fill(str(page_volume))
            else:
                # 最后一个数字输入框通常是成交量
                all_num = page.locator("input[type='number']")
                if all_num.count() >= 3:
                    all_num.nth(2).fill(str(page_volume))
        except Exception as e:
            print(f"[warn] 设置成交量失败: {e}")

        page.wait_for_timeout(800)

        # 点击「应用设置」
        applied = False
        for text in ["应用设置", "应用", "确定", "查询"]:
            btn = page.locator(f"button:has-text('{text}'), .ant-btn:has-text('{text}')")
            if btn.count() > 0:
                try:
                    btn.first.click(timeout=3000)
                    applied = True
                    print(f"[info] 已点击「{text}」")
                    break
                except Exception:
                    continue

        if not applied:
            print("[warn] 未找到应用按钮，尝试直接抓取")

        # 等待表格数据出现
        print("[info] 等待表格数据...")
        data_ready = False
        for attempt in range(12):
            page.wait_for_timeout(1500)
            # 检查是否有真实数据行
            rows = page.locator("table tbody tr")
            n = rows.count()
            if n > 0:
                # 排除纯 “No Data” 行
                first_text = rows.first.inner_text() if n > 0 else ""
                if "No Data" not in first_text and len(first_text.strip()) > 5:
                    data_ready = True
                    print(f"[info] 表格已加载，约 {n} 行")
                    break
            print(f"[info] 等待中... ({attempt + 1}/12)")

        if not data_ready:
            # 再点一次应用
            try:
                page.locator("button:has-text('应用设置')").first.click(timeout=2000)
                page.wait_for_timeout(4000)
            except Exception:
                pass

        page.wait_for_timeout(wait_ms // 2)

        # ---------- 解析表格 ----------
        tables = page.locator("table")
        if tables.count() == 0:
            html_snip = page.content()[:2000]
            browser.close()
            raise RuntimeError(f"未找到 table。页面片段: {html_snip[:300]}")

        chosen = None
        chosen_headers = []
        for i in range(tables.count()):
            t = tables.nth(i)
            try:
                hs = t.locator("thead tr").first.locator("th").all_inner_texts()
            except Exception:
                continue
            joined = " ".join(hs)
            if any(k in joined for k in ["挂刀比例", "七日涨跌", "Steam售价", "饰品名称", "到手Steam"]):
                chosen = t
                chosen_headers = [h.strip() for h in hs]
                break

        if chosen is None:
            # 兜底用第一个有 thead 的表
            for i in range(tables.count()):
                t = tables.nth(i)
                try:
                    hs = t.locator("thead tr").first.locator("th").all_inner_texts()
                    if hs:
                        chosen = t
                        chosen_headers = [h.strip() for h in hs]
                        break
                except Exception:
                    continue

        if chosen is None:
            browser.close()
            raise RuntimeError("找到 table，但无法识别表头")

        print(f"[info] 表头: {chosen_headers}")

        rows = chosen.locator("tbody tr")
        results = []
        row_count = rows.count()
        print(f"[info] 原始行数: {row_count}")

        for i in range(row_count):
            cells = rows.nth(i).locator("td").all_inner_texts()
            if not cells:
                continue
            text_join = " ".join(c.strip() for c in cells)
            if "No Data" in text_join or len(text_join.strip()) < 3:
                continue
            results.append({"headers": chosen_headers, "cells": [c.strip() for c in cells]})

        browser.close()

    return parse_rows(results)


def parse_rows(raw_rows):
    out = []
    for row in raw_rows:
        headers = row["headers"]
        cells = row["cells"]
        if len(cells) < 5:
            continue

        data = {headers[i]: cells[i] for i in range(min(len(headers), len(cells)))}

        def col(*keywords):
            for h, v in data.items():
                if any(k in h for k in keywords):
                    return v
            return None

        name = col("饰品名称", "商品", "名称") or (cells[1] if len(cells) > 1 else cells[0])
        # 去掉可能的图片/空格
        if name:
            name = re.sub(r"\s+", " ", name).strip()

        change_7d = parse_percent(col("七日涨跌", "7日涨跌", "涨跌"))
        volume = parse_number(col("成交量"))
        steam_price = parse_number(col("Steam售价", "Steam售"))
        platform_price = parse_number(col("平台售价", "平台价"))
        steam_balance = parse_number(col("到手Steam余额", "Steam余额", "到手余额"))
        ratio = normalize_ratio(col("挂刀比例", "比例"))
        platform = col("交易平台", "平台") or ""
        market_link = col("Steam市场", "市场") or ""
        updated = col("更新时间") or ""

        if not name or ratio is None:
            continue

        steam_url = (
            market_link
            if market_link.startswith("http")
            else "https://steamcommunity.com/market/search?appid=730&q=" + quote(name)
        )

        out.append(
            {
                "name": name,
                "change_7d": change_7d,
                "volume": volume,
                "steam_price": steam_price,
                "platform_price": platform_price,
                "steam_balance": steam_balance,
                "ratio": ratio,
                "platform": platform,
                "steam_url": steam_url,
                "updated": updated,
                "scraped_at": now_utc().isoformat(),
            }
        )
    return out


def qualify(item, filters):
    ratio_max = float(filters.get("ratio_max", 0.70))
    drop_min = float(filters.get("drop_min_pct", 3))
    volume_min = float(filters.get("volume_min", 50))
    price_min = filters.get("price_min")
    price_max = filters.get("price_max")
    platforms = [str(x).lower() for x in filters.get("platforms", []) if str(x).strip()]

    if item["ratio"] is None or item["ratio"] > ratio_max:
        return False
    if item["change_7d"] is None or item["change_7d"] > -drop_min:
        return False
    if item["volume"] is not None and item["volume"] < volume_min:
        return False
    if platforms and item["platform"].lower() not in platforms:
        return False

    p = item["platform_price"]
    if price_min is not None and (p is None or p < float(price_min)):
        return False
    if price_max is not None and (p is None or p > float(price_max)):
        return False
    return True


def key_for(item):
    return f"{item['name']}::{item['platform']}"


def fmt_money(v):
    return "—" if v is None else f"¥{v:,.2f}"


def fmt_pct(v):
    return "—" if v is None else f"{v:+.2f}%"


def telegram_send(bot_token, chat_id, text):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    r = requests.post(
        url,
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    r.raise_for_status()


def discovery_message(item, unlock_at, sale_method):
    return (
        "🟢 <b>发现挂刀机会</b>\n\n"
        f"<b>{item['name']}</b>\n"
        f"7日跌幅：<b>{fmt_pct(item['change_7d'])}</b>\n"
        f"挂刀比例：<b>{item['ratio']:.4f}</b>\n"
        f"平台：{item['platform'] or '—'}\n"
        f"平台价：{fmt_money(item['platform_price'])}\n"
        f"Steam售价：{fmt_money(item['steam_price'])}\n"
        f"到手余额：{fmt_money(item['steam_balance'])}\n"
        f"成交量：{item['volume'] if item['volume'] is not None else '—'}\n\n"
        f"策略：<b>{sale_method}</b>\n"
        f"预计可卖时间：<b>{unlock_at.astimezone().strftime('%Y-%m-%d %H:%M')}</b>\n"
        f"<a href=\"{item['steam_url']}\">打开 Steam 市场</a>"
    )


def mature_message(item, record, sale_method):
    ratio = item.get("ratio")
    ratio_text = f"{ratio:.4f}" if ratio is not None else "—"
    change = fmt_pct(item.get("change_7d"))
    return (
        "⏰ <b>7天保护期到期复核</b>\n\n"
        f"<b>{item.get('name', record['name'])}</b>\n"
        f"当前平台：{item.get('platform', record.get('platform', '—'))}\n"
        f"当前挂刀比例：<b>{ratio_text}</b>\n"
        f"当前7日涨跌：<b>{change}</b>\n"
        f"平台价：{fmt_money(item.get('platform_price'))}\n"
        f"Steam售价：{fmt_money(item.get('steam_price'))}\n"
        f"到手余额：{fmt_money(item.get('steam_balance'))}\n\n"
        f"建议：按 <b>{sale_method}</b> 路径核对实时市场后出售。\n"
        f"<a href=\"{item.get('steam_url', record.get('steam_url', ''))}\">打开 Steam 市场</a>"
    )


def run():
    config = get_config()
    token = os.getenv("TG_BOT_TOKEN")
    chat_id = os.getenv("TG_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("缺少环境变量 TG_BOT_TOKEN 或 TG_CHAT_ID")

    state = load_json(DATA_FILE, {"candidates": {}})
    candidates = state.setdefault("candidates", {})
    filters = config.get("filters", {})
    sale_method = config.get("strategy", {}).get("sale_method", "Steam挂底价")
    hold_days = float(config.get("strategy", {}).get("hold_days", 7))

    print("[info] 开始抓取...")
    rows = scrape_exchange(config)
    print(f"[info] 解析到 {len(rows)} 条有效商品")

    # 调试：打印前几条比例
    for i, r in enumerate(rows[:8]):
        print(
            f"  [{i+1}] {r['name'][:20]:20s}  ratio={r['ratio']:.4f}  "
            f"7d={r['change_7d']}  vol={r['volume']}  plat={r['platform']}"
        )

    by_key = {key_for(x): x for x in rows}
    qualified = [x for x in rows if qualify(x, filters)]
    qualified.sort(key=lambda x: (x["ratio"], -(x["volume"] or 0)))

    print(f"[info] 符合过滤条件: {len(qualified)} 条")
    for q in qualified[:10]:
        print(f"  ✓ {q['name']}  ratio={q['ratio']:.4f}  7d={q['change_7d']}%")

    changed = False
    new_count = 0

    for item in qualified:
        key = key_for(item)
        if key not in candidates:
            unlock = now_utc() + timedelta(days=hold_days)
            candidates[key] = {
                "name": item["name"],
                "platform": item["platform"],
                "steam_url": item["steam_url"],
                "first_seen": now_utc().isoformat(),
                "unlock_at": unlock.isoformat(),
                "discovery_ratio": item["ratio"],
                "discovery_change_7d": item["change_7d"],
                "status": "waiting",
            }
            try:
                telegram_send(token, chat_id, discovery_message(item, unlock, sale_method))
                print(f"[tg] 已推送新机会: {item['name']}")
                new_count += 1
            except Exception as e:
                print(f"[error] Telegram 发送失败: {e}")
            changed = True

    # 到期复核
    for key, record in list(candidates.items()):
        if record.get("status") != "waiting":
            continue
        try:
            unlock_at = datetime.fromisoformat(record["unlock_at"])
        except Exception:
            continue
        if now_utc() < unlock_at:
            continue

        item = by_key.get(key)
        if item is None:
            item = dict(record)
            item.update(
                {
                    "ratio": None,
                    "change_7d": None,
                    "platform_price": None,
                    "steam_price": None,
                    "steam_balance": None,
                    "volume": None,
                }
            )
        try:
            telegram_send(token, chat_id, mature_message(item, record, sale_method))
            print(f"[tg] 已推送到期复核: {record.get('name')}")
        except Exception as e:
            print(f"[error] Telegram 发送失败: {e}")
        record["status"] = "matured"
        record["matured_at"] = now_utc().isoformat()
        changed = True

    # 限制状态文件大小
    max_records = int(config.get("state", {}).get("max_records", 3000))
    if len(candidates) > max_records:
        matured = [(k, v) for k, v in candidates.items() if v.get("status") == "matured"]
        matured.sort(key=lambda kv: kv[1].get("matured_at", ""))
        for k, _ in matured[: max(0, len(candidates) - max_records)]:
            candidates.pop(k, None)
            changed = True

    if changed:
        save_json(DATA_FILE, state)

    summary = {
        "scraped": len(rows),
        "qualified": len(qualified),
        "new_candidates": new_count,
    }
    print(json.dumps(summary, ensure_ascii=False))
    return summary


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)
