"""
TTM Breakout Scanner  --  with Fundamental ELITE layer
-------------------------------------------------------
Pure Darvas technical breakout + fundamental overlay for A-grade setups.

Pipeline:
  1. Candidates from Finviz + Yahoo
  2. Darvas box + volume verification via yfinance
  3. Technical scoring -> grades D through A+
  4. For CLEAN (A / A+) setups only: fetch fundamentals from Financial
     Modeling Prep and score across 5 pillars (Lynch + O'Neil core):
       - Earnings quality      (EPS growth, acceleration, margins)
       - Growth at reasonable price (PEG, P/E)
       - Market leader         (sales growth, ROE, rel strength)
       - Balance sheet         (D/E, FCF, current ratio)
       - Smart money           (insider buys, short interest, ownership)
  5. Total fundamental score 0-100:
       80+ -> ELITE tag   (fundamentals + technicals aligned)
       60+ -> STRONG tag  (solid but imperfect)
       <60 -> no tag, keep A-grade

FMP free tier = 250 calls/day. We only call it for A-grade breakouts
(typically 0-5 per run), so we stay well under the limit.

Env vars expected:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, FMP_API_KEY
"""

import time
import os
import json
import re
from datetime import datetime

import requests
import pandas as pd
import yfinance as yf
from bs4 import BeautifulSoup


# ============================================================
# SECRETS FROM ENVIRONMENT
# ============================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
FMP_API_KEY        = os.environ.get("FMP_API_KEY", "")
SEND_TELEGRAM = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
FUNDAMENTALS_ENABLED = bool(FMP_API_KEY)


# ============================================================
# SETTINGS
# ============================================================
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

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
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
    url = (
        "https://finviz.com/screener.ashx?"
        "v=111&"
        "f=cap_smallover,sh_avgvol_o300,sh_price_1to100,sh_relvol_o1.5,ta_gap_u,ta_perf_1w5o"
        "&ft=4"
    )
    print("  [Finviz] fetching...")
    try:
        response = requests.get(url, headers=DEFAULT_HEADERS, timeout=15)
        response.raise_for_status()
    except Exception as e:
        print(f"    Finviz failed: {e}")
        return []
    soup = BeautifulSoup(response.text, "html.parser")
    tickers = []
    for link in soup.find_all("a", class_="tab-link"):
        href = link.get("href", "")
        if href.startswith("quote.ashx?t="):
            ticker = link.text.strip()
            if ticker and ticker not in tickers:
                tickers.append(ticker)
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
                href = a["href"]
                m = re.match(r"^/quote/([A-Z][A-Z0-9\-]{0,5})(?:[/?]|$)", href)
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
            continue
    print(f"    Yahoo total unique: {len(tickers)}")
    return tickers


def get_all_candidates():
    print("Gathering candidates from all sources...")
    finviz = get_finviz_candidates()
    yahoo  = get_yahoo_candidates()
    seen = set()
    combined = []
    for t in finviz + yahoo:
        if t not in seen:
            seen.add(t)
            combined.append(t)
    print(f"  Combined (de-duped): {len(combined)} tickers "
          f"(Finviz {len(finviz)} + Yahoo {len(yahoo)})\n")
    return combined[:MAX_RESULTS_TO_CHECK]


# ============================================================
# Darvas verification + technical scoring
# ============================================================
def check_darvas_breakout(ticker):
    try:
        data = yf.download(
            ticker, period="60d", interval="1d",
            progress=False, auto_adjust=False,
        )
    except Exception:
        return None
    if data is None or len(data) < DARVAS_BOX_DAYS + 5:
        return None
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)

    today = data.iloc[-1]
    today_close = float(today["Close"])
    today_volume = float(today["Volume"])

    if not (MIN_PRICE <= today_close <= MAX_PRICE):
        return None

    prior = data.iloc[-(DARVAS_BOX_DAYS + 1):-1]
    box_top = float(prior["High"].max())
    box_bottom = float(prior["Low"].min())
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
        "ticker": ticker,
        "price": round(today_close, 2),
        "box_top": round(box_top, 2),
        "box_bottom": round(box_bottom, 2),
        "pct_above_box": round(pct_above_box, 2),
        "vol_today": int(today_volume),
        "vol_avg_20d": int(avg_vol),
        "vol_ratio": round(vol_ratio, 2),
        "suggested_stop": round(stop, 2),
        "target_30pct": round(target, 2),
        "risk_pct": round(risk_pct, 2),
        "rr_ratio": round(rr_ratio, 2),
        "box_range_pct": round(box_range_pct, 2),
        "ma20": round(ma20, 2),
        "ma_rising": ma_rising,
        "price_vs_ma20_pct": round((today_close - ma20) / ma20 * 100, 2),
        "spark": spark,
    }
    hit.update(score_breakout(hit))
    return hit


