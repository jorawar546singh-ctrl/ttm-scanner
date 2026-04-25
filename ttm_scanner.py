"""
TTM Breakout Scanner  --  with Fundamental ELITE layer + streak history logging
-------------------------------------------------------------------------------
"""
import time, os, json, re
from datetime import datetime
import requests
import pandas as pd
import yfinance as yf
from bs4 import BeautifulSoup

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
FMP_API_KEY        = os.environ.get("FMP_API_KEY", "")
SEND_TELEGRAM = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
FUNDAMENTALS_ENABLED = bool(FMP_API_KEY)

DARVAS_BOX_DAYS = 14
VOLUME_MULTIPLIER = 2.0
MIN_PRICE = 2.0
MAX_PRICE = 100.0
MAX_RESULTS_TO_CHECK = 80
TOP_N_TO_SHOW = 12
DEDUP_HOURS = 6

DEDUP_FILE = "alerted.log"
LATEST_JSON = "latest.json"
HISTORY_JSON = "history.json"
ALERTED_HISTORY = "alerted_history.json"   # NEW: 30-day breakout record
HISTORY_RETENTION_DAYS = 30

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
FMP_BASE = "https://financialmodelingprep.com/api/v3"

def script_path(name):
    return os.path.join(SCRIPT_DIR, name)


