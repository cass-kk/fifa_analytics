"""
Scraper for men's international friendly matches from inside.fifa.com.

Strategy: open the friendlies filter page in a real browser, dismiss the
cookie banner, then repeatedly click "Show more" until we have all matches
back to min_year. After each click we count how many match rows are on the
page. When the count stops growing we know we are done. We then parse
the rendered HTML once at the very end to extract every match.

Fields collected per match:
    date, home_team, away_team, home_score, away_score,
    tournament, stage, stadium

Run standalone:
    python friendly_scraper.py
    python friendly_scraper.py --min-year 1970
    python friendly_scraper.py --show-browser
    python friendly_scraper.py --no-export
"""

from __future__ import annotations

import argparse
import logging
import random
import re
import sys
import time
from datetime import datetime, timezone

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
import undetected_chromedriver as uc

from exporters import DataExporter

logger = logging.getLogger(__name__)

# friendlies filter on the FIFA archive page
FRIENDLY_URL = "https://inside.fifa.com/data-centre/matches?competitionClassificationCode=F"

# wait limits in seconds
PAGE_LOAD_WAIT = 30
SHOW_MORE_WAIT = 20

# random pause ranges (seconds) -- keeps request rate human-like
CLICK_PAUSE_MIN = 2.5
CLICK_PAUSE_MAX = 4.5
POLL_INTERVAL   = 1.5

# stop if row count doesn't grow after this many consecutive clicks
MAX_STALE_CLICKS = 3


def _pause(lo: float = CLICK_PAUSE_MIN, hi: float = CLICK_PAUSE_MAX) -> None:
    # random sleep so we don't hammer the server
    time.sleep(random.uniform(lo, hi))


def _build_driver(headless: bool = True) -> uc.Chrome:
    """Return an undetected Chrome instance."""
    options = uc.ChromeOptions()

    if headless:
        options.add_argument("--headless=new")

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1440,900")
    options.add_argument("--lang=en-GB")

    # point at the real Chrome binary
    chrome_binary = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    logger.info("Using Chrome binary: %s", chrome_binary)
    options.binary_location = chrome_binary

    driver = uc.Chrome(options=options, version_main=149)

    # hide the webdriver flag
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )

    return driver


def _dismiss_cookie_banner(driver: uc.Chrome) -> None:
    """Click the cookie accept button if it appears."""
    try:
        btn = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable(
                (By.XPATH, "//*[contains(text(), \"I'm OK with that\")]")
            )
        )
        btn.click()
        logger.info("Cookie banner dismissed")
        _pause(1.5, 2.5)
    except Exception:
        logger.info("No cookie banner -- continuing")


def _find_show_more(driver: uc.Chrome):
    """Return the Show more button element or None."""
    try:
        candidates = driver.find_elements(
            By.XPATH,
            "//*[contains(translate(text(), 'SHOWMRE', 'showmre'), 'show more')]"
        )
        for el in candidates:
            if el.is_displayed() and el.is_enabled():
                return el
    except Exception:
        pass
    return None


def _count_rows(driver: uc.Chrome) -> int:
    """Count how many match rows are currently rendered on the page."""
    try:
        # each match row contains a date cell -- count those as a proxy for row count
        rows = driver.find_elements(By.XPATH, "//tr[.//td]")
        return len(rows)
    except Exception:
        return 0


def _get_earliest_year(driver: uc.Chrome) -> int | None:
    """
    Find the earliest year visible in the date column.
    Dates on the page look like '10 Jun 2026' -- grab the 4-digit year.
    """
    try:
        page_text = driver.find_element(By.TAG_NAME, "body").text
        years = re.findall(r'\b(1\d{3}|20\d{2})\b', page_text)
        if years:
            return min(int(y) for y in years)
    except Exception:
        pass
    return None


def _parse_rows(driver: uc.Chrome) -> list[dict]:
    """
    Parse every visible match row from the rendered page HTML.

    The table columns visible in the screenshot are:
      Date | Match Result (home flag, home name, score, away name, away flag) | Tournament | Stage | Stadium

    We locate each <tr> that has at least one <td>, then extract text from
    each cell by position.
    """
    matches = []
    seen = set()

    try:
        rows = driver.find_elements(By.XPATH, "//tr[.//td]")
        logger.info("Parsing %s table rows", len(rows))

        for row in rows:
            try:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) < 4:
                    # not a match row
                    continue

                # cell 0: date  e.g. "10 Jun 2026"
                date_raw = cells[0].text.strip()

                # cell 1: match result block -- contains both team names and score
                result_cell = cells[1]
                result_text = result_cell.text.strip()

                # cell 2: tournament name
                tournament = cells[2].text.strip() if len(cells) > 2 else ""

                # cell 3: stage
                stage = cells[3].text.strip() if len(cells) > 3 else ""

                # cell 4: stadium
                stadium = cells[4].text.strip() if len(cells) > 4 else ""

                # parse score out of the result cell -- looks like "3\n0" or "FT\n3\n0"
                # team names are on separate lines too
                lines = [l.strip() for l in result_text.splitlines() if l.strip()]

                # remove "FT", "AET" etc status tokens
                status_tokens = {"FT", "AET", "PEN", "HT", "CANC", "PST", "-"}
                lines_no_status = [l for l in lines if l not in status_tokens]

                # score lines are purely numeric
                score_lines = [l for l in lines_no_status if re.match(r'^\d+$', l)]
                team_lines  = [l for l in lines_no_status if not re.match(r'^\d+$', l)]

                home_team  = team_lines[0] if len(team_lines) > 0 else ""
                away_team  = team_lines[1] if len(team_lines) > 1 else ""
                home_score = score_lines[0] if len(score_lines) > 0 else None
                away_score = score_lines[1] if len(score_lines) > 1 else None

                # skip rows that didn't parse into something useful
                if not date_raw or not home_team or not away_team:
                    continue

                # deduplicate by date + teams
                key = (date_raw, home_team, away_team)
                if key in seen:
                    continue
                seen.add(key)

                matches.append({
                    "date":        date_raw,
                    "home_team":   home_team,
                    "away_team":   away_team,
                    "home_score":  home_score,
                    "away_score":  away_score,
                    "tournament":  tournament,
                    "stage":       stage if stage != "-" else "",
                    "stadium":     stadium,
                })

            except Exception as exc:
                logger.debug("Row parse error: %s", exc)
                continue

    except Exception as exc:
        logger.warning("Failed to parse rows: %s", exc)

    return matches