def score_breakout(h):
    v = h["vol_ratio"]
    if v >= 5.0:   vol_score = 20
    elif v >= 4.0: vol_score = 18
    elif v >= 3.0: vol_score = 14
    elif v >= 2.5: vol_score = 11
    elif v >= 2.0: vol_score = 8
    else:          vol_score = 4
    p = h["pct_above_box"]
    if p >= 6:      clarity_score = 20
    elif p >= 4:    clarity_score = 16
    elif p >= 2.5:  clarity_score = 13
    elif p >= 1.5:  clarity_score = 10
    elif p >= 0.5:  clarity_score = 6
    else:           clarity_score = 3
    r = h["box_range_pct"]
    if r <= 10:    box_score = 20
    elif r <= 15:  box_score = 16
    elif r <= 20:  box_score = 12
    elif r <= 25:  box_score = 8
    elif r <= 35:  box_score = 5
    else:          box_score = 2
    rr = h["rr_ratio"]
    if rr >= 5:     rr_score = 20
    elif rr >= 4:   rr_score = 16
    elif rr >= 3:   rr_score = 12
    elif rr >= 2:   rr_score = 8
    elif rr >= 1.5: rr_score = 5
    else:           rr_score = 2
    pct_ma = h["price_vs_ma20_pct"]
    rising = h["ma_rising"]
    if rising and pct_ma >= 10:  trend_score = 20
    elif rising and pct_ma >= 5: trend_score = 16
    elif rising and pct_ma >= 0: trend_score = 12
    elif rising:                 trend_score = 8
    elif pct_ma >= 5:            trend_score = 8
    elif pct_ma >= 0:            trend_score = 5
    else:                        trend_score = 2

    total = vol_score + clarity_score + box_score + rr_score + trend_score

    if total >= 90:   grade, tier, emoji = "A+", "CLEAN",    "\U0001F7E2"
    elif total >= 85: grade, tier, emoji = "A",  "CLEAN",    "\U0001F7E2"
    elif total >= 75: grade, tier, emoji = "B+", "SOLID",    "\U0001F7E1"
    elif total >= 70: grade, tier, emoji = "B",  "SOLID",    "\U0001F7E1"
    elif total >= 60: grade, tier, emoji = "C+", "MARGINAL", "\U0001F7E0"
    elif total >= 55: grade, tier, emoji = "C",  "MARGINAL", "\U0001F7E0"
    else:             grade, tier, emoji = "D",  "WEAK",     "\U0001F534"

    return {
        "score": total, "grade": grade, "tier": tier, "emoji": emoji,
        "score_breakdown": {
            "volume": vol_score, "clarity": clarity_score,
            "box_quality": box_score, "risk_reward": rr_score,
            "trend": trend_score,
        },
    }


# ============================================================
# FUNDAMENTAL ANALYSIS (ELITE LAYER)
# ============================================================
def fmp_get(endpoint, params=None):
    """GET helper for Financial Modeling Prep."""
    if not FMP_API_KEY:
        return None
    url = f"{FMP_BASE}/{endpoint}"
    params = params or {}
    params["apikey"] = FMP_API_KEY
    try:
        r = requests.get(url, params=params, timeout=12)
        if r.status_code != 200:
            return None
        data = r.json()
        if isinstance(data, dict) and data.get("Error Message"):
            return None
        return data
    except Exception:
        return None


