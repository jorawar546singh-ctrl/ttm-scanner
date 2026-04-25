"""
TTM Streak Tracker  --  monitors continuation on recently-alerted tickers
--------------------------------------------------------------------------
For every ticker in alerted_history.json (last 30 days), checks whether it
is still trending by the stacked Darvas principles:

  1. Today's close is a new POST-BREAKOUT high
  2. Today's volume >= 1.5x 20-day average (confirms with volume)
  3. Today's close broke above a new recent 5-day box top
     (real structure break, not drift)
  4. Streak has hit a milestone: 3, 5, 7, 10, 15, 20 days

If all four conditions pass, send a "TRENDING" alert to Telegram.
If streak breaks after 5+ days (first close below recent high), send
a "TREND FADING" alert.

Auto-removes tickers from tracking when:
  - 30+ days since breakout (time decay)
  - 5+ consecutive red days (trend clearly dead)
"""
import os, json, time
from datetime import datetime, timedelta

import requests
import yfinance as yf
import pandas as pd


TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
SEND_TELEGRAM = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ALERTED_HISTORY = "alerted_history.json"
STREAKS_STATE   = "streaks_state.json"

VOLUME_MULT_FOR_CONTINUATION = 1.5
RECENT_BOX_DAYS = 5
MILESTONE_DAYS  = [3, 5, 7, 10, 15, 20]
MAX_RED_DAYS_BEFORE_DROP = 5
MAX_TRACK_DAYS = 30


def script_path(name):
    return os.path.join(SCRIPT_DIR, name)


def send_telegram(message):
    if not SEND_TELEGRAM:
        print("  (Telegram env vars missing -- skipping push)")
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message,
                  "parse_mode": "Markdown", "disable_web_page_preview": True},
            timeout=10)
        if r.status_code != 200:
            print(f"  Telegram send failed ({r.status_code}): {r.text[:200]}")
    except Exception as e:
        print(f"  Telegram send error: {e}")


def load_json(path, default):
    p = script_path(path)
    if not os.path.exists(p):
        return default
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(script_path(path), "w") as f:
        json.dump(data, f, indent=2)


def fetch_history(ticker, days=45):
    """Fetch enough daily OHLCV to compute streaks."""
    try:
        data = yf.download(ticker, period=f"{days}d", interval="1d",
                           progress=False, auto_adjust=False)
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)
        return data if data is not None and not data.empty else None
    except Exception:
        return None


def analyze_streak(ticker, breakout_entry, data):
    """
    Returns a dict with streak analysis.
    breakout_entry: the record from alerted_history.json
    data: pandas DataFrame of OHLCV
    """
    breakout_price = breakout_entry["breakout_price"]
    breakout_date_str = breakout_entry["first_alerted"][:10]

    try:
        breakout_date = datetime.fromisoformat(breakout_date_str)
    except Exception:
        breakout_date = datetime.now() - timedelta(days=1)

    # Post-breakout slice (trading days AFTER original breakout date)
    data_post = data[data.index.date > breakout_date.date()]
    if data_post.empty:
        return None

    today_row = data_post.iloc[-1]
    today_close = float(today_row["Close"])
    today_volume = float(today_row["Volume"])
    today_high = float(today_row["High"])

    # Post-breakout running high (excluding today)
    if len(data_post) >= 2:
        prior_post = data_post.iloc[:-1]
        post_breakout_high = float(prior_post["Close"].max())
    else:
        post_breakout_high = breakout_price

    is_new_post_breakout_high = today_close > post_breakout_high

    # Volume confirmation: today vs 20-day average (using full data, not just post)
    vol_lookback = min(20, len(data) - 1)
    avg_vol = float(data["Volume"].iloc[-(vol_lookback + 1):-1].mean()) if vol_lookback > 0 else 0
    vol_ratio = today_volume / avg_vol if avg_vol > 0 else 0
    volume_ok = vol_ratio >= VOLUME_MULT_FOR_CONTINUATION

    # New 5-day box top break: today > max of last 5 daily highs (excluding today)
    if len(data) >= RECENT_BOX_DAYS + 1:
        recent_box = data.iloc[-(RECENT_BOX_DAYS + 1):-1]
        recent_box_top = float(recent_box["High"].max())
        new_box_break = today_close > recent_box_top
    else:
        recent_box_top = None
        new_box_break = False

    # Streak counting: how many consecutive days of "higher close" since breakout
    streak = 0
    closes = data_post["Close"].tolist()
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] > closes[i - 1]:
            streak += 1
        else:
            break
    # Today's close counts if it's strictly up
    if streak == 0 and len(closes) >= 1 and closes[-1] > breakout_price:
        streak = 1

    # Days since breakout (trading days)
    days_since_breakout = len(data_post)

    # Consecutive red-day streak (for auto-drop)
    red_streak = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] < closes[i - 1]:
            red_streak += 1
        else:
            break

    pct_from_breakout = (today_close - breakout_price) / breakout_price * 100

    return {
        "today_close": today_close,
        "today_volume": int(today_volume),
        "avg_vol_20d": int(avg_vol),
        "vol_ratio": round(vol_ratio, 2),
        "volume_ok": volume_ok,
        "recent_box_top": round(recent_box_top, 2) if recent_box_top else None,
        "new_box_break": new_box_break,
        "post_breakout_high": round(post_breakout_high, 2),
        "is_new_post_breakout_high": is_new_post_breakout_high,
        "streak": streak,
        "days_since_breakout": days_since_breakout,
        "red_streak": red_streak,
        "pct_from_breakout": round(pct_from_breakout, 2),
    }


