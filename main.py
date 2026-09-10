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
def items_from_api_payload(payload, _depth=0):
    """解析 smis /api/commodity/exchange 返回。"""
    if payload is None:
        return []

    candidates = []
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        data = payload.get("data", payload)
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, dict):
            for k in ("list", "records", "rows", "items", "result"):
                if isinstance(data.get(k), list):
                    candidates = data[k]
                    break
            if not candidates:
                # 递归浅搜
                for v in data.values():
                    if isinstance(v, list) and v and isinstance(v[0], dict):
                        candidates = v
                        break

    if not candidates and isinstance(payload, dict):
        try:
            preview = {k: type(v).__name__ for k, v in list(payload.items())[:8]}
            print(f"[debug] API 结构无法识别: {preview}")
        except Exception:
            pass
        return []

    out = []
    for it in candidates:
        if not isinstance(it, dict):
            continue
        lower = {str(k).lower(): v for k, v in it.items()}

        def g(*names):
            for n in names:
                if n.lower() in lower and lower[n.lower()] is not None:
                    return lower[n.lower()]
            return None

        # smis 字段：id / cnName / extremeRatio / platform / priceRatio7 / steamTransactionQuantity
        cid = g("id", "commodityId", "commodity_id", "goodsId", "goodId")
        name = g("cnName", "name", "commodityName", "goodsName", "hashName", "market_hash_name")
        if not name:
            continue

        platform = str(g("platform", "platformName") or "").upper()

        # 挂刀比例：优先 extremeRatio，再按平台取 *ToSteamBySellRatio
        ratio = g("extremeRatio", "ratio", "exchangeRatio")
        if ratio is None and platform:
            plat_key = {
                "BUFF": "buffToSteamBySellRatio",
                "UUYP": "uuypToSteamBySellRatio",
                "C5": "c5ToSteamBySellRatio",
                "IGXE": "igxeToSteamBySellRatio",
                "ECO": "ecoToSteamBySellRatio",
            }.get(platform)
            if plat_key:
                ratio = g(plat_key)

        ratio = normalize_ratio(ratio)
        if ratio is None:
            continue

        change_7d = g("priceRatio7", "change7d", "change_7d", "weekChange", "rise")
        if isinstance(change_7d, (int, float)):
            # smis 的 priceRatio7 可能是涨跌比例（如 -0.0609 或 -6.09）
            change_7d = float(change_7d)
            if abs(change_7d) <= 1.5:
                change_7d = change_7d * 100
        else:
            change_7d = parse_percent(change_7d)

        volume = g("steamTransactionQuantity", "volume", "turnover", "dayVolume", "sellNum")
        if not isinstance(volume, (int, float)):
            volume = parse_number(volume)

        steam_price = g("steamSellPrice", "steamPrice", "steam_price")
        if not isinstance(steam_price, (int, float)):
            steam_price = parse_number(steam_price)

        # 平台售价：按 platform 取对应 sellPrice
        platform_price = None
        if platform:
            pk = {
                "BUFF": "buffSellPrice",
                "UUYP": "uuypSellPrice",
                "C5": "c5SellPrice",
                "IGXE": "igxeSellPrice",
                "ECO": "ecoSellPrice",
            }.get(platform)
            if pk:
                platform_price = g(pk)
        if platform_price is None:
            platform_price = g("platformPrice", "platform_price", "sellPrice")
        if not isinstance(platform_price, (int, float)):
            platform_price = parse_number(platform_price)

        # 到手 Steam 余额约 = 平台价 / 比例
        steam_balance = g("steamBalance", "steam_balance", "toSteam")
        if steam_balance is None and platform_price and ratio and ratio > 0:
            try:
                steam_balance = float(platform_price) / float(ratio)
            except Exception:
                steam_balance = None
        if not isinstance(steam_balance, (int, float)):
            steam_balance = parse_number(steam_balance)

        out.append({
            "name": str(name).strip(),
            "commodity_id": cid,
            "change_7d": change_7d,
            "volume": volume,
            "steam_price": steam_price,
            "platform_price": platform_price,
            "steam_balance": steam_balance,
            "ratio": ratio,
            "platform": platform,
            "steam_url": "https://steamcommunity.com/market/search?appid=730&q=" + quote(str(name)),
            "smis_url": f"https://smis.club/commodity/{cid}" if cid is not None else None,
            "updated": str(g("updateTime", "updated") or ""),
            "scraped_at": now_utc().isoformat(),
        })
    return out



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
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",      # Docker/小内存 VPS 必加
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-default-apps",
                "--disable-sync",
                "--disable-translate",
                "--mute-audio",
                "--no-first-run",
                "--no-zygote",
                "--renderer-process-limit=1",
                "--js-flags=--max-old-space-size=256",
            ],
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
            print(f"[info] 请在弹出的浏览器里完成验证码（如有），等待最多 {manual_wait} 秒...")
            print("[info] 看到表格有数据后会提前继续")
            for _w in range(manual_wait):
                page.wait_for_timeout(1000)
                if api_payloads:
                    # 检查是否真有列表数据
                    ok = False
                    for pl in api_payloads:
                        if items_from_api_payload(pl):
                            ok = True
                            break
                    if ok:
                        print(f"[info] 已拿到有效接口数据，提前结束等待 (第 {_w+1} 秒)")
                        break
                ready = page.evaluate("""() => {
                    const trs = document.querySelectorAll('table tbody tr, .el-table__body tr');
                    for (const tr of trs) {
                        const t = (tr.innerText || '').trim();
                        if (t && !t.includes('No Data') && t.length > 15) return true;
                    }
                    return false;
                }""")
                if ready and _w >= 5:
                    print(f"[info] DOM 已有数据，提前结束等待 (第 {_w+1} 秒)")
                    break

        # 设置筛选（用 Playwright 精确定位「价格区间」「日成交量」旁的输入框）
        print(f"[info] 设置筛选: 价格 {price_min}~{price_max}, 成交量>={page_volume}")

        def fill_near_label(label_text, values):
            """找到包含 label_text 的那一行，填写其中的 input。"""
            # 精确文本节点
            loc = page.locator(f"text={label_text}").first
            if loc.count() == 0:
                loc = page.get_by_text(label_text, exact=False).first
            # 向上找较近的容器
            row = loc.locator("xpath=ancestor::div[contains(@class,'el-') or contains(@class,'form') or contains(@class,'item')][1]")
            if row.count() == 0:
                row = loc.locator("xpath=ancestor::div[2]")
            inputs = row.locator("input:not([type='radio']):not([type='checkbox']):not([type='hidden'])")
            n = inputs.count()
            print(f"[info] 标签「{label_text}」附近 input 数量: {n}")
            for i, v in enumerate(values):
                if i >= n:
                    break
                el = inputs.nth(i)
                el.click(timeout=3000)
                el.fill("")
                el.fill(str(v))
                el.press("Tab")
            return n

        try:
            # 先点「重置设置」避免沿用错误缓存
            try:
                rst = page.locator("button:has-text('重置设置')")
                if rst.count() > 0:
                    rst.first.click(timeout=2000)
                    page.wait_for_timeout(800)
                    print("[info] 已点重置设置")
            except Exception:
                pass

            n_price = fill_near_label("价格区间", [price_min, price_max])
            if n_price < 2:
                # 兜底：页面上所有可见 number/text 输入，按顺序找价格行
                print("[warn] 价格区间定位失败，尝试全局输入框")
                all_in = page.locator("input.el-input__inner, input[type='number'], input[type='text']")
                vals = []
                for i in range(min(all_in.count(), 12)):
                    try:
                        vis = all_in.nth(i).is_visible()
                        if vis:
                            vals.append(i)
                    except Exception:
                        pass
                # 通常价格两个 + 成交量一个
                if len(vals) >= 2:
                    all_in.nth(vals[0]).fill(str(price_min))
                    all_in.nth(vals[1]).fill(str(price_max))
                    if len(vals) >= 3:
                        all_in.nth(vals[2]).fill(str(page_volume))
            else:
                fill_near_label("日成交量", [page_volume])

            # 读回当前值确认
            try:
                shown = page.evaluate("""() => {
                    const pick = (label) => {
                        const nodes = Array.from(document.querySelectorAll('div,span,label'));
                        for (const n of nodes) {
                            if ((n.childNodes[0] && n.childNodes[0].textContent || n.textContent || '').trim().startsWith(label)) {
                                let p = n;
                                for (let i=0;i<5 && p;i++, p=p.parentElement) {
                                    const ins = Array.from(p.querySelectorAll('input')).filter(el => {
                                        const t=(el.type||'').toLowerCase();
                                        return !['radio','checkbox','hidden'].includes(t);
                                    });
                                    if (ins.length) return ins.map(el => el.value);
                                }
                            }
                        }
                        return [];
                    };
                    return { price: pick('价格'), volume: pick('日成交') };
                }""")
                print(f"[info] 读回输入框: {shown}")
            except Exception as e:
                print(f"[warn] 读回失败: {e}")
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

        no_data = page.evaluate(r"""() => {
            const body = document.body.innerText || '';
            return body.includes('No Data') && !/0\.\d{2,}/.test(body);
        }""")
        if no_data and not api_payloads:
            print("[warn] 页面 No Data，尝试重置筛选后重新加载")
            try:
                page.locator("button:has-text('重置设置')").first.click(timeout=2000)
                page.wait_for_timeout(600)
                page.locator("button:has-text('应用设置')").first.click(timeout=2000)
                page.wait_for_timeout(3500)
            except Exception as e:
                print(f"[warn] 重置失败: {e}")

        # 优先用 API 数据
        items = []
        for payload in api_payloads:
            part = items_from_api_payload(payload)
            items.extend(part)
            if not part:
                try:
                    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
                    (DEBUG_DIR / "last_api.json").write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2)[:80000], "utf-8"
                    )
                    print("[debug] 已保存 last_api.json 供分析字段")
                except Exception as e:
                    print(f"[debug] 保存 API 失败: {e}")
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