# ============================================================
# Candidate sources
# ============================================================
def get_finviz_candidates():
    url = ("https://finviz.com/screener.ashx?v=111&"
           "f=cap_smallover,sh_avgvol_o300,sh_price_1to100,sh_relvol_o1.5,ta_gap_u,ta_perf_1w5o&ft=4")
    print("  [Finviz] fetching...")
    try:
        r = requests.get(url, headers=DEFAULT_HEADERS, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print(f"    Finviz failed: {e}")
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    tickers = []
    for link in soup.find_all("a", class_="tab-link"):
        href = link.get("href", "")
        if href.startswith("quote.ashx?t="):
            t = link.text.strip()
            if t and t not in tickers:
                tickers.append(t)
    print(f"    Finviz returned {len(tickers)} candidates.")
    return tickers


def get_yahoo_candidates():
    print("  [Yahoo] fetching most-active + gainers...")
    pages = [
        ("most-active", "https://finance.yahoo.com/markets/stocks/most-active/"),
        ("gainers",     "https://finance.yahoo.com/markets/stocks/gainers/"),
    ]
    tickers = []
    for label, url in pages:
        try:
            r = requests.get(url, headers=DEFAULT_HEADERS, timeout=15)
            if r.status_code != 200:
                print(f"    Yahoo {label} -> status {r.status_code}")
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            found = []
            for a in soup.find_all("a", href=True):
                m = re.match(r"^/quote/([A-Z][A-Z0-9\-]{0,5})(?:[/?]|$)", a["href"])
                if m:
                    sym = m.group(1).upper()
                    if sym.isalpha() and len(sym) <= 5 and sym not in found:
                        found.append(sym)
            for sym in found:
                if sym not in tickers:
                    tickers.append(sym)
            print(f"    Yahoo {label} returned {len(found)} tickers.")
        except Exception as e:
            print(f"    Yahoo {label} error: {e}")
    print(f"    Yahoo total unique: {len(tickers)}")
    return tickers


def get_all_candidates():
    print("Gathering candidates from all sources...")
    finviz = get_finviz_candidates()
    yahoo  = get_yahoo_candidates()
    seen = set(); combined = []
    for t in finviz + yahoo:
        if t not in seen:
            seen.add(t); combined.append(t)
    print(f"  Combined: {len(combined)} tickers (Finviz {len(finviz)} + Yahoo {len(yahoo)})\n")
    return combined[:MAX_RESULTS_TO_CHECK]


# ============================================================
# Darvas + scoring
# ============================================================
def check_darvas_breakout(ticker):
    try:
        data = yf.download(ticker, period="60d", interval="1d", progress=False, auto_adjust=False)
    except Exception:
        return None
    if data is None or len(data) < DARVAS_BOX_DAYS + 5:
        return None
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    today = data.iloc[-1]
    today_close = float(today["Close"]); today_volume = float(today["Volume"])
    if not (MIN_PRICE <= today_close <= MAX_PRICE):
        return None
    prior = data.iloc[-(DARVAS_BOX_DAYS + 1):-1]
    box_top = float(prior["High"].max()); box_bottom = float(prior["Low"].min())
    if today_close <= box_top:
        return None
    vol_lookback = min(20, len(data) - 1)
    avg_vol = float(data["Volume"].iloc[-(vol_lookback + 1):-1].mean())
    if avg_vol <= 0 or today_volume < avg_vol * VOLUME_MULTIPLIER:
        return None

    vol_ratio = today_volume / avg_vol
    pct_above_box = (today_close - box_top) / box_top * 100
    closes = data["Close"].astype(float)
    ma20 = float(closes.iloc[-20:].mean())
    ma20_prev = float(closes.iloc[-21:-1].mean())
    ma_rising = ma20 > ma20_prev
    box_range_pct = (box_top - box_bottom) / box_top * 100
    stop = box_top * 0.98
    target = today_close * 1.30
    risk = today_close - stop
    reward = target - today_close
    rr_ratio = reward / risk if risk > 0 else 0
    risk_pct = risk / today_close * 100
    spark = [round(float(x), 2) for x in closes.iloc[-30:].tolist()]

    hit = {
        "ticker": ticker, "price": round(today_close, 2),
        "box_top": round(box_top, 2), "box_bottom": round(box_bottom, 2),
        "pct_above_box": round(pct_above_box, 2),
        "vol_today": int(today_volume), "vol_avg_20d": int(avg_vol),
        "vol_ratio": round(vol_ratio, 2),
        "suggested_stop": round(stop, 2), "target_30pct": round(target, 2),
        "risk_pct": round(risk_pct, 2), "rr_ratio": round(rr_ratio, 2),
        "box_range_pct": round(box_range_pct, 2), "ma20": round(ma20, 2),
        "ma_rising": ma_rising,
        "price_vs_ma20_pct": round((today_close - ma20) / ma20 * 100, 2),
        "spark": spark,
    }
    hit.update(score_breakout(hit))
    return hit


def score_breakout(h):
    v = h["vol_ratio"]
    vol_score = 20 if v >= 5 else 18 if v >= 4 else 14 if v >= 3 else 11 if v >= 2.5 else 8 if v >= 2 else 4
    p = h["pct_above_box"]
    clarity_score = 20 if p >= 6 else 16 if p >= 4 else 13 if p >= 2.5 else 10 if p >= 1.5 else 6 if p >= 0.5 else 3
    r = h["box_range_pct"]
    box_score = 20 if r <= 10 else 16 if r <= 15 else 12 if r <= 20 else 8 if r <= 25 else 5 if r <= 35 else 2
    rr = h["rr_ratio"]
    rr_score = 20 if rr >= 5 else 16 if rr >= 4 else 12 if rr >= 3 else 8 if rr >= 2 else 5 if rr >= 1.5 else 2
    pct_ma = h["price_vs_ma20_pct"]; rising = h["ma_rising"]
    if rising and pct_ma >= 10:  trend_score = 20
    elif rising and pct_ma >= 5: trend_score = 16
    elif rising and pct_ma >= 0: trend_score = 12
    elif rising:                 trend_score = 8
    elif pct_ma >= 5:            trend_score = 8
    elif pct_ma >= 0:            trend_score = 5
    else:                        trend_score = 2

    total = vol_score + clarity_score + box_score + rr_score + trend_score
    if total >= 90:   grade, tier, emoji = "A+", "CLEAN", "\U0001F7E2"
    elif total >= 85: grade, tier, emoji = "A",  "CLEAN", "\U0001F7E2"
    elif total >= 75: grade, tier, emoji = "B+", "SOLID", "\U0001F7E1"
    elif total >= 70: grade, tier, emoji = "B",  "SOLID", "\U0001F7E1"
    elif total >= 60: grade, tier, emoji = "C+", "MARGINAL", "\U0001F7E0"
    elif total >= 55: grade, tier, emoji = "C",  "MARGINAL", "\U0001F7E0"
    else:             grade, tier, emoji = "D",  "WEAK", "\U0001F534"
    return {"score": total, "grade": grade, "tier": tier, "emoji": emoji,
        "score_breakdown": {"volume": vol_score, "clarity": clarity_score,
            "box_quality": box_score, "risk_reward": rr_score, "trend": trend_score}}


# ============================================================
# Fundamentals (unchanged from ELITE layer)
# ============================================================
def fmp_get(endpoint, params=None):
    if not FMP_API_KEY: return None
    params = params or {}; params["apikey"] = FMP_API_KEY
    try:
        r = requests.get(f"{FMP_BASE}/{endpoint}", params=params, timeout=12)
        if r.status_code != 200: return None
        data = r.json()
        if isinstance(data, dict) and data.get("Error Message"): return None
        return data
    except Exception:
        return None


def fetch_fundamentals(ticker):
    d = {}
    for key, ep, params in [
        ("key_metrics", f"key-metrics/{ticker}", {"limit": 2}),
        ("ratios",      f"ratios/{ticker}",       {"limit": 2}),
        ("income_q",    f"income-statement/{ticker}", {"period": "quarter", "limit": 5}),
        ("cashflow",    f"cash-flow-statement/{ticker}", {"limit": 2}),
    ]:
        r = fmp_get(ep, params)
        d[key] = r if isinstance(r, list) else []
    q = fmp_get(f"quote/{ticker}")
    d["quote"] = q[0] if isinstance(q, list) and q else {}
    ins = fmp_get("insider-trading", {"symbol": ticker, "limit": 50})
    d["insider"] = ins if isinstance(ins, list) else []
    inst = fmp_get(f"institutional-holder/{ticker}")
    d["institutional"] = inst if isinstance(inst, list) else []
    return d


def score_fundamentals(ticker, f):
    if not f or not f.get("key_metrics"): return None
    km = (f["key_metrics"] or [{}])[0]
    ratios = (f["ratios"] or [{}])[0]
    income_q = f.get("income_q") or []
    cashflow = (f["cashflow"] or [{}])[0]
    insider = f.get("insider") or []
    institutional = f.get("institutional") or []

    def safe(d, k, default=None):
        v = d.get(k) if isinstance(d, dict) else None
        return v if v is not None else default

    p1 = 0
    try:
        if len(income_q) >= 4:
            eps_q = [safe(q, "eps", 0) or 0 for q in income_q[:4]]
            if eps_q[3]:
                yoy = (eps_q[0] - eps_q[3]) / abs(eps_q[3]) * 100
                p1 += 10 if yoy >= 40 else 7 if yoy >= 25 else 4 if yoy >= 15 else 2 if yoy > 0 else 0
            if len(income_q) >= 5:
                g_now = (eps_q[0] - eps_q[2]) if eps_q[2] else 0
                g_prev = (eps_q[1] - eps_q[3]) if eps_q[3] else 0
                if g_now > g_prev > 0: p1 += 5
        nm = safe(ratios, "netProfitMargin", 0) or 0
        p1 += 5 if nm >= 0.20 else 3 if nm >= 0.10 else 1 if nm >= 0.05 else 0
    except Exception: pass
    p1 = min(20, p1)

    p2 = 0
    peg = safe(km, "pegRatio"); pe = safe(km, "peRatio")
    try:
        if peg and peg > 0:
            p2 += 12 if peg <= 1 else 9 if peg <= 1.5 else 5 if peg <= 2 else 2 if peg <= 3 else 0
        if pe and pe > 0:
            p2 += 8 if pe <= 25 else 5 if pe <= 40 else 2 if pe <= 60 else 0
    except Exception: pass
    p2 = min(20, p2)

    p3 = 0
    try:
        if len(income_q) >= 4:
            rn = safe(income_q[0], "revenue", 0) or 0
            r4 = safe(income_q[3], "revenue", 0) or 0
            if r4 > 0:
                rg = (rn - r4) / r4 * 100
                p3 += 10 if rg >= 30 else 7 if rg >= 20 else 4 if rg >= 10 else 2 if rg > 0 else 0
        roe = safe(km, "roe", 0) or 0
        p3 += 10 if roe >= 0.25 else 7 if roe >= 0.15 else 4 if roe >= 0.10 else 2 if roe > 0 else 0
    except Exception: pass
    p3 = min(20, p3)

    p4 = 0
    try:
        de = safe(km, "debtToEquity")
        if de is not None:
            p4 += 7 if de <= 0.3 else 5 if de <= 0.6 else 3 if de <= 1 else 1 if de <= 2 else 0
        cr = safe(ratios, "currentRatio", 0) or 0
        p4 += 6 if cr >= 2 else 4 if cr >= 1.5 else 2 if cr >= 1 else 0
        fcf = safe(cashflow, "freeCashFlow", 0) or 0
        if fcf > 0: p4 += 7
    except Exception: pass
    p4 = min(20, p4)

    p5 = 0
    try:
        from datetime import timedelta, datetime as dt
        cutoff = dt.now() - timedelta(days=90)
        recent = []
        for tx in insider:
            ds = tx.get("transactionDate") or tx.get("filingDate") or ""
            try: d = dt.fromisoformat(ds[:10])
            except Exception: continue
            if d >= cutoff: recent.append(tx)
        buys = sum(1 for tx in recent if "Purchase" in str(tx.get("transactionType", "")))
        sells = sum(1 for tx in recent if "Sale" in str(tx.get("transactionType", "")))
        if buys > sells and buys >= 2: p5 += 10
        elif buys > 0 and buys >= sells: p5 += 6
        elif sells == 0: p5 += 4
        else: p5 += 2
        if len(institutional) >= 50: p5 += 6
        elif len(institutional) >= 20: p5 += 4
        elif len(institutional) >= 5: p5 += 2
        p5 += 4
    except Exception: pass
    p5 = min(20, p5)

    total = p1 + p2 + p3 + p4 + p5
    if total >= 80: tag, emoji = "ELITE", "\U0001F31F"
    elif total >= 60: tag, emoji = "STRONG", "\u2B50"
    else: tag, emoji = None, None

    return {"fund_score": total, "fund_tag": tag, "fund_emoji": emoji,
        "fund_breakdown": {"earnings": p1, "value": p2, "leader": p3, "balance": p4, "smart_money": p5}}


# ============================================================
# De-dup + alerted history
# ============================================================
def load_recent_alerts():
    recent = set(); path = script_path(DEDUP_FILE)
    if not os.path.exists(path): return recent
    cutoff = time.time() - (DEDUP_HOURS * 3600)
    try:
        with open(path) as f:
            for line in f:
                parts = line.strip().split(",")
                if len(parts) == 2 and float(parts[0]) >= cutoff:
                    recent.add(parts[1])
    except Exception: pass
    return recent


def record_alerts(tickers):
    now = time.time()
    with open(script_path(DEDUP_FILE), "a") as f:
        for t in tickers:
            f.write(f"{now},{t}\n")


def update_alerted_history(hits):
    """
    Maintain a 30-day rolling record of every alerted breakout.
    Used by streak_tracker.py to identify tickers to monitor.
    """
    from datetime import timedelta, datetime as dt
    path = script_path(ALERTED_HISTORY)
    history = []
    if os.path.exists(path):
        try:
            with open(path) as f:
                history = json.load(f)
        except Exception:
            history = []

    # Remove entries older than retention window
    cutoff = dt.now() - timedelta(days=HISTORY_RETENTION_DAYS)
    history = [h for h in history if _parse_iso(h.get("first_alerted")) and
               _parse_iso(h["first_alerted"]) >= cutoff]
    existing_tickers = {h["ticker"]: h for h in history}

    # Only record NEW tickers (not re-alerts)
    for hit in hits:
        t = hit["ticker"]
        if t in existing_tickers:
            continue
        existing_tickers[t] = {
            "ticker": t,
            "first_alerted": dt.now().isoformat(timespec="seconds"),
            "breakout_price": hit["price"],
            "breakout_box_top": hit["box_top"],
            "original_grade": hit["grade"],
            "original_tier": hit["tier"],
            "original_score": hit["score"],
            "original_fund_score": hit.get("fund_score"),
            "original_fund_tag": hit.get("fund_tag"),
        }

    with open(path, "w") as f:
        json.dump(list(existing_tickers.values()), f, indent=2)


def _parse_iso(s):
    try:
        from datetime import datetime as dt
        return dt.fromisoformat(s)
    except Exception:
        return None


# ============================================================
# Telegram
# ============================================================
def send_telegram(message):
    if not SEND_TELEGRAM:
        print("  (Telegram env vars missing -- skipping push)"); return
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message,
                  "parse_mode": "Markdown", "disable_web_page_preview": True},
            timeout=10)
        if r.status_code != 200:
            print(f"  Telegram send failed ({r.status_code}): {r.text[:200]}")
    except Exception as e:
        print(f"  Telegram send error: {e}")