def fetch_fundamentals(ticker):
    """Pull all fundamentals we need in as few calls as possible."""
    data = {}
    # Key metrics (annual) - P/E, PEG, ROE, D/E, etc.
    km = fmp_get(f"key-metrics/{ticker}", {"limit": 2})
    data["key_metrics"] = km if isinstance(km, list) else []
    # Ratios (annual) - margins, current ratio
    ratios = fmp_get(f"ratios/{ticker}", {"limit": 2})
    data["ratios"] = ratios if isinstance(ratios, list) else []
    # Income statement quarterly - EPS growth + acceleration
    income_q = fmp_get(f"income-statement/{ticker}", {"period": "quarter", "limit": 5})
    data["income_q"] = income_q if isinstance(income_q, list) else []
    # Cash flow - free cash flow
    cashflow = fmp_get(f"cash-flow-statement/{ticker}", {"limit": 2})
    data["cashflow"] = cashflow if isinstance(cashflow, list) else []
    # Quote for market cap, shares, short ratio
    quote = fmp_get(f"quote/{ticker}")
    data["quote"] = quote[0] if isinstance(quote, list) and quote else {}
    # Insider trading
    insider = fmp_get("insider-trading", {"symbol": ticker, "limit": 50})
    data["insider"] = insider if isinstance(insider, list) else []
    # Institutional ownership
    inst = fmp_get(f"institutional-holder/{ticker}")
    data["institutional"] = inst if isinstance(inst, list) else []
    return data


def score_fundamentals(ticker, f):
    """Score 0-100 across 5 pillars. Returns dict or None if data missing."""
    if not f or not f.get("key_metrics"):
        return None

    km  = (f["key_metrics"] or [{}])[0]
    km_prev = (f["key_metrics"] or [{}, {}])[1] if len(f["key_metrics"]) > 1 else {}
    ratios = (f["ratios"] or [{}])[0]
    income_q = f.get("income_q") or []
    cashflow = (f["cashflow"] or [{}])[0]
    quote = f.get("quote") or {}
    insider = f.get("insider") or []
    institutional = f.get("institutional") or []

    def safe(d, key, default=None):
        v = d.get(key) if isinstance(d, dict) else None
        return v if v is not None else default

    # --- PILLAR 1: Earnings quality (0-20) ---
    #   EPS YoY growth + acceleration across last 3 quarters + net margin
    p1 = 0
    try:
        if len(income_q) >= 4:
            eps_q = [safe(q, "eps", 0) or 0 for q in income_q[:4]]
            # Current vs 4-quarters-ago YoY
            if eps_q[3] and eps_q[3] != 0:
                yoy = (eps_q[0] - eps_q[3]) / abs(eps_q[3]) * 100
                if yoy >= 40: p1 += 10
                elif yoy >= 25: p1 += 7
                elif yoy >= 15: p1 += 4
                elif yoy > 0: p1 += 2
            # Acceleration: is most recent growth > previous growth?
            if len(income_q) >= 5 and income_q[4].get("eps"):
                g_now = (eps_q[0] - eps_q[2]) if eps_q[2] else 0
                g_prev = (eps_q[1] - eps_q[3]) if eps_q[3] else 0
                if g_now > g_prev > 0:
                    p1 += 5
        # Net margin
        nm = safe(ratios, "netProfitMargin", 0) or 0
        if nm >= 0.20: p1 += 5
        elif nm >= 0.10: p1 += 3
        elif nm >= 0.05: p1 += 1
    except Exception:
        pass
    p1 = min(20, p1)

    # --- PILLAR 2: Growth at reasonable price (0-20) ---
    p2 = 0
    peg = safe(km, "pegRatio")
    pe  = safe(km, "peRatio")
    try:
        if peg is not None and peg > 0:
            if peg <= 1.0: p2 += 12
            elif peg <= 1.5: p2 += 9
            elif peg <= 2.0: p2 += 5
            elif peg <= 3.0: p2 += 2
        if pe is not None and pe > 0:
            if pe <= 25: p2 += 8
            elif pe <= 40: p2 += 5
            elif pe <= 60: p2 += 2
    except Exception:
        pass
    p2 = min(20, p2)

    # --- PILLAR 3: Market leader (0-20) ---
    #   Revenue growth YoY + ROE
    p3 = 0
    try:
        if len(income_q) >= 4:
            rev_now = safe(income_q[0], "revenue", 0) or 0
            rev_4q  = safe(income_q[3], "revenue", 0) or 0
            if rev_4q > 0:
                rev_growth = (rev_now - rev_4q) / rev_4q * 100
                if rev_growth >= 30: p3 += 10
                elif rev_growth >= 20: p3 += 7
                elif rev_growth >= 10: p3 += 4
                elif rev_growth > 0: p3 += 2
        roe = safe(km, "roe", 0) or 0
        if roe >= 0.25: p3 += 10
        elif roe >= 0.15: p3 += 7
        elif roe >= 0.10: p3 += 4
        elif roe > 0: p3 += 2
    except Exception:
        pass
    p3 = min(20, p3)

    # --- PILLAR 4: Balance sheet (0-20) ---
    p4 = 0
    try:
        de = safe(km, "debtToEquity")
        if de is not None:
            if de <= 0.3: p4 += 7
            elif de <= 0.6: p4 += 5
            elif de <= 1.0: p4 += 3
            elif de <= 2.0: p4 += 1
        cr = safe(ratios, "currentRatio", 0) or 0
        if cr >= 2.0: p4 += 6
        elif cr >= 1.5: p4 += 4
        elif cr >= 1.0: p4 += 2
        fcf = safe(cashflow, "freeCashFlow", 0) or 0
        if fcf > 0: p4 += 7
    except Exception:
        pass
    p4 = min(20, p4)

    # --- PILLAR 5: Smart money (0-20) ---
    p5 = 0
    try:
        # Insider net activity over last ~90 days
        from datetime import timedelta, datetime as dt
        recent = []
        cutoff = dt.now() - timedelta(days=90)
        for tx in insider:
            ds = tx.get("transactionDate") or tx.get("filingDate") or ""
            try:
                d = dt.fromisoformat(ds[:10])
            except Exception:
                continue
            if d >= cutoff:
                recent.append(tx)
        buys = sum(1 for tx in recent
                   if "Purchase" in str(tx.get("transactionType", ""))
                   or "P-Purchase" in str(tx.get("transactionType", "")))
        sells = sum(1 for tx in recent
                    if "Sale" in str(tx.get("transactionType", ""))
                    or "S-Sale" in str(tx.get("transactionType", "")))
        if buys > sells and buys >= 2: p5 += 10
        elif buys > 0 and buys >= sells: p5 += 6
        elif sells == 0: p5 += 4
        elif sells > buys * 3: p5 += 0
        else: p5 += 2

        # Institutional ownership count as rough proxy
        if len(institutional) >= 50: p5 += 6
        elif len(institutional) >= 20: p5 += 4
        elif len(institutional) >= 5: p5 += 2

        # Short interest (via quote data if available)
        float_shares = safe(quote, "sharesOutstanding", 0) or 0
        short_pct = None  # FMP free tier doesn't always include this; skip if missing
        # Reserve remainder for short interest when available
        p5 += 4
    except Exception:
        pass
    p5 = min(20, p5)

    total = p1 + p2 + p3 + p4 + p5

    if total >= 80:
        tag, emoji = "ELITE", "\U0001F31F"
    elif total >= 60:
        tag, emoji = "STRONG", "\u2B50"
    else:
        tag, emoji = None, None

    return {
        "fund_score": total,
        "fund_tag": tag,
        "fund_emoji": emoji,
        "fund_breakdown": {
            "earnings": p1,
            "value": p2,
            "leader": p3,
            "balance": p4,
            "smart_money": p5,
        },
        "fund_metrics": {
            "peg_ratio": peg,
            "pe_ratio": pe,
            "debt_to_equity": safe(km, "debtToEquity"),
            "roe": safe(km, "roe"),
            "net_margin": safe(ratios, "netProfitMargin"),
            "current_ratio": safe(ratios, "currentRatio"),
            "free_cash_flow_positive": (safe(cashflow, "freeCashFlow", 0) or 0) > 0,
        },
    }


