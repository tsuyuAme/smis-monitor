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
    page_volume = max(10, min(int(filters.get("volume_min", 50)), 50))

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = browser.new_context(
            viewport={"width": 1600, "height": 1400},
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

        page.wait_for_timeout(2500)

        # 关闭可能的弹窗
        for sel in [
            "button:has-text('关闭')",
            "text=关闭",
            ".aliyun-captcha-close",
        ]:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=1500)
            except Exception:
                pass

        # ---------- 设置筛选（只动 text/number，绝不碰 radio） ----------
        print(f"[info] 设置筛选: 价格 {price_min}~{price_max}, 成交量>={page_volume}")

        try:
            # Element UI / 常见：价格区间附近的两个可输入框
            filled = page.evaluate(
                """([minV, maxV, volV]) => {
                    const isFillable = (el) => {
                        if (!el || el.disabled || el.readOnly) return false;
                        const t = (el.type || '').toLowerCase();
                        if (t === 'radio' || t === 'checkbox' || t === 'hidden' || t === 'button') return false;
                        return t === 'text' || t === 'number' || t === '' || t === 'search';
                    };
                    // 找「价格区间」附近的 input
                    let priceInputs = [];
                    const labels = Array.from(document.querySelectorAll('div, span, label'));
                    for (const lab of labels) {
                        const txt = (lab.textContent || '').trim();
                        if (txt.includes('价格区间') || txt === '价格') {
                            const box = lab.closest('div') || lab.parentElement;
                            if (box) {
                                const ins = Array.from(box.querySelectorAll('input')).filter(isFillable);
                                if (ins.length >= 2) { priceInputs = ins; break; }
                            }
                        }
                    }
                    if (priceInputs.length < 2) {
                        priceInputs = Array.from(document.querySelectorAll('input')).filter(isFillable);
                    }
                    if (priceInputs.length >= 2) {
                        const setVal = (el, v) => {
                            el.focus();
                            el.value = String(v);
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        };
                        setVal(priceInputs[0], minV);
                        setVal(priceInputs[1], maxV);
                    }
                    // 成交量
                    let volInput = null;
                    for (const lab of labels) {
                        const txt = (lab.textContent || '').trim();
                        if (txt.includes('日成交量') || txt.includes('成交量')) {
                            const box = lab.closest('div') || lab.parentElement;
                            if (box) {
                                const ins = Array.from(box.querySelectorAll('input')).filter(isFillable);
                                if (ins.length) { volInput = ins[0]; break; }
                            }
                        }
                    }
                    if (!volInput && priceInputs.length >= 3) volInput = priceInputs[2];
                    if (volInput) {
                        volInput.focus();
                        volInput.value = String(volV);
                        volInput.dispatchEvent(new Event('input', { bubbles: true }));
                        volInput.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                    return { priceCount: priceInputs.length, hasVol: !!volInput };
                }""",
                [price_min, price_max, page_volume],
            )
            print(f"[info] 输入框设置结果: {filled}")
        except Exception as e:
            print(f"[warn] 设置筛选失败: {e}")

        page.wait_for_timeout(600)

        # 点击「应用设置」
        applied = False
        for text in ["应用设置", "应用"]:
            btn = page.locator(f"button:has-text('{text}')")
            if btn.count() > 0:
                try:
                    btn.first.click(timeout=3000)
                    applied = True
                    print(f"[info] 已点击「{text}」")
                    break
                except Exception:
                    continue
        if not applied:
            print("[warn] 未找到应用按钮，继续抓取")

        # 等待数据
        print("[info] 等待表格数据...")
        for attempt in range(15):
            page.wait_for_timeout(1200)
            # 多种选择器探测是否有数据
            ready = page.evaluate(
                """() => {
                    const bad = (t) => !t || t.includes('No Data') || t.trim().length < 3;
                    // 标准 tr
                    const trs = Array.from(document.querySelectorAll('table tbody tr, .el-table__body tr, .el-table__row'));
                    for (const tr of trs) {
                        const t = (tr.innerText || '').trim();
                        if (!bad(t) && t.length > 10) return true;
                    }
                    // 任意带数字和比例样式的行
                    const divs = Array.from(document.querySelectorAll('[class*="row"], [class*="table"] tr'));
                    let hit = 0;
                    for (const d of divs) {
                        const t = (d.innerText || '');
                        if (/0\\.\\d{2,4}/.test(t) && /¥|￥|\\d+%/.test(t)) hit++;
                    }
                    return hit >= 3;
                }"""
            )
            if ready:
                print(f"[info] 检测到数据 (attempt {attempt + 1})")
                break
            if attempt % 3 == 2:
                print(f"[info] 等待中... ({attempt + 1}/15)")
                try:
                    page.locator("button:has-text('应用设置')").first.click(timeout=1500)
                except Exception:
                    pass

        page.wait_for_timeout(max(1500, wait_ms // 3))

        # ---------- 用 JS 统一抽表头 + 行（兼容 Element UI 虚拟表 / 双表结构） ----------
        extracted = page.evaluate(
            """() => {
                const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();

                // 1) 找表头
                let headers = [];
                const ths = document.querySelectorAll(
                    'table thead th, .el-table__header th, .el-table__header-wrapper th'
                );
                if (ths.length) {
                    headers = Array.from(ths).map(th => clean(th.innerText));
                }
                // 去重连续空头
                headers = headers.filter((h, i, arr) => h || (i > 0 && arr[i-1]));

                // 2) 找数据行：优先 tbody / el-table body
                const rowSelectors = [
                    'table tbody tr',
                    '.el-table__body-wrapper tbody tr',
                    '.el-table__body tr',
                    '.el-table__row',
                    'table tr',
                ];
                let rows = [];
                for (const sel of rowSelectors) {
                    const els = Array.from(document.querySelectorAll(sel));
                    const good = [];
                    for (const el of els) {
                        // 跳过表头行
                        if (el.querySelector('th')) continue;
                        const cells = Array.from(el.querySelectorAll('td, .el-table__cell, [class*="cell"]'));
                        let texts;
                        if (cells.length >= 4) {
                            texts = cells.map(c => clean(c.innerText));
                        } else {
                            texts = clean(el.innerText).split('\\n').map(clean).filter(Boolean);
                        }
                        const joined = texts.join(' ');
                        if (!joined || joined.includes('No Data') || joined.length < 8) continue;
                        // 至少要有数字
                        if (!/\\d/.test(joined)) continue;
                        good.push(texts);
                    }
                    if (good.length >= 3) {
                        rows = good;
                        break;
                    }
                    if (good.length > rows.length) rows = good;
                }

                return { headers, rows, rowCount: rows.length };
            }"""
        )

        browser.close()

    headers = extracted.get("headers") or []
    raw_rows = extracted.get("rows") or []
    print(f"[info] 表头: {headers}")
    print(f"[info] 原始行数: {extracted.get('rowCount', 0)}")

    if not raw_rows:
        print("[warn] 未解析到任何数据行，可能页面仍是 No Data 或结构变化")
        return []

    # 转成 parse_rows 需要的格式
    results = []
    for cells in raw_rows:
        results.append({"headers": headers, "cells": cells})
    return parse_rows(results)



def parse_rows(raw_rows):
    out = []
    for row in raw_rows:
        headers = row.get("headers") or []
        cells = row.get("cells") or []
        if len(cells) < 4:
            continue

        data = {}
        for i in range(min(len(headers), len(cells))):
            h = (headers[i] or "").strip()
            if h:
                data[h] = cells[i]

        def col(*keywords):
            for h, v in data.items():
                if any(k in h for k in keywords):
                    return v
            return None

        # 按表头取；取不到则按常见列顺序兜底
        # 常见顺序: 排行, 名称, 七日涨跌, 成交量, Steam售价, 平台售价, 到手余额, 挂刀比例, 平台, ...
        name = col("饰品名称", "商品", "名称")
        if not name:
            # 找第一个不含纯数字/百分比的较长文本
            for c in cells:
                t = re.sub(r"\s+", " ", str(c)).strip()
                if len(t) >= 2 and not re.fullmatch(r"[\d.%+\-¥￥,\s]+", t) and t not in {"刚刚", "Steam"}:
                    name = t
                    break
        if name:
            name = re.sub(r"\s+", " ", name).strip()

        change_7d = parse_percent(col("七日涨跌", "7日涨跌", "涨跌"))
        volume = parse_number(col("成交量"))
        steam_price = parse_number(col("Steam售价", "Steam售"))
        platform_price = parse_number(col("平台售价", "平台价"))
        steam_balance = parse_number(col("到手Steam余额", "Steam余额", "到手余额"))
        ratio = normalize_ratio(col("挂刀比例", "比例"))
        platform = col("交易平台") or ""
        # 「平台」单独匹配时容易和「平台售价」冲突，上面已优先用更长关键词
        if not platform:
            for h, v in data.items():
                if h == "交易平台" or (h == "平台"):
                    platform = v
                    break
        market_link = col("Steam市场") or ""
        updated = col("更新时间") or ""

        # 若表头匹配失败，尝试从 cells 里用正则抠挂刀比例 (0.6x ~ 0.9x)
        if ratio is None:
            for c in cells:
                m = re.search(r"\b0\.\d{2,4}\b", str(c))
                if m:
                    ratio = float(m.group())
                    break

        if change_7d is None:
            for c in cells:
                m = re.search(r"([+\-]?\d+(?:\.\d+)?)\s*%", str(c))
                if m:
                    change_7d = float(m.group(1))
                    break

        if not name or ratio is None:
            continue

        steam_url = (
            market_link
            if str(market_link).startswith("http")
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
