"""
One-shot patcher for the existing afternoon_fresh_move_scanner.py.

It does NOT replace the scanner. It adds the Results Catalyst layer by:
1. importing ResultsMonitor and enrich_results_score;
2. creating a monitor in AfternoonScanner.__init__;
3. augmenting the stage-0 universe with today's A/B results watchlist;
4. enriching deep candidates with results metadata and score overlay;
5. adding results columns to the output.

Run from Scanner:
    python results_integration_patch.py afternoon_fresh_move_scanner.py

A .bak copy is created before modification.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


def patch(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    original = text

    if "from results_monitor import ResultsMonitor" not in text:
        needle = "from market_scanner import _SHARED_SESSION"
        if needle not in text:
            raise SystemExit("Could not find scanner import anchor.")
        text = text.replace(
            needle,
            needle
            + "\nfrom results_monitor import ResultsMonitor\n"
            + "from results_score import enrich_results_score\n",
            1,
        )

    if "self.results_monitor = ResultsMonitor(" not in text:
        needle = "self._cycle_number: int = 0"
        if needle not in text:
            raise SystemExit("Could not find AfternoonScanner __init__ anchor.")
        replacement = needle + """
        self.results_monitor = ResultsMonitor(
            universe,
            calendar_path=os.getenv("RESULTS_CALENDAR_PATH", "results_calendar.csv"),
            refresh_minutes=int(os.getenv("RESULTS_REFRESH_MINUTES", "10")),
            timeout_seconds=int(os.getenv("RESULTS_REQUEST_TIMEOUT", "15")),
        )
"""
        text = text.replace(needle, replacement, 1)

    if "results_watchlist = self.results_monitor.watchlist(now)" not in text:
        needle = """        print(
            f"[Afternoon stage0] {source_note}: verifying {len(scan_universe)} of "
            f"{len(self.universe)} universe names with 5-minute bars"
        )"""
        if needle not in text:
            raise SystemExit("Could not find broad_scan stage0 print anchor.")

        replacement = """        # Results Catalyst: force today's scheduled A/B result names into
        # stage-0 verification so they are monitored even when they are not
        # already among BSE's live gainers.
        results_watchlist = self.results_monitor.watchlist(now)
        if results_watchlist:
            existing = {
                str(x.get("security_code") or x.get("security_id") or x.get("ticker")).upper()
                for x in scan_universe
            }
            added = 0
            for result_stock in results_watchlist:
                key = str(
                    result_stock.get("security_code")
                    or result_stock.get("security_id")
                    or result_stock.get("ticker")
                ).upper()
                if key and key not in existing:
                    result_stock = dict(result_stock)
                    result_stock["stage0_source"] = "RESULTS_TODAY_PRIORITY"
                    scan_universe.append(result_stock)
                    existing.add(key)
                    added += 1
            print(
                f"[Results monitor] today's scheduled results matched={len(results_watchlist)} "
                f"| added to stage0={added}"
            )

        print(
            f"[Afternoon stage0] {source_note}: verifying {len(scan_universe)} of "
            f"{len(self.universe)} universe names with 5-minute bars"
        )"""
        text = text.replace(needle, replacement, 1)

    if "results_event = self.results_monitor.event_for_stock(stock, now)" not in text:
        needle = """            item = {
                **stock,
                "ticker": ticker,"""
        if needle not in text:
            raise SystemExit("Could not find deep-scan candidate anchor.")

        replacement = """            results_event = self.results_monitor.event_for_stock(stock, now)
            item = {
                **stock,
                "ticker": ticker,"""
        text = text.replace(needle, replacement, 1)

        # Insert result metadata immediately after item construction closes.
        needle2 = """                "_daily_frame": daily,
            }
            (rejected if item["rejected"] else provisional).append(item)"""
        if needle2 not in text:
            raise SystemExit("Could not find item construction completion anchor.")

        replacement2 = """                "_daily_frame": daily,
            }

            if results_event is not None:
                item["results_scheduled_today"] = True
                item["results_result_date"] = results_event.result_date
                item["results_board_meeting_date"] = results_event.board_meeting_date
                item["results_expected_time"] = results_event.expected_time
                item["results_source"] = results_event.source
                item["results_type"] = results_event.result_type
                item["results_release_confirmed"] = results_event.release_confirmed
                item["results_release_time"] = results_event.release_time
                item["results_priority"] = (
                    "A_RESULTS_TODAY"
                    if str(stock.get("group") or "").upper() == "A"
                    else "B_RESULTS_TODAY"
                    if str(stock.get("group") or "").upper() == "B"
                    else "RESULTS_TODAY"
                )
                item = enrich_results_score(
                    item,
                    results_scheduled_today=True,
                    release_confirmed=results_event.release_confirmed,
                )
            else:
                item["results_scheduled_today"] = False
                item["results_release_confirmed"] = False
                item["results_priority"] = "NORMAL"

            (rejected if item["rejected"] else provisional).append(item)"""
        text = text.replace(needle2, replacement2, 1)

    # Add output columns if not already present.
    if '"results_scheduled_today"' not in text.split("LATEST_COLUMNS = [", 1)[1].split("]", 1)[0]:
        needle = '    "reasons", "warnings", "hard_rejects",\n'
        if needle not in text:
            raise SystemExit("Could not find LATEST_COLUMNS anchor.")
        replacement = needle + (
            '    "results_scheduled_today", "results_release_confirmed", '
            '"results_result_date", "results_board_meeting_date", '
            '"results_expected_time", "results_source", "results_type", '
            '"results_priority", "results_priority_bonus", "extension_penalty", '
            '"score_before_results_overlay", "results_setup_score",\n'
        )
        text = text.replace(needle, replacement, 1)

    if text == original:
        print("Scanner already appears to contain the Results Catalyst integration.")
        return

    backup = path.with_suffix(path.suffix + ".pre_results.bak")
    shutil.copy2(path, backup)
    path.write_text(text, encoding="utf-8")

    print(f"PATCHED: {path}")
    print(f"BACKUP : {backup}")


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python results_integration_patch.py afternoon_fresh_move_scanner.py")
        return 2
    patch(Path(sys.argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
