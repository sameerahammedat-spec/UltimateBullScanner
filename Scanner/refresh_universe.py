"""
Monthly universe refresh — redownloads the official NSE index constituent
lists so your scan always reflects the current Nifty 500 / Smallcap 250
membership (these lists change a few times a year on index reconstitution).

Run this once a month (scheduled task calls this before the scan).

Usage:
    python refresh_universe.py
"""

import shutil
import requests

FILES = {
    "nifty500.csv": "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
    "niftysmallcap250.csv": "https://archives.nseindia.com/content/indices/ind_niftysmallcap250list.csv",
}

# NSE blocks requests without a browser-like header
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/csv,*/*",
}


def refresh():
    for filename, url in FILES.items():
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            if len(resp.content) < 1000:  # sanity check — NSE sometimes returns an error page
                raise ValueError("Response too small, likely blocked/error page")
            # keep the old file as a .bak in case the new one is malformed
            try:
                shutil.copy(filename, filename + ".bak")
            except FileNotFoundError:
                pass
            with open(filename, "wb") as f:
                f.write(resp.content)
            print(f"Refreshed {filename} ({len(resp.content)} bytes)")
        except Exception as e:
            print(f"[!] Failed to refresh {filename}: {e}")
            print(f"    Keeping the existing local copy. You can also download manually from:\n    {url}")


if __name__ == "__main__":
    refresh()