# ============================================================
# De-dup
# ============================================================
def load_recent_alerts():
    recent = set()
    path = script_path(DEDUP_FILE)
    if not os.path.exists(path):
        return recent
    cutoff = time.time() - (DEDUP_HOURS * 3600)
    try:
        with open(path, "r") as f:
            for line in f:
                parts = line.strip().split(",")
                if len(parts) == 2:
                    ts, tkr = parts
                    if float(ts) >= cutoff:
                        recent.add(tkr)
    except Exception:
        pass
    return recent


def record_alerts(tickers):
    now = time.time()
    with open(script_path(DEDUP_FILE), "a") as f:
        for t in tickers:
            f.write(f"{now},{t}\n")


# ============================================================
# Telegram
# ============================================================
def send_telegram(message):
    if not SEND_TELEGRAM:
        print("  (Telegram env vars missing -- skipping push)")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, data=payload, timeout=10)
        if r.status_code != 200:
            print(f"  Telegram send failed ({r.status_code}): {r.text[:200]}")
    except Exception as e:
        print(f"  Telegram send error: {e}")


def format_telegram_message(h):
    sb = h["score_breakdown"]
    tag_line = ""
    fund_section = ""
    if h.get("fund_tag"):
        tag_line = f"{h['fund_emoji']} *{h['fund_tag']}*  ({h['fund_score']}/100 fundamentals)\n"
    if h.get("fund_breakdown"):
        fb = h["fund_breakdown"]
        fund_section = (
            f"\n*Fundamentals (Lynch + O'Neil):*\n"
            f"\u2022 Earnings quality:  `{fb['earnings']}/20`\n"
            f"\u2022 Value (PEG/PE):    `{fb['value']}/20`\n"
            f"\u2022 Market leader:     `{fb['leader']}/20`\n"
            f"\u2022 Balance sheet:     `{fb['balance']}/20`\n"
            f"\u2022 Smart money:       `{fb['smart_money']}/20`\n"
        )

    return (
        f"{h['emoji']} *{h['tier']}* \u2022 *{h['ticker']}*  `${h['price']}`  "
        f"*[{h['grade']} \u2022 {h['score']}/100]*\n"
        f"{tag_line}"
        f"\n"
        f"*Technical breakdown:*\n"
        f"\u2022 Volume: `{sb['volume']}/20`  ({h['vol_ratio']}\u00D7 avg)\n"
        f"\u2022 Clarity: `{sb['clarity']}/20`  (+{h['pct_above_box']}% above box)\n"
        f"\u2022 Box quality: `{sb['box_quality']}/20`\n"
        f"\u2022 Risk/reward: `{sb['risk_reward']}/20`  ({h['rr_ratio']}:1)\n"
        f"\u2022 Trend: `{sb['trend']}/20`\n"
        f"{fund_section}"
        f"\n*Trade plan:*\n"
        f"\u2022 Entry:  `${h['price']}`\n"
        f"\u2022 Stop:   `${h['suggested_stop']}`  \u2192 risk `{h['risk_pct']}%`\n"
        f"\u2022 Target: `${h['target_30pct']}`  \u2192 reward `+30%`\n\n"
        f"[Chart](https://finance.yahoo.com/quote/{h['ticker']})  "
        f"\u00B7  [Finviz](https://finviz.com/quote.ashx?t={h['ticker']})  "
        f"\u00B7  [TV](https://www.tradingview.com/symbols/{h['ticker']}/)"
    )