def _parse_date(date_raw: str) -> str | None:
    """Convert '10 Jun 2026' to 'YYYY-MM-DD'."""
    try:
        return datetime.strptime(date_raw, "%d %b %Y").strftime("%Y-%m-%d")
    except Exception:
        return date_raw or None


def scrape_friendlies(
    min_year: int = 1950,
    headless: bool = True,
    export: bool = True,
    output_dir: str | None = None,
) -> list[dict]:
    """
    Scrape all men's international friendly matches back to min_year.
    Returns a list of match dicts.
    """
    logger.info("Starting friendly scraper (min_year=%s, headless=%s)", min_year, headless)

    driver = _build_driver(headless=headless)

    try:
        logger.info("Loading %s", FRIENDLY_URL)
        driver.get(FRIENDLY_URL)

        # wait for the main content area to appear
        WebDriverWait(driver, PAGE_LOAD_WAIT).until(
            EC.presence_of_element_located((By.TAG_NAME, "main"))
        )

        # dismiss cookie banner first so it doesn't block clicks
        _dismiss_cookie_banner(driver)

        # let the initial match list render
        _pause(3.0, 5.0)

        stale_clicks = 0
        click_num    = 0
        last_row_count = 0

        while True:
            current_row_count = _count_rows(driver)
            logger.info("Rows on page: %s", current_row_count)

            # check the earliest year visible -- stop if we've gone back far enough
            earliest = _get_earliest_year(driver)
            if earliest is not None:
                logger.info("Earliest year visible: %s", earliest)
                if earliest <= min_year:
                    logger.info("Reached min_year=%s -- stopping clicks", min_year)
                    break

            # check if the row count grew since last click
            if current_row_count <= last_row_count and click_num > 0:
                stale_clicks += 1
                logger.warning("Row count didn't grow (stale %s/%s)", stale_clicks, MAX_STALE_CLICKS)
                if stale_clicks >= MAX_STALE_CLICKS:
                    logger.info("Max stale clicks reached -- assuming end of data")
                    break
            else:
                stale_clicks = 0

            last_row_count = current_row_count

            # find and click Show more
            btn = _find_show_more(driver)
            if btn is None:
                logger.info("No Show More button -- end of data")
                break

            # scroll button into view then click
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
            _pause(1.0, 2.0)
            driver.execute_script("arguments[0].click();", btn)
            click_num += 1
            logger.info("Show More click #%s", click_num)

            # wait for new rows to appear
            deadline = time.time() + SHOW_MORE_WAIT
            while time.time() < deadline:
                _pause(POLL_INTERVAL, POLL_INTERVAL)
                new_count = _count_rows(driver)
                if new_count > current_row_count:
                    logger.info("New rows loaded (%s -> %s)", current_row_count, new_count)
                    break

            # human pause before next click
            _pause()

        # parse all visible rows now that everything is loaded
        logger.info("Parsing all loaded match rows...")
        matches = _parse_rows(driver)

    finally:
        driver.quit()
        logger.info("Browser closed")

    # normalise dates and filter to min_year
    for m in matches:
        m["date"] = _parse_date(m["date"])

    matches = [
        m for m in matches
        if (m.get("date") or "")[:4].isdigit()
        and int((m.get("date") or "0000")[:4]) >= min_year
    ]

    logger.info("Total friendly matches collected: %s", len(matches))

    if export and matches:
        _export(matches, output_dir)

    return matches


def _export(matches: list[dict], output_dir: str | None) -> None:
    """Write matches to JSON and Excel."""
    exporter = DataExporter(output_dir=output_dir) if output_dir else DataExporter()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    basename  = f"mens_international_friendlies_{timestamp}"

    exporter.export_bundle(
        {"matches": matches},
        basename,
        metadata={
            "source": "inside.fifa.com friendlies archive",
            "competition_classification": "F",
            "gender": "mens",
            "count": len(matches),
        },
    )

    exporter.export_excel({"matches": matches}, basename)
    logger.info("Exported %s matches to output/%s", len(matches), basename)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scrape men's international friendly matches from inside.fifa.com"
    )
    parser.add_argument(
        "--min-year", type=int, default=1950,
        help="Collect matches back to this year (default: 1950)",
    )
    parser.add_argument(
        "--show-browser", action="store_true",
        help="Run with a visible browser window (useful for debugging)",
    )
    parser.add_argument(
        "--no-export", action="store_true",
        help="Skip writing output files",
    )
    parser.add_argument(
        "--output-dir", type=str, default=None,
        help="Override output directory (default: output/)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = _build_parser()
    args   = parser.parse_args(argv)

    if args.min_year < 1900 or args.min_year > datetime.now().year:
        print(f"Invalid --min-year: {args.min_year}", file=sys.stderr)
        return 1

    matches = scrape_friendlies(
        min_year=args.min_year,
        headless=not args.show_browser,
        export=not args.no_export,
        output_dir=args.output_dir,
    )

    print(f"Done. {len(matches)} friendly matches collected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())