def format_telegram_message(h):
    sb = h["score_breakdown"]
    tag_line = ""; fund_section = ""
    if h.get("fund_tag"):
        tag_line = f"{h['fund_emoji']} *{h['fund_tag']}*  ({h['fund_score']}/100 fundamentals)\n"
    if h.get("fund_breakdown"):
        fb = h["fund_breakdown"]
        fund_section = (f"\n*Fundamentals (Lynch + O'Neil):*\n"
            f"\u2022 Earnings quality:  `{fb['earnings']}/20`\n"
            f"\u2022 Value (PEG/PE):    `{fb['value']}/20`\n"
            f"\u2022 Market leader:     `{fb['leader']}/20`\n"
            f"\u2022 Balance sheet:     `{fb['balance']}/20`\n"
            f"\u2022 Smart money:       `{fb['smart_money']}/20`\n")
    return (f"{h['emoji']} *{h['tier']}* \u2022 *{h['ticker']}*  `${h['price']}`  "
        f"*[{h['grade']} \u2022 {h['score']}/100]*\n{tag_line}\n"
        f"*Technical:*\n"
        f"\u2022 Volume: `{sb['volume']}/20`  ({h['vol_ratio']}\u00D7 avg)\n"
        f"\u2022 Clarity: `{sb['clarity']}/20`  (+{h['pct_above_box']}% above box)\n"
        f"\u2022 Box quality: `{sb['box_quality']}/20`\n"
        f"\u2022 Risk/reward: `{sb['risk_reward']}/20`  ({h['rr_ratio']}:1)\n"
        f"\u2022 Trend: `{sb['trend']}/20`\n{fund_section}"
        f"\n*Trade plan:*\n"
        f"\u2022 Entry:  `${h['price']}`\n"
        f"\u2022 Stop:   `${h['suggested_stop']}`  \u2192 risk `{h['risk_pct']}%`\n"
        f"\u2022 Target: `${h['target_30pct']}`  \u2192 reward `+30%`\n\n"
        f"[Chart](https://finance.yahoo.com/quote/{h['ticker']})  "
        f"\u00B7  [Finviz](https://finviz.com/quote.ashx?t={h['ticker']})  "
        f"\u00B7  [TV](https://www.tradingview.com/symbols/{h['ticker']}/)")