def smis_item_url(item):
    """详情页 https://smis.club/commodity/{id}"""
    if item.get("smis_url"):
        return item["smis_url"]
    cid = item.get("commodity_id") or item.get("id")
    if cid is not None and str(cid).strip() != "":
        return f"https://smis.club/commodity/{cid}"
    # 无 id 时退回挂刀列表页
    return "https://smis.club/exchange"



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


def discovery_batch_message(items, sale_method, hold_days):
    """最多 10 条合并成一条 TG 消息。"""
    lines = [
        f"🟢 <b>挂刀机会 Top {len(items)}</b>",
        f"策略: {sale_method} · 保护期约 {hold_days:g} 天",
        "━━━━━━━━━━━━━━━━",
    ]
    for i, item in enumerate(items, 1):
        url = smis_item_url(item)
        name = item.get("name") or "?"
        ratio = item.get("ratio")
        ratio_s = f"{ratio:.4f}" if ratio is not None else "—"
        ch_s = fmt_pct(item.get("change_7d"))
        vol = item.get("volume")
        vol_s = f"{vol:g}" if isinstance(vol, (int, float)) else "—"
        plat = item.get("platform") or "—"
        pp = fmt_money(item.get("platform_price"))
        sb = fmt_money(item.get("steam_balance"))
        lines.append(
            f"<b>{i}. <a href=\"{url}\">{name}</a></b>\n"
            f"   比例 <b>{ratio_s}</b> · 7日 <b>{ch_s}</b> · {plat}\n"
            f"   平台价 {pp} → 到手 {sb} · 量 {vol_s}"
        )
    lines.append("━━━━━━━━━━━━━━━━")
    lines.append('<a href="https://smis.club/exchange">打开挂刀行情</a>')
    return "\n".join(lines)