def format_trending_alert(ticker, be, st, milestone):
    """Build a trending continuation alert."""
    warning = ""
    if milestone >= 10:
        warning = ("\n\u26A0 *Late-streak warning:* Stock is extended from entry. "
                   "Risk/reward on late entry is poor. If not already in, skip. "
                   "If holding, let trailing stop do the work.")

    return (
        f"\U0001F525 *TRENDING: {ticker}* \u2022 Day {milestone} streak\n\n"
        f"\u2022 Originally alerted *{be.get('original_grade', '?')}* "
        f"({be.get('original_tier', '?')}) on {be['first_alerted'][:10]}\n"
        f"\u2022 Breakout price: `${be['breakout_price']:.2f}` \u2192 "
        f"now `${st['today_close']:.2f}` "
        f"(*{st['pct_from_breakout']:+.1f}%*)\n"
        f"\u2022 New post-breakout high \u2705\n"
        f"\u2022 Volume: *{st['vol_ratio']}\u00D7* avg \u2705\n"
        f"\u2022 Broke new 5-day box at `${st['recent_box_top']}` \u2705\n"
        f"\u2022 Streak: {st['streak']} consecutive up-days"
        f"{warning}\n\n"
        f"[Chart](https://finance.yahoo.com/quote/{ticker})  "
        f"\u00B7  [Finviz](https://finviz.com/quote.ashx?t={ticker})  "
        f"\u00B7  [TV](https://www.tradingview.com/symbols/{ticker}/)"
    )


def format_fading_alert(ticker, be, st, peak_streak):
    return (
        f"\u26A0 *TREND FADING: {ticker}*\n\n"
        f"\u2022 Peak streak: {peak_streak} days\n"
        f"\u2022 Post-breakout high: `${st['post_breakout_high']:.2f}`\n"
        f"\u2022 Today: `${st['today_close']:.2f}` "
        f"({st['pct_from_breakout']:+.1f}% from entry)\n"
        f"\u2022 First close below running high after multi-day run\n\n"
        f"Not a sell signal by itself.\n"
        f"If holding: check your trailing stop.\n"
        f"If not holding: don't chase, trend may be ending."
    )


