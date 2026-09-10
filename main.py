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
import time
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
    """低配 VPS 优化：仍完整走「填筛选 → 应用设置 → 用应用后的接口数据」。"""
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

    # SMIS_PERSISTENT=1 时用用户目录（本机过验证）；VPS 默认不用，省内存
    use_persistent = os.getenv("SMIS_PERSISTENT", "").strip() in {"1", "true", "True", "yes", "YES"} or setup_mode

    chrome_args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--no-sandbox",
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
        "--js-flags=--max-old-space-size=192",
    ]
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
    viewport = {"width": 900, "height": 700}

    max_attempts = 2
    last_err = None

    for attempt in range(1, max_attempts + 1):
        api_payloads = []
        post_apply_payloads = []
        apply_clicked = False
        context = None
        browser = None

        try:
            with sync_playwright() as p:
                if use_persistent:
                    BROWSER_DIR.mkdir(parents=True, exist_ok=True)
                    context = p.chromium.launch_persistent_context(
                        user_data_dir=str(BROWSER_DIR),
                        headless=not headed,
                        viewport=viewport,
                        locale="zh-CN",
                        timezone_id="Asia/Shanghai",
                        args=chrome_args,
                        user_agent=ua,
                    )
                    page = context.pages[0] if context.pages else context.new_page()
                else:
                    browser = p.chromium.launch(headless=not headed, args=chrome_args)
                    context = browser.new_context(
                        viewport=viewport,
                        locale="zh-CN",
                        timezone_id="Asia/Shanghai",
                        user_agent=ua,
                    )
                    page = context.new_page()

                def on_response(resp):
                    try:
                        u = resp.url or ""
                        if "commodity/exchange" not in u and not (
                            "exchange" in u and "/api/" in u
                        ):
                            return
                        if resp.status != 200:
                            print(f"[warn] 接口状态 {resp.status}: {u[:90]}")
                            return
                        data = resp.json()
                        api_payloads.append(data)
                        if apply_clicked:
                            post_apply_payloads.append(data)
                        keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
                        tag = "应用后" if apply_clicked else "应用前"
                        print(f"[info] 拦截接口({tag}) {resp.status}: {u[:80]} keys={keys}")
                    except Exception:
                        pass

                page.on("response", on_response)

                print(
                    f"[info] 打开 {url} (attempt={attempt}/{max_attempts}, "
                    f"headed={headed}, persistent={use_persistent})"
                )
                page.goto(url, wait_until="domcontentloaded", timeout=90000)
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                page.wait_for_timeout(1500)

                if manual_wait > 0:
                    print(f"[info] 手动验证等待最多 {manual_wait}s...")
                    for _w in range(manual_wait):
                        page.wait_for_timeout(1000)
                        if any(items_from_api_payload(pl) for pl in api_payloads):
                            print(f"[info] 已有接口数据，提前继续 ({_w+1}s)")
                            break

                # ---------- 填筛选 ----------
                print(f"[info] 设置筛选: 价格 {price_min}~{price_max}, 成交量>={page_volume}")

                def fill_near_label(label_text, values):
                    loc = page.get_by_text(label_text, exact=False).first
                    row = loc.locator(
                        "xpath=ancestor::div[contains(@class,'el-') or contains(@class,'form') or contains(@class,'item')][1]"
                    )
                    if row.count() == 0:
                        row = loc.locator("xpath=ancestor::div[2]")
                    inputs = row.locator(
                        "input:not([type='radio']):not([type='checkbox']):not([type='hidden'])"
                    )
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
                    try:
                        rst = page.locator("button:has-text('重置设置')")
                        if rst.count() > 0:
                            rst.first.click(timeout=2000)
                            page.wait_for_timeout(600)
                            print("[info] 已点重置设置")
                    except Exception:
                        pass

                    n_price = fill_near_label("价格区间", [price_min, price_max])
                    if n_price < 2:
                        print("[warn] 价格区间定位失败，尝试 el-input")
                        all_in = page.locator("input.el-input__inner")
                        vis = []
                        for i in range(min(all_in.count(), 12)):
                            try:
                                if all_in.nth(i).is_visible():
                                    vis.append(i)
                            except Exception:
                                pass
                        if len(vis) >= 2:
                            all_in.nth(vis[0]).fill(str(price_min))
                            all_in.nth(vis[1]).fill(str(price_max))
                            if len(vis) >= 3:
                                all_in.nth(vis[2]).fill(str(page_volume))
                    else:
                        fill_near_label("日成交量", [page_volume])

                    try:
                        shown = page.evaluate(
                            """() => {
                            const pick = (label) => {
                              const nodes = Array.from(document.querySelectorAll('div,span,label'));
                              for (const n of nodes) {
                                const t = (n.textContent || '').trim();
                                if (t.startsWith(label) || t.includes(label)) {
                                  let p = n;
                                  for (let i=0;i<5 && p;i++, p=p.parentElement) {
                                    const ins = Array.from(p.querySelectorAll('input')).filter(el => {
                                      const ty=(el.type||'').toLowerCase();
                                      return !['radio','checkbox','hidden'].includes(ty);
                                    });
                                    if (ins.length) return ins.map(el => el.value);
                                  }
                                }
                              }
                              return [];
                            };
                            return { price: pick('价格'), volume: pick('日成交') };
                            }"""
                        )
                        print(f"[info] 读回输入框: {shown}")
                    except Exception as e:
                        print(f"[warn] 读回失败: {e}")
                except Exception as e:
                    print(f"[warn] 设置筛选失败: {e}")
                    raise

                page.wait_for_timeout(400)

                # ---------- 应用设置（之后的接口才是筛选后结果）----------
                clicked = False
                for text in ["应用设置", "应用"]:
                    btn = page.locator(f"button:has-text('{text}')")
                    if btn.count() > 0:
                        try:
                            # 清空「应用前」干扰：只认应用后的包
                            post_apply_payloads.clear()
                            apply_clicked = True
                            btn.first.click(timeout=3000)
                            print(f"[info] 已点击「{text}」")
                            clicked = True
                            break
                        except Exception as e:
                            print(f"[warn] 点击 {text} 失败: {e}")
                if not clicked:
                    print("[warn] 未找到应用按钮，仍等待接口...")
                    apply_clicked = True

                # ---------- 等待「应用后」接口 ----------
                print("[info] 等待筛选后的接口数据...")
                got = []
                for i in range(25):
                    try:
                        page.wait_for_timeout(800)
                    except Exception as e:
                        print(f"[warn] 等待中页面异常: {e}")
                        break
                    if post_apply_payloads:
                        for pl in post_apply_payloads:
                            part = items_from_api_payload(pl)
                            if part:
                                got = part
                                break
                        if got:
                            print(f"[info] 应用后接口解析到 {len(got)} 条 (第 {i+1} 次等待)")
                            break
                    # 若应用后没有新包但应用前有，有时站点不重新请求——再点一次应用
                    if i in (8, 16) and not post_apply_payloads:
                        try:
                            page.locator("button:has-text('应用设置')").first.click(timeout=1500)
                            print("[info] 再次点击应用设置")
                        except Exception:
                            pass

                # 兜底：只有应用前数据时也用（并打日志）
                if not got:
                    for pl in reversed(api_payloads):
                        part = items_from_api_payload(pl)
                        if part:
                            got = part
                            print(
                                f"[warn] 未捕获到应用后接口，暂用已有数据 {len(got)} 条"
                                "（可能未重新请求）"
                            )
                            break

                # 尽快关浏览器释放内存
                try:
                    context.close()
                except Exception:
                    pass
                try:
                    if browser:
                        browser.close()
                except Exception:
                    pass

                if got:
                    return got

                # DOM 兜底（尽量不用）
                print("[warn] 接口无有效列表，尝试结束本轮")
                last_err = RuntimeError("无有效商品数据")
        except Exception as e:
            last_err = e
            print(f"[error] 第 {attempt} 次尝试失败: {e}")
            try:
                if context:
                    context.close()
            except Exception:
                pass
            try:
                if browser:
                    browser.close()
            except Exception:
                pass
            if attempt < max_attempts:
                print("[info] 1.5s 后重试...")
                time.sleep(1.5)

    if last_err:
        raise last_err
    return []



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
