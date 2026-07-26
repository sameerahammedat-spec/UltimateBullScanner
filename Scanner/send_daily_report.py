"""
Builds a styled HTML email from a fresh scan and sends it via Gmail SMTP.

SECURITY - do this once before using:
  1. Turn on 2-Step Verification on the SENDING Gmail account (can be the
     same address you're mailing to, or a separate one).
  2. Create an "App Password": Google Account -> Security -> 2-Step
     Verification -> App passwords. Generate one for "Mail".
  3. Set two environment variables (do NOT hardcode the password in this
     file or paste it into chat):
       Windows (persist across sessions), run once in cmd.exe:
         setx GMAIL_SENDER "youraddress@gmail.com"
         setx GMAIL_APP_PASSWORD "the16charapppassword"
       Then close and reopen the terminal/Task Scheduler will pick it up
       on new sessions.

Usage:
    python send_daily_report.py trading_toolkit.xlsx nifty500.csv niftysmallcap250.csv
"""

import os
import sys
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import date

from market_scanner import run_scan

TO_ADDRESS = "sa2337308@gmail.com"
SUBJECT_PREFIX = "Daily Market Scan"

NAVY = "#1F3864"
GOLD = "#C9A227"
GREEN = "#2E7D32"
AMBER = "#B8860B"
RED = "#B00020"
LIGHT_BG = "#F4F6F9"
ROW_ALT = "#F2F2F2"


def verdict_color(v):
    return {"STRONG SETUP": GREEN, "WATCHLIST": AMBER, "AVOID": RED}.get(v, "#555")


def fmt(v, nd=2):
    if v is None:
        return "—"
    try:
        return f"{round(float(v), nd):,}"
    except (TypeError, ValueError):
        return str(v)


def build_filtered_table(rows):
    """Table 1: the main filtered shortlist (STRONG SETUP + WATCHLIST)."""
    head = ["Symbol", "Name", "Segment", "Close", "% Chg", "Score", "Verdict"]
    thead = "".join(f'<th style="padding:8px 10px;background:{NAVY};color:#fff;font-size:12px;text-align:left;">{h}</th>' for h in head)
    body_rows = []
    for i, d in enumerate(rows):
        bg = ROW_ALT if i % 2 else "#ffffff"
        vcolor = verdict_color(d["verdict"])
        body_rows.append(f"""
        <tr style="background:{bg};">
          <td style="padding:7px 10px;font-weight:600;">{d['symbol']}</td>
          <td style="padding:7px 10px;color:#444;">{d['name']}</td>
          <td style="padding:7px 10px;">{d.get('cap_segment','')}</td>
          <td style="padding:7px 10px;">{fmt(d['close'])}</td>
          <td style="padding:7px 10px;color:{'#2E7D32' if (d['pct_change'] or 0) >= 0 else '#B00020'};">{fmt(d['pct_change'])}%</td>
          <td style="padding:7px 10px;text-align:center;">{d['score']}/9</td>
          <td style="padding:7px 10px;"><span style="background:{vcolor};color:#fff;padding:3px 8px;border-radius:10px;font-size:11px;font-weight:600;">{d['verdict']}</span></td>
        </tr>""")
    return f"""
    <table style="width:100%;border-collapse:collapse;font-family:Segoe UI,Arial,sans-serif;font-size:13px;">
      <thead><tr>{thead}</tr></thead>
      <tbody>{''.join(body_rows)}</tbody>
    </table>"""


def build_deepdive_table(rows):
    """Table 2: trend + risk levels for the top-ranked names."""
    head = ["Symbol", "Segment", "Close", "Vol Ratio", "Trend", "Strength",
            "Entry", "Stop Loss", "Target 1", "Target 2"]
    thead = "".join(f'<th style="padding:8px 10px;background:{NAVY};color:#fff;font-size:12px;text-align:left;">{h}</th>' for h in head)
    body_rows = []
    for i, d in enumerate(rows):
        bg = ROW_ALT if i % 2 else "#ffffff"
        tcolor = GREEN if d.get("trend_direction") == "Uptrend" else RED
        body_rows.append(f"""
        <tr style="background:{bg};">
          <td style="padding:7px 10px;font-weight:600;">{d['symbol']}</td>
          <td style="padding:7px 10px;">{d.get('cap_segment','')}</td>
          <td style="padding:7px 10px;">{fmt(d['close'])}</td>
          <td style="padding:7px 10px;">{fmt(d.get('vol_ratio_3d'))}x</td>
          <td style="padding:7px 10px;color:{tcolor};font-weight:600;">{d.get('trend_direction','N/A')}</td>
          <td style="padding:7px 10px;">{d.get('trend_strength','N/A')}</td>
          <td style="padding:7px 10px;">{fmt(d.get('entry'))}</td>
          <td style="padding:7px 10px;color:{RED};">{fmt(d.get('stop_loss'))}</td>
          <td style="padding:7px 10px;color:{GREEN};">{fmt(d.get('target1'))}</td>
          <td style="padding:7px 10px;color:{GREEN};">{fmt(d.get('target2'))}</td>
        </tr>""")
    return f"""
    <table style="width:100%;border-collapse:collapse;font-family:Segoe UI,Arial,sans-serif;font-size:13px;">
      <thead><tr>{thead}</tr></thead>
      <tbody>{''.join(body_rows)}</tbody>
    </table>"""


