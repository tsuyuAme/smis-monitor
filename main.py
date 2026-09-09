#!/usr/bin/env python3
"""
SMIS 挂刀监控
- 打开 smis.club/exchange
- 拦截 API 数据 + 解析表格（双通道）
- 按挂刀比例 / 7日跌幅 / 成交量过滤
- 新发现发 Telegram，7 天后复核提醒

本地调试（Windows PowerShell）:
  $env:TG_BOT_TOKEN = "xxx"
  $env:TG_CHAT_ID = "xxx"
  $env:SMIS_HEADED = "1"          # 弹出浏览器窗口
  $env:SMIS_MANUAL_WAIT = "60"    # 有验证码时给你 60 秒手动点
  python main.py
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "state.json"
CONFIG_FILE = BASE_DIR / "config.json"
DEBUG_DIR = BASE_DIR / "data" / "debug"
BROWSER_DIR = BASE_DIR / "browser_data"
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


# ---------------------------------------------------------------------------
# 从 API JSON 解析（优先）
# ---------------------------------------------------------------------------
def items_from_api_payload(payload):
    """尽量兼容多种返回结构，抽出商品列表。"""
    if payload is None:
        return []

    candidates = []
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        for key in ("data", "list", "records", "rows", "result", "items"):
            val = payload.get(key)
            if isinstance(val, list) and val:
                candidates = val
                break
            if isinstance(val, dict):
                for k2 in ("list", "records", "rows", "items"):
                    if isinstance(val.get(k2), list) and val[k2]:
                        candidates = val[k2]
                        break
                if candidates:
                    break
        if not candidates and any(k in payload for k in ("name", "ratio", "commodityName")):
            candidates = [payload]

    out = []
    for it in candidates:
        if not isinstance(it, dict):
            continue
        # 字段名尽量兼容
        name = (
            it.get("name")
            or it.get("commodityName")
            or it.get("goodsName")
            or it.get("itemName")
            or it.get("market_hash_name")
            or it.get("marketHashName")
        )
        ratio = it.get("ratio") or it.get("exchangeRatio") or it.get("knifeRatio") or it.get("rate")
        if ratio is None and it.get("platformPrice") and it.get("steamBalance"):
            try:
                ratio = float(it["platformPrice"]) / float(it["steamBalance"])
            except Exception:
                pass

        change_7d = (
            it.get("change7d")
            or it.get("change_7d")
            or it.get("weekChange")
            or it.get("sevenDayChange")
            or it.get("rise")
        )
        volume = it.get("volume") or it.get("turnover") or it.get("dayVolume") or it.get("sellNum")
        steam_price = it.get("steamPrice") or it.get("steam_price") or it.get("steamSellPrice")
        platform_price = it.get("platformPrice") or it.get("platform_price") or it.get("sellPrice")
        steam_balance = it.get("steamBalance") or it.get("steam_balance") or it.get("toSteam")
        platform = it.get("platform") or it.get("platformName") or it.get("from") or ""

        if name is None or ratio is None:
            continue

        ratio = normalize_ratio(ratio)
        change_7d = parse_percent(change_7d) if not isinstance(change_7d, (int, float)) else float(change_7d)
        volume = parse_number(volume) if not isinstance(volume, (int, float)) else float(volume)
        steam_price = parse_number(steam_price) if not isinstance(steam_price, (int, float)) else float(steam_price)
        platform_price = parse_number(platform_price) if not isinstance(platform_price, (int, float)) else float(platform_price)
        steam_balance = parse_number(steam_balance) if not isinstance(steam_balance, (int, float)) else float(steam_balance)

        out.append(
            {
                "name": str(name).strip(),
                "change_7d": change_7d,
                "volume": volume,
                "steam_price": steam_price,
                "platform_price": platform_price,
                "steam_balance": steam_balance,
                "ratio": ratio,
                "platform": str(platform),
                "steam_url": "https://steamcommunity.com/market/search?appid=730&q=" + quote(str(name)),
                "updated": str(it.get("updateTime") or it.get("updated") or ""),
                "scraped_at": now_utc().isoformat(),
            }
        )
    return out


# ---------------------------------------------------------------------------
# 从 DOM 表格解析（备用）
# ---------------------------------------------------------------------------
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

        name = col("饰品名称", "商品", "名称")
        if not name:
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
        market_link = col("Steam市场") or ""
        updated = col("更新时间") or ""

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


def scrape_exchange(config):
    url = config.get("site", {}).get("url", "https://smis.club/exchange")
    wait_ms = int(config.get("site", {}).get("wait_ms", 8000))
    filters = config.get("filters", {})

    price_min = filters.get("price_min", 1)
    price_max = filters.get("price_max", 5000)
    page_volume = max(10, min(int(filters.get("volume_min", 50)), 50))

    setup_mode = os.getenv("SMIS_SETUP", "").strip() in {"1", "true", "True", "yes", "YES"}
    headed_env = os.getenv("SMIS_HEADED", "").strip() in {"1", "true", "True", "yes", "YES"}
    headed = setup_mode or headed_env
    manual_wait = int(os.getenv("SMIS_MANUAL_WAIT", "0") or "0")
    if setup_mode and manual_wait <= 0:
        manual_wait = 90

    api_payloads = []

    with sync_playwright() as p:
        BROWSER_DIR.mkdir(parents=True, exist_ok=True)
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_DIR),
            headless=not headed,
            viewport={"width": 1600, "height": 1400},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            args=["--disable-blink-features=AutomationControlled"],
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        page = context.pages[0] if context.pages else context.new_page()

        def on_response(resp):
            try:
                u = resp.url or ""
                if "exchange" in u and ("/api/" in u or "commodity" in u):
                    if resp.status == 200:
                        try:
                            data = resp.json()
                            api_payloads.append(data)
                            keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
                            print(f"[info] 拦截接口 {resp.status}: {u[:90]}  keys={keys}")
                        except Exception:
                            txt = resp.text()[:200]
                            print(f"[info] 接口非 JSON: {u[:60]} -> {txt}")
                    else:
                        print(f"[warn] 接口状态 {resp.status}: {u[:90]}")
            except Exception:
                pass

        page.on("response", on_response)

        print(f"[info] 打开 {url}  (headed={headed}, manual_wait={manual_wait}s)")
        page.goto(url, wait_until="domcontentloaded", timeout=90000)

        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(2000)

        if manual_wait > 0:
            print(f"[info] 请在弹出的浏览器里完成验证码（如有），等待 {manual_wait} 秒...")
            print("[info] 看到表格有数据后，程序会自动继续；也可等倒计时结束")
            page.wait_for_timeout(manual_wait * 1000)

        # 设置筛选
        print(f"[info] 设置筛选: 价格 {price_min}~{price_max}, 成交量>={page_volume}")
        try:
            filled = page.evaluate(
                """([minV, maxV, volV]) => {
                    const isFillable = (el) => {
                        if (!el || el.disabled || el.readOnly) return false;
                        const t = (el.type || '').toLowerCase();
                        if (['radio','checkbox','hidden','button','submit'].includes(t)) return false;
                        return true;
                    };
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
                    if (priceInputs.length < 2)
                        priceInputs = Array.from(document.querySelectorAll('input')).filter(isFillable);
                    const setVal = (el, v) => {
                        el.focus(); el.value = String(v);
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    };
                    if (priceInputs.length >= 2) {
                        setVal(priceInputs[0], minV);
                        setVal(priceInputs[1], maxV);
                    }
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
                    if (volInput) setVal(volInput, volV);
                    return { priceCount: priceInputs.length, hasVol: !!volInput };
                }""",
                [price_min, price_max, page_volume],
            )
            print(f"[info] 输入框设置结果: {filled}")
        except Exception as e:
            print(f"[warn] 设置筛选失败: {e}")

        page.wait_for_timeout(500)

        for text in ["应用设置", "应用"]:
            btn = page.locator(f"button:has-text('{text}')")
            if btn.count() > 0:
                try:
                    btn.first.click(timeout=3000)
                    print(f"[info] 已点击「{text}」")
                    break
                except Exception:
                    continue

        print("[info] 等待数据...")
        for attempt in range(20):
            page.wait_for_timeout(1000)
            if api_payloads:
                print(f"[info] 已拦截到 {len(api_payloads)} 个接口响应")
                break
            ready = page.evaluate(
                """() => {
                    const trs = document.querySelectorAll('table tbody tr, .el-table__body tr, .el-table__row');
                    for (const tr of trs) {
                        const t = (tr.innerText || '').trim();
                        if (t && !t.includes('No Data') && t.length > 15 && /\\d/.test(t)) return true;
                    }
                    return false;
                }"""
            )
            if ready:
                print(f"[info] DOM 检测到数据行 (attempt {attempt+1})")
                break
            if attempt in (5, 10, 15):
                print(f"[info] 等待中... ({attempt+1}/20)  若有验证码请在窗口中完成")
                try:
                    page.locator("button:has-text('应用设置')").first.click(timeout=1000)
                except Exception:
                    pass

        page.wait_for_timeout(max(1000, wait_ms // 3))

        # 优先用 API 数据
        items = []
        for payload in api_payloads:
            items.extend(items_from_api_payload(payload))
        if items:
            print(f"[info] 从 API 解析到 {len(items)} 条")
            context.close()
            return items

        # 退回 DOM
        extracted = page.evaluate(
            """() => {
                const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                let headers = [];
                const ths = document.querySelectorAll('table thead th, .el-table__header th');
                if (ths.length) headers = Array.from(ths).map(th => clean(th.innerText));
                const rowSelectors = [
                    'table tbody tr', '.el-table__body-wrapper tbody tr',
                    '.el-table__body tr', '.el-table__row', 'table tr'
                ];
                let rows = [];
                for (const sel of rowSelectors) {
                    const good = [];
                    for (const el of document.querySelectorAll(sel)) {
                        if (el.querySelector('th')) continue;
                        const cells = Array.from(el.querySelectorAll('td, .el-table__cell'));
                        let texts = cells.length >= 4
                            ? cells.map(c => clean(c.innerText))
                            : clean(el.innerText).split('\\n').map(clean).filter(Boolean);
                        const joined = texts.join(' ');
                        if (!joined || joined.includes('No Data') || joined.length < 8 || !/\\d/.test(joined))
                            continue;
                        good.push(texts);
                    }
                    if (good.length >= 3) { rows = good; break; }
                    if (good.length > rows.length) rows = good;
                }
                return { headers, rows, rowCount: rows.length };
            }"""
        )

        # 调试：保存页面快照
        if not extracted.get("rowCount"):
            try:
                DEBUG_DIR.mkdir(parents=True, exist_ok=True)
                snap = DEBUG_DIR / "last_page.html"
                snap.write_text(page.content(), "utf-8")
                page.screenshot(path=str(DEBUG_DIR / "last_page.png"), full_page=True)
                print(f"[warn] 无数据，已保存调试文件到 {DEBUG_DIR}")
            except Exception as e:
                print(f"[warn] 保存调试文件失败: {e}")

        context.close()

    headers = extracted.get("headers") or []
    raw_rows = extracted.get("rows") or []
    print(f"[info] 表头: {headers}")
    print(f"[info] 原始行数: {extracted.get('rowCount', 0)}")

    if not raw_rows:
        print("[warn] 未解析到任何数据行（验证码未过 或 接口 401）")
        return []

    results = [{"headers": headers, "cells": cells} for cells in raw_rows]
    return parse_rows(results)


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
    return (
        "⏰ <b>7天保护期到期复核</b>\n\n"
        f"<b>{item.get('name', record['name'])}</b>\n"
        f"当前平台：{item.get('platform', record.get('platform', '—'))}\n"
        f"当前挂刀比例：<b>{ratio_text}</b>\n"
        f"当前7日涨跌：<b>{fmt_pct(item.get('change_7d'))}</b>\n"
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

    for i, r in enumerate(rows[:8]):
        print(
            f"  [{i+1}] {r['name'][:24]:24s}  ratio={r['ratio']:.4f}  "
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

    for key, record in list(candidates.items()):
        if record.get("status") != "waiting":
            continue
        try:
            unlock_at = datetime.fromisoformat(record["unlock_at"])
        except Exception:
            continue
        if now_utc() < unlock_at:
            continue
        item = by_key.get(key) or {
            **record,
            "ratio": None,
            "change_7d": None,
            "platform_price": None,
            "steam_price": None,
            "steam_balance": None,
            "volume": None,
        }
        try:
            telegram_send(token, chat_id, mature_message(item, record, sale_method))
            print(f"[tg] 已推送到期复核: {record.get('name')}")
        except Exception as e:
            print(f"[error] Telegram 发送失败: {e}")
        record["status"] = "matured"
        record["matured_at"] = now_utc().isoformat()
        changed = True

    max_records = int(config.get("state", {}).get("max_records", 3000))
    if len(candidates) > max_records:
        matured = [(k, v) for k, v in candidates.items() if v.get("status") == "matured"]
        matured.sort(key=lambda kv: kv[1].get("matured_at", ""))
        for k, _ in matured[: max(0, len(candidates) - max_records)]:
            candidates.pop(k, None)
            changed = True

    if changed:
        save_json(DATA_FILE, state)

    summary = {"scraped": len(rows), "qualified": len(qualified), "new_candidates": new_count}
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