def format_summary_message(hits):
    lines = [f"\U0001F4CA *SCAN SUMMARY* \u2014 {len(hits)} breakout(s)\n"]
    for h in hits:
        tag_str = f"  {h['fund_emoji']} {h['fund_tag']}" if h.get("fund_tag") else ""
        lines.append(f"{h['emoji']} `{h['grade']:>2}` \u2022 *{h['ticker']:<5}* "
            f"`${h['price']:>7}` \u2022 {h['score']}{tag_str}")
    elite = [h for h in hits if h.get("fund_tag") == "ELITE"]
    clean = [h for h in hits if h["tier"] == "CLEAN"]
    lines.append("")
    if elite:
        lines.append(f"\U0001F31F *ELITE signals:* " + ", ".join(f"*{h['ticker']}*" for h in elite))
    elif clean:
        lines.append(f"\U0001F4A1 *Primary candidates:* " + ", ".join(f"*{h['ticker']}*" for h in clean))
    else:
        lines.append("\u26A0 No CLEAN setups this run. Be selective.")
    return "\n".join(lines)


# ============================================================
# Dashboard output
# ============================================================
def write_dashboard_data(hits):
    now_iso = datetime.now().isoformat(timespec="seconds")
    payload = {"timestamp": now_iso, "count": len(hits), "hits": hits,
        "settings": {"box_days": DARVAS_BOX_DAYS, "vol_multiplier": VOLUME_MULTIPLIER,
            "min_price": MIN_PRICE, "max_price": MAX_PRICE,
            "sources": ["Finviz", "Yahoo"], "fundamentals_enabled": FUNDAMENTALS_ENABLED}}
    with open(script_path(LATEST_JSON), "w") as f:
        json.dump(payload, f, indent=2)

    history = []
    hist_path = script_path(HISTORY_JSON)
    if os.path.exists(hist_path):
        try:
            with open(hist_path) as f: history = json.load(f)
        except Exception: history = []
    history.insert(0, {"timestamp": now_iso, "count": len(hits),
        "tickers": [h["ticker"] for h in hits],
        "clean_count": sum(1 for h in hits if h["tier"] == "CLEAN"),
        "elite_count": sum(1 for h in hits if h.get("fund_tag") == "ELITE")})
    history = history[:50]
    with open(hist_path, "w") as f:
        json.dump(history, f, indent=2)