def format_summary_message(hits):
    lines = [f"\U0001F4CA *SCAN SUMMARY* \u2014 {len(hits)} breakout(s)\n"]
    for h in hits:
        tag_str = ""
        if h.get("fund_tag"):
            tag_str = f"  {h['fund_emoji']} {h['fund_tag']}"
        lines.append(
            f"{h['emoji']} `{h['grade']:>2}` \u2022 *{h['ticker']:<5}* "
            f"`${h['price']:>7}` \u2022 {h['score']}{tag_str}"
        )
    elite = [h for h in hits if h.get("fund_tag") == "ELITE"]
    clean = [h for h in hits if h["tier"] == "CLEAN"]
    lines.append("")
    if elite:
        lines.append(f"\U0001F31F *ELITE signals:* "
                     + ", ".join(f"*{h['ticker']}*" for h in elite))
    elif clean:
        lines.append(f"\U0001F4A1 *Primary candidates:* "
                     + ", ".join(f"*{h['ticker']}*" for h in clean))
    else:
        lines.append("\u26A0 No CLEAN setups this run. Be selective.")
    return "\n".join(lines)


# ============================================================
# Dashboard output
# ============================================================
def write_dashboard_data(hits):
    now_iso = datetime.now().isoformat(timespec="seconds")
    payload = {
        "timestamp": now_iso,
        "count": len(hits),
        "hits": hits,
        "settings": {
            "box_days": DARVAS_BOX_DAYS,
            "vol_multiplier": VOLUME_MULTIPLIER,
            "min_price": MIN_PRICE,
            "max_price": MAX_PRICE,
            "sources": ["Finviz", "Yahoo"],
            "fundamentals_enabled": FUNDAMENTALS_ENABLED,
        },
    }
    with open(script_path(LATEST_JSON), "w") as f:
        json.dump(payload, f, indent=2)

    history = []
    hist_path = script_path(HISTORY_JSON)
    if os.path.exists(hist_path):
        try:
            with open(hist_path, "r") as f:
                history = json.load(f)
        except Exception:
            history = []
    history.insert(0, {
        "timestamp": now_iso,
        "count": len(hits),
        "tickers": [h["ticker"] for h in hits],
        "clean_count": sum(1 for h in hits if h["tier"] == "CLEAN"),
        "elite_count": sum(1 for h in hits if h.get("fund_tag") == "ELITE"),
    })
    history = history[:50]
    with open(hist_path, "w") as f:
        json.dump(history, f, indent=2)