def mature_message(item, record, sale_method):
    ratio = item.get("ratio")
    ratio_text = f"{ratio:.4f}" if ratio is not None else "—"
    url = smis_item_url(item if item.get("name") else record)
    name = item.get("name", record.get("name"))
    return (
        "⏰ <b>7天保护期到期复核</b>\n\n"
        f"<b><a href=\"{url}\">{name}</a></b>\n"
        f"平台：{item.get('platform', record.get('platform', '—'))}\n"
        f"当前挂刀比例：<b>{ratio_text}</b>\n"
        f"当前7日涨跌：<b>{fmt_pct(item.get('change_7d'))}</b>\n"
        f"平台价：{fmt_money(item.get('platform_price'))}\n"
        f"Steam售价：{fmt_money(item.get('steam_price'))}\n"
        f"到手余额：{fmt_money(item.get('steam_balance'))}\n\n"
        f"建议：按 <b>{sale_method}</b> 核对后出售。\n"
        f'<a href="https://smis.club/exchange">挂刀行情</a>'
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
            f"7d={r['change_7d'] if r['change_7d'] is None else round(r['change_7d'], 2)}  vol={r['volume']}  plat={r['platform']}"
        )

    by_key = {key_for(x): x for x in rows}
    print(
        f"[info] 过滤条件: ratio<={filters.get('ratio_max')} "
        f"跌幅>={filters.get('drop_min_pct')}% "
        f"vol>={filters.get('volume_min')} "
        f"价 {filters.get('price_min')}~{filters.get('price_max')}"
    )
    qualified = []
    skip_shown = 0
    for x in rows:
        if qualify(x, filters):
            qualified.append(x)
        elif skip_shown < 5:
            reasons = []
            rm = float(filters.get("ratio_max", 0.70))
            dm = float(filters.get("drop_min_pct", 3))
            vm = float(filters.get("volume_min", 50))
            if x["ratio"] is None or x["ratio"] > rm:
                reasons.append(f"ratio={x['ratio']}>{rm}")
            if x["change_7d"] is None or x["change_7d"] > -dm:
                reasons.append(f"7d={x['change_7d']}未跌够{dm}%")
            if x["volume"] is not None and x["volume"] < vm:
                reasons.append(f"vol={x['volume']}<{vm}")
            print(f"  [skip] {str(x['name'])[:18]}: {', '.join(reasons) or '其他'}")
            skip_shown += 1
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
                "steam_url": item.get("steam_url"),
                "smis_url": smis_item_url(item),
                "commodity_id": item.get("commodity_id"),
                "first_seen": now_utc().isoformat(),
                "unlock_at": unlock.isoformat(),
                "discovery_ratio": item["ratio"],
                "discovery_change_7d": item["change_7d"],
                "status": "waiting",
            }
            new_count += 1
            changed = True

    # 只要有符合条件的，就推送 Top（最多 10），链接用 commodity/{id}
    batch = qualified[:10]
    if batch:
        try:
            telegram_send(
                token,
                chat_id,
                discovery_batch_message(batch, sale_method, hold_days),
            )
            print(f"[tg] 已推送 Top {len(batch)}（本轮新发现 {new_count}）")
            for it in batch:
                print(f"  → id={it.get('commodity_id')} {it['name']} {smis_item_url(it)}")
        except Exception as e:
            print(f"[error] Telegram 发送失败: {e}")
    else:
        print("[info] 无符合条件商品，不推送")

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