def print_results(hits):
    if not hits:
        print("\nNo clean breakout setups found. That's normal -- patience."); return
    print("\n" + "=" * 80)
    print(f"TTM BREAKOUT CANDIDATES  --  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 80)
    for h in hits:
        tag = f"  [{h['fund_tag']} {h['fund_score']}/100]" if h.get("fund_tag") else ""
        print(f"\n  [{h['grade']:>2}] {h['tier']:<8} {h['ticker']:<5}  "
              f"${h['price']}  tech {h['score']}/100{tag}")


def main():
    print(f"\n>>> TTM Scanner (ELITE {'ON' if FUNDAMENTALS_ENABLED else 'OFF'})\n")
    tickers = get_all_candidates()
    hits = []
    if tickers:
        print("Verifying Darvas breakouts...\n")
        for i, t in enumerate(tickers, 1):
            print(f"  [{i}/{len(tickers)}] {t}...", end=" ")
            r = check_darvas_breakout(t)
            if r:
                print(f"BREAKOUT [{r['grade']}] {r['tier']} {r['score']}/100")
                hits.append(r)
            else:
                print("no")
            time.sleep(0.8)

    hits = sorted(hits, key=lambda x: (x["score"], x["vol_ratio"]), reverse=True)[:TOP_N_TO_SHOW]

    if FUNDAMENTALS_ENABLED:
        clean = [h for h in hits if h["tier"] == "CLEAN"]
        if clean:
            print(f"\nFetching fundamentals for {len(clean)} A-grade setup(s)...\n")
            for h in clean:
                print(f"  {h['ticker']} fundamentals...", end=" ")
                f = fetch_fundamentals(h["ticker"])
                fs = score_fundamentals(h["ticker"], f)
                if fs:
                    h.update(fs)
                    print(f"{fs['fund_score']}/100 [{fs.get('fund_tag') or 'none'}]")
                else:
                    print("data unavailable")
                time.sleep(0.3)

    print_results(hits)
    write_dashboard_data(hits)
    update_alerted_history(hits)   # NEW: record breakouts for streak tracker

    if hits and SEND_TELEGRAM:
        recent = load_recent_alerts()
        new_hits = [h for h in hits if h["ticker"] not in recent]
        if new_hits:
            print(f"Sending {len(new_hits)} alert(s)...")
            for h in new_hits:
                send_telegram(format_telegram_message(h))
                time.sleep(0.6)
            send_telegram(format_summary_message(new_hits))
            record_alerts([h["ticker"] for h in new_hits])

    if hits:
        df = pd.DataFrame([{k: v for k, v in h.items()
            if k not in ("spark", "score_breakdown", "fund_breakdown")} for h in hits])
        fn = script_path(f"breakouts_{datetime.now().strftime('%Y%m%d_%H%M')}.csv")
        df.to_csv(fn, index=False)
        print(f"CSV saved: {os.path.basename(fn)}\n")


if __name__ == "__main__":
    main()