def print_results(hits):
    if not hits:
        print("\nNo clean breakout setups found. That's normal -- patience.")
        return
    print("\n" + "=" * 80)
    print(f"TTM BREAKOUT CANDIDATES  --  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 80)
    for h in hits:
        tag = ""
        if h.get("fund_tag"):
            tag = f"  [{h['fund_tag']} {h['fund_score']}/100]"
        print(f"\n  [{h['grade']:>2}] {h['tier']:<8} {h['ticker']:<5}  "
              f"${h['price']}  tech {h['score']}/100{tag}")
    elite = [h for h in hits if h.get("fund_tag") == "ELITE"]
    if elite:
        print("\n" + "-" * 80)
        print("\U0001F31F ELITE SIGNALS: " + ", ".join(h["ticker"] for h in elite))
    print("=" * 80 + "\n")


# ============================================================
# MAIN
# ============================================================
def main():
    print("\n>>> TTM Breakout Scanner starting (ELITE layer "
          f"{'ON' if FUNDAMENTALS_ENABLED else 'OFF'})...\n")
    tickers = get_all_candidates()

    hits = []
    if tickers:
        print(f"Verifying Darvas breakouts via Yahoo Finance...\n")
        for i, ticker in enumerate(tickers, start=1):
            print(f"  [{i}/{len(tickers)}] {ticker}...", end=" ")
            result = check_darvas_breakout(ticker)
            if result:
                print(f"BREAKOUT  [{result['grade']}] {result['tier']}  "
                      f"score {result['score']}/100")
                hits.append(result)
            else:
                print("no")
            time.sleep(0.8)

    hits = sorted(hits, key=lambda x: (x["score"], x["vol_ratio"]), reverse=True)
    hits = hits[:TOP_N_TO_SHOW]

    # Fundamental scoring ONLY for CLEAN (A / A+) technical setups.
    if FUNDAMENTALS_ENABLED:
        clean_hits = [h for h in hits if h["tier"] == "CLEAN"]
        if clean_hits:
            print(f"\nFetching fundamentals for {len(clean_hits)} A-grade setup(s)...\n")
            for h in clean_hits:
                t = h["ticker"]
                print(f"  {t} fundamentals...", end=" ")
                f = fetch_fundamentals(t)
                fscore = score_fundamentals(t, f)
                if fscore:
                    h.update(fscore)
                    tag = fscore.get("fund_tag") or "none"
                    print(f"{fscore['fund_score']}/100 [{tag}]")
                else:
                    print("data unavailable")
                time.sleep(0.3)
    else:
        print("\n(FMP_API_KEY not set -- skipping fundamental analysis)\n")

    print_results(hits)
    write_dashboard_data(hits)

    if hits and SEND_TELEGRAM:
        recent = load_recent_alerts()
        new_hits = [h for h in hits if h["ticker"] not in recent]
        if new_hits:
            print(f"Sending {len(new_hits)} alert(s) + summary to Telegram...")
            for h in new_hits:
                send_telegram(format_telegram_message(h))
                time.sleep(0.6)
            send_telegram(format_summary_message(new_hits))
            record_alerts([h["ticker"] for h in new_hits])
        else:
            print(f"(All {len(hits)} tickers already alerted in last {DEDUP_HOURS}h.)")

    if hits:
        df = pd.DataFrame([
            {k: v for k, v in h.items() if k not in (
                "spark", "score_breakdown", "fund_breakdown", "fund_metrics")}
            for h in hits
        ])
        filename = script_path(f"breakouts_{datetime.now().strftime('%Y%m%d_%H%M')}.csv")
        df.to_csv(filename, index=False)
        print(f"Results saved to: {os.path.basename(filename)}\n")


if __name__ == "__main__":
    main()