def build_email_html(full_scan, deep_dive, today):
    shortlist = [d for d in full_scan if d["verdict"] in ("STRONG SETUP", "WATCHLIST")][:40]
    strong_count = sum(1 for d in full_scan if d["verdict"] == "STRONG SETUP")
    watch_count = sum(1 for d in full_scan if d["verdict"] == "WATCHLIST")

    return f"""
    <div style="background:{LIGHT_BG};padding:24px;font-family:Segoe UI,Arial,sans-serif;">
      <div style="max-width:900px;margin:0 auto;background:#ffffff;border-radius:10px;overflow:hidden;box-shadow:0 2px 10px rgba(0,0,0,0.08);">

        <div style="background:linear-gradient(135deg,{NAVY},#0f1f3d);padding:26px 30px;">
          <div style="color:{GOLD};font-size:12px;letter-spacing:2px;font-weight:700;">DAILY MARKET SCAN</div>
          <div style="color:#ffffff;font-size:22px;font-weight:700;margin-top:4px;">{today.strftime('%A, %d %B %Y')}</div>
          <div style="color:#c9d4e3;font-size:13px;margin-top:6px;">
            {len(full_scan)} stocks scanned &middot; {strong_count} Strong Setups &middot; {watch_count} Watchlist
          </div>
        </div>

        <div style="padding:22px 30px;">
          <h3 style="color:{NAVY};font-size:15px;margin:0 0 10px 0;border-left:4px solid {GOLD};padding-left:10px;">
            Shortlist — Strong Setups &amp; Watchlist
          </h3>
          {build_filtered_table(shortlist)}
        </div>

        <div style="padding:0 30px 22px 30px;">
          <h3 style="color:{NAVY};font-size:15px;margin:20px 0 10px 0;border-left:4px solid {GOLD};padding-left:10px;">
            Deep Dive — Trend, Entry &amp; Risk Levels (Top {len(deep_dive)})
          </h3>
          {build_deepdive_table(deep_dive)}
          <p style="font-size:11px;color:#888;margin-top:10px;line-height:1.5;">
            Entry/Stop Loss/Target levels are ATR(14)-based (1.5x risk, 1.5x &amp; 3x reward) in the
            direction of the current 20-day trend. Trend Strength uses ADX(14): below 20 is
            weak/range-bound, 20&ndash;40 is moderate, above 40 is strong. This is technical-analysis
            math, not a prediction of how many days a move will last — always size positions to
            your own risk tolerance.
          </p>
        </div>

        <div style="background:{LIGHT_BG};padding:16px 30px;font-size:11px;color:#777;line-height:1.5;">
          Generated automatically from Nifty 500 (includes Nifty Midcap 150 &amp; Smallcap 250 constituents).
          P/E, ROE, Debt/Equity and QoQ growth are best-effort from Yahoo Finance — verify on Screener.in
          before acting. This report is for informational purposes only and is not investment advice.
          Markets carry risk of loss.
        </div>
      </div>
    </div>
    """


def send_email(html_body, today):
    sender = os.environ.get("GMAIL_SENDER")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    if not sender or not app_password:
        print("[!] GMAIL_SENDER / GMAIL_APP_PASSWORD environment variables not set. See the "
              "setup instructions at the top of this file. Email not sent.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"{SUBJECT_PREFIX} — {today.strftime('%d %b %Y')}"
    msg["From"] = sender
    msg["To"] = TO_ADDRESS
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, app_password)
        server.sendmail(sender, [TO_ADDRESS], msg.as_string())
    print(f"Email sent to {TO_ADDRESS}")
    return True


def main(xlsx_path, universe_paths):
    today = date.today()
    full_scan, early_momentum, deep_dive = run_scan(xlsx_path, universe_paths)
    html = build_email_html(full_scan, deep_dive, today)

    # Save a local copy of the email for reference/debugging
    out_path = f"daily_report_{today.strftime('%Y%m%d')}.html"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Report saved locally to {out_path}")

    send_email(html, today)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python send_daily_report.py trading_toolkit.xlsx nifty500.csv [niftysmallcap250.csv ...]")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2:])