def main():
    print("\n>>> TTM Streak Tracker starting...\n")

    alerted = load_json(ALERTED_HISTORY, [])
    if not alerted:
        print("No alerted tickers to track yet.\n")
        save_json(STREAKS_STATE, {"updated": datetime.now().isoformat(timespec="seconds"),
                                   "streaks": []})
        return

    prev_state = load_json(STREAKS_STATE, {"streaks": []})
    prev_by_ticker = {s["ticker"]: s for s in prev_state.get("streaks", [])}

    new_streaks = []
    alerts = []

    for entry in alerted:
        ticker = entry["ticker"]
        try:
            first_alerted = datetime.fromisoformat(entry["first_alerted"])
        except Exception:
            continue

        age_days = (datetime.now() - first_alerted).days
        if age_days > MAX_TRACK_DAYS:
            continue   # Time decay: stop tracking

        print(f"  {ticker}...", end=" ")
        data = fetch_history(ticker)
        if data is None:
            print("data unavailable")
            continue

        st = analyze_streak(ticker, entry, data)
        if not st:
            print("not enough post-breakout data")
            continue

        # Drop if 5+ consecutive red days
        if st["red_streak"] >= MAX_RED_DAYS_BEFORE_DROP:
            print(f"dropping (red streak {st['red_streak']})")
            continue

        prev = prev_by_ticker.get(ticker, {})
        last_milestone = prev.get("last_milestone_alerted", 0)
        peak_streak = max(prev.get("peak_streak", 0), st["streak"])
        prev_status = prev.get("status", "fresh")

        # Determine alert
        status = "trending"
        if st["streak"] >= MAX_RED_DAYS_BEFORE_DROP:
            status = "trending"
        elif st["red_streak"] >= 1 and peak_streak >= 5 and prev_status == "trending":
            status = "fading"
            alerts.append(format_fading_alert(ticker, entry, st, peak_streak))
        else:
            status = prev_status if st["streak"] <= 1 else "trending"

        # Milestone alert: all stacked conditions must pass
        fired_milestone = None
        if (st["is_new_post_breakout_high"] and st["volume_ok"]
                and st["new_box_break"]):
            for m in MILESTONE_DAYS:
                if st["streak"] >= m > last_milestone:
                    fired_milestone = m
                    alerts.append(format_trending_alert(ticker, entry, st, m))

        # Record
        new_streaks.append({
            "ticker": ticker,
            "breakout_price": entry["breakout_price"],
            "first_alerted": entry["first_alerted"],
            "original_grade": entry.get("original_grade"),
            "original_tier": entry.get("original_tier"),
            "days_since_breakout": st["days_since_breakout"],
            "today_close": st["today_close"],
            "pct_from_breakout": st["pct_from_breakout"],
            "post_breakout_high": st["post_breakout_high"],
            "current_streak": st["streak"],
            "peak_streak": peak_streak,
            "last_milestone_alerted": fired_milestone or last_milestone,
            "volume_ok_today": st["volume_ok"],
            "vol_ratio": st["vol_ratio"],
            "new_box_break_today": st["new_box_break"],
            "red_streak": st["red_streak"],
            "status": status,
        })
        pct = st["pct_from_breakout"]
        tag = "🔥" if status == "trending" else "⚠️" if status == "fading" else "\u2026"
        print(f"{tag} day {st['days_since_breakout']}, streak {st['streak']}, "
              f"{pct:+.1f}% from entry")

    save_json(STREAKS_STATE, {
        "updated": datetime.now().isoformat(timespec="seconds"),
        "streaks": new_streaks,
    })

    if alerts:
        print(f"\nSending {len(alerts)} streak alert(s) to Telegram...")
        for msg in alerts:
            send_telegram(msg)
            time.sleep(0.6)
    else:
        print("\nNo milestone/fade alerts this cycle.")

    print("\n" + "=" * 70)
    print(f"STREAK TRACKER  --  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 70)
    for s in new_streaks:
        marker = "\U0001F525" if s["status"] == "trending" else (
            "\u26A0" if s["status"] == "fading" else "\u2026")
        print(f"  {marker}  {s['ticker']:<5}  day {s['days_since_breakout']:>2}  "
              f"streak {s['current_streak']:>2}  "
              f"{s['pct_from_breakout']:+6.1f}%  "
              f"peak {s['peak_streak']}")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
