#!/usr/bin/env python3
"""Watch a Cinemark theatre for newly-released show dates and email when one appears.

Built for The Odyssey in IMAX 70mm at Cinemark Seven Bridges, but the movie and
theatre are just config values.

The site renders showtimes client-side from an Umbraco surface endpoint that
returns base64-encoded JSON. We call that endpoint directly -- no browser, no
dependencies, two requests per run.

Designed to run hourly under GitHub Actions with state committed back to the
repo, but it is an ordinary script and runs the same way locally.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import html
import json
import logging
import os
import random
import re
import smtplib
import ssl
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.toml"
STATE_PATH = HERE / "state.json"

# A current desktop Chrome UA. We are one user checking a public page hourly,
# so this is about looking ordinary, not about evading anything.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

FETCH_ATTEMPTS = 3
FETCH_TIMEOUT = 20
# Consecutive failed runs before we conclude something is actually broken and
# say so. At hourly cadence this is ~6 hours of silence.
FAILURE_ALERT_THRESHOLD = 6
HEARTBEAT_INTERVAL = timedelta(hours=24)

log = logging.getLogger("odyssey")


class FetchError(Exception):
    """The site could not be reached, or returned something unusable."""


# --------------------------------------------------------------------------
# config + state
# --------------------------------------------------------------------------

def load_config() -> dict:
    with CONFIG_PATH.open("rb") as fh:
        cfg = tomllib.load(fh)

    # Secrets never live in the file. Locally they come from the shell; in CI
    # from repo secrets.
    cfg["smtp_password"] = os.environ.get("SMTP_PASSWORD", "")
    cfg["smtp_from"] = os.environ.get("SMTP_FROM", "")
    cfg["smtp_to"] = os.environ.get("SMTP_TO", "")
    return cfg


def default_state() -> dict:
    return {
        "known_dates": [],
        "latest_date": None,
        "alerted_dates": [],
        "pending_alerts": [],
        "consecutive_failures": 0,
        "last_success": None,
        "last_heartbeat": None,
        "health_alert_sent": False,
    }


class FileStateStore:
    """State in a local file. Used when running the script directly."""

    label = "state.json"

    def __init__(self, path: Path = STATE_PATH):
        self.path = path

    def read(self) -> str | None:
        return self.path.read_text() if self.path.exists() else None

    def write(self, text: str) -> None:
        # Atomic: a crash mid-write cannot leave a half-written file behind.
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(text)
        tmp.replace(self.path)

    def quarantine(self) -> str:
        backup = self.path.with_suffix(".json.corrupt")
        try:
            self.path.replace(backup)
        except OSError:
            pass
        return backup.name


class BlobStateStore:
    """State in an Azure blob. Used by the timer-triggered Function.

    Blob writes are atomic server-side (a PUT either lands whole or not at
    all), so this needs no temp-file dance -- but it must present the same
    read/write/quarantine surface as the file store so the logic above it
    never has to know which one it is talking to.
    """

    label = "state blob"

    def __init__(self, connection_string: str, container: str, name: str):
        from azure.storage.blob import BlobServiceClient  # Azure-only import

        service = BlobServiceClient.from_connection_string(connection_string)
        self.container_client = service.get_container_client(container)
        try:
            self.container_client.create_container()
        except Exception:
            pass  # already exists
        self.name = name
        self.blob = self.container_client.get_blob_client(name)

    def read(self) -> str | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return self.blob.download_blob().readall().decode("utf-8")
        except ResourceNotFoundError:
            return None

    def write(self, text: str) -> None:
        self.blob.upload_blob(text.encode("utf-8"), overwrite=True)

    def quarantine(self) -> str:
        corrupt_name = f"{self.name}.corrupt"
        try:
            body = self.blob.download_blob().readall()
            self.container_client.get_blob_client(corrupt_name).upload_blob(
                body, overwrite=True)
        except Exception as exc:
            log.warning("could not preserve corrupt state: %s", exc)
        return corrupt_name


def load_state(cfg: dict, store) -> dict:
    """Read stored state, degrading safely rather than silently re-seeding.

    A corrupt state file is the one case where the obvious recovery is wrong:
    re-seeding from whatever the site says today would quietly adopt any new
    date as the baseline and never alert on it. Falling back to the configured
    watermark instead biases us toward a duplicate email, which is harmless,
    over a missed one, which is the whole point of the tool.
    """
    state = default_state()
    try:
        raw = store.read()
    except Exception as exc:
        log.error("could not read %s (%s); using the configured baseline",
                  store.label, exc)
        state["latest_date"] = cfg.get("baseline_latest_date")
        return state

    if raw is None:
        log.info("no %s yet; will seed a baseline this run", store.label)
        state["latest_date"] = cfg.get("baseline_latest_date")
        return state

    try:
        loaded = json.loads(raw)
        if not isinstance(loaded, dict):
            raise ValueError(f"{store.label} is not an object")
    except (json.JSONDecodeError, ValueError) as exc:
        backup = store.quarantine()
        log.error("%s unreadable (%s); preserved as %s and falling back "
                  "to the configured baseline", store.label, exc, backup)
        state["latest_date"] = cfg.get("baseline_latest_date")
        return state

    state.update(loaded)
    if not state.get("latest_date"):
        state["latest_date"] = cfg.get("baseline_latest_date")
    return state


def save_state(state: dict, store) -> None:
    store.write(json.dumps(state, indent=2, sort_keys=True) + "\n")


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def http_get(url: str, referer: str) -> str:
    """GET with retries. Raises FetchError once all attempts are spent."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
        "Connection": "close",
    }

    last_error: Exception | None = None
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", errors="replace")
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                gzip.BadGzipFile) as exc:
            last_error = exc
            if attempt < FETCH_ATTEMPTS:
                delay = 2 ** attempt + random.uniform(0, 2)
                log.warning("fetch attempt %d/%d failed (%s); retrying in %.1fs",
                            attempt, FETCH_ATTEMPTS, exc, delay)
                time.sleep(delay)

    raise FetchError(f"{url} failed after {FETCH_ATTEMPTS} attempts: {last_error}")


def fetch_show_dates(cfg: dict) -> list[date]:
    """Every date this theatre currently has showtimes for this movie."""
    url = (
        f"{cfg['base_url']}/umbraco/surface/showtimes/GetShowDatesbyTheaterIdMovieId"
        f"?cinemarkMovieId={cfg['movie_id']}&theaterId={cfg['theater_id']}"
    )
    body = http_get(url, referer=cfg["movie_url"]).strip().strip('"')

    try:
        decoded = base64.b64decode(body).decode("utf-8")
        raw_dates = json.loads(decoded)
    except Exception as exc:  # malformed payload == site changed shape
        raise FetchError(f"could not decode show dates response: {exc}") from exc

    dates = []
    for item in raw_dates:
        try:
            dates.append(datetime.fromisoformat(item).date())
        except (TypeError, ValueError):
            log.warning("skipping unparseable date %r", item)
    return sorted(set(dates))


def fetch_showtimes(cfg: dict, day: date) -> list[tuple[str, str]]:
    """Showtimes for one date as (label, booking_url) pairs.

    Best-effort only: this exists to make the alert email more useful, so any
    failure here is logged and swallowed. An alert must never be lost because
    a nice-to-have enrichment step broke.
    """
    url = (
        f"{cfg['base_url']}/umbraco/surface/Showtimes/GetByMovieId"
        f"?cinemarkMovieId={cfg['movie_id']}&showDate={day.isoformat()}"
        f"&theaterIds={cfg['theater_id']}&expandSearch=false"
        f"&currentTheaterId={cfg['theater_id']}"
    )
    try:
        markup = http_get(url, referer=cfg["movie_url"])
    except FetchError as exc:
        log.warning("could not fetch showtimes for %s: %s", day, exc)
        return []

    results: list[tuple[str, str]] = []
    pattern = re.compile(r'<a\b([^>]*class="showtime-link"[^>]*)>([^<]*)</a>')
    for match in pattern.finditer(markup):
        attrs, label = match.group(1), match.group(2).strip()
        href_match = re.search(r'href="([^"]+)"', attrs)
        if not href_match:
            continue
        href = html.unescape(href_match.group(1))
        if href.startswith("/"):
            href = cfg["base_url"] + href
        results.append((label, href))

    if not results:
        log.warning("no showtimes parsed for %s (markup may have changed)", day)
    return results


# --------------------------------------------------------------------------
# email
# --------------------------------------------------------------------------

def send_email(cfg: dict, subject: str, body: str) -> None:
    """Send one mail. Raises on any failure so the caller can queue a retry."""
    for key in ("smtp_password", "smtp_from", "smtp_to"):
        if not cfg.get(key):
            raise RuntimeError(
                f"{key.upper()} is not set -- refusing to continue. "
                "Set it in the environment (locally) or repo secrets (CI)."
            )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["smtp_from"]
    msg["To"] = cfg["smtp_to"]
    msg.set_content(body)

    with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=30) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(cfg["smtp_from"], cfg["smtp_password"])
        smtp.send_message(msg)
    log.info("sent email: %s", subject)


def pretty(day: date) -> str:
    return day.strftime("%a %b %-d, %Y")


def build_alert(cfg: dict, days: list[date]) -> tuple[str, str]:
    theatre = cfg["theater_name"]
    if len(days) == 1:
        subject = f"New {cfg['movie_short_name']} date: {pretty(days[0])} - {theatre}"
    else:
        subject = (f"{len(days)} new {cfg['movie_short_name']} dates "
                   f"({pretty(days[0])} onward) - {theatre}")

    lines = [
        f"New show date(s) just appeared for {cfg['movie_name']} at {theatre}.",
        "",
    ]
    for day in days:
        lines.append(f"  {pretty(day)}")
        for label, link in fetch_showtimes(cfg, day):
            lines.append(f"      {label}  ->  {link}")
        lines.append("")

    lines += [
        f"All showtimes: {cfg['movie_url']}",
        "",
        "-- odyssey-checker",
    ]
    return subject, "\n".join(lines)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def run_check(cfg: dict, state: dict, store, *, dry_run: bool,
              force_alert: bool) -> int:
    """Returns the process exit code."""
    now = datetime.now(timezone.utc)

    try:
        dates = fetch_show_dates(cfg)
    except FetchError as exc:
        state["consecutive_failures"] += 1
        log.error("fetch failed (%d consecutive): %s",
                  state["consecutive_failures"], exc)
        if (state["consecutive_failures"] >= FAILURE_ALERT_THRESHOLD
                and not state["health_alert_sent"] and not dry_run):
            maybe_send_health_alert(cfg, state, reason=str(exc))
        if not dry_run:
            save_state(state, store)
        return 1

    log.info("found %d show dates%s", len(dates),
             f" ({dates[0]} .. {dates[-1]})" if dates else "")

    # An empty list is the dangerous case: the script "worked" but the answer
    # is useless. Almost always it means Cinemark reissued the movie id, and it
    # is indistinguishable from "no new dates" unless we treat it specially.
    if not dates:
        state["consecutive_failures"] += 1
        log.error("show date list is EMPTY -- movie id %s may no longer be valid",
                  cfg["movie_id"])
        if not state["health_alert_sent"] and not dry_run:
            maybe_send_health_alert(
                cfg, state,
                reason=f"the API returned no dates at all for movie id "
                       f"{cfg['movie_id']}. Cinemark most likely reissued the id.")
        if not dry_run:
            save_state(state, store)
        return 1

    state["consecutive_failures"] = 0
    state["health_alert_sent"] = False
    state["last_success"] = now.isoformat()

    latest_known = (date.fromisoformat(state["latest_date"])
                    if state["latest_date"] else None)
    alerted = set(state["alerted_dates"])

    if latest_known is None:
        # First ever run: adopt today's answer as the baseline and stay quiet.
        state["latest_date"] = dates[-1].isoformat()
        state["known_dates"] = [d.isoformat() for d in dates]
        log.info("seeded baseline at %s; no alert on first run", dates[-1])
        if not dry_run:
            save_state(state, store)
        return 0

    new_dates = [d for d in dates
                 if d > latest_known and d.isoformat() not in alerted]
    if force_alert and not new_dates:
        new_dates = dates[-1:]
        log.info("--force-alert: pretending %s is new", new_dates[0])

    # Anything that failed to send on an earlier run gets retried here, which
    # is why a date is only marked alerted after the send actually succeeds.
    pending = sorted(set(state["pending_alerts"])
                     | {d.isoformat() for d in new_dates})

    # The watermark advances regardless of send success; pending_alerts is what
    # guarantees delivery, so we never need to re-detect the same date.
    state["latest_date"] = max(dates[-1].isoformat(), state["latest_date"])
    state["known_dates"] = [d.isoformat() for d in dates]

    exit_code = 0
    if pending:
        days = [date.fromisoformat(d) for d in pending]
        log.info("new date(s) to announce: %s", ", ".join(pending))
        if dry_run:
            subject, body = build_alert(cfg, days)
            print(f"\n--- would send ---\nSubject: {subject}\n\n{body}")
        else:
            try:
                subject, body = build_alert(cfg, days)
                send_email(cfg, subject, body)
                state["alerted_dates"] = sorted(alerted | set(pending))
                state["pending_alerts"] = []
            except Exception as exc:
                # Keep them pending so every later run retries the send.
                state["pending_alerts"] = pending
                log.error("could not send alert (queued for retry): %s", exc)
                exit_code = 2
    else:
        log.info("no new dates past %s", latest_known)
        if not dry_run:
            exit_code = send_heartbeat_if_due(cfg, state, now)

    if not dry_run:
        save_state(state, store)
    return exit_code


def maybe_send_health_alert(cfg: dict, state: dict, reason: str) -> None:
    """Tell the user the watcher itself looks broken. Sent at most once."""
    body = (
        "The Odyssey show-date watcher is not working correctly, so it may not "
        "be able to tell you when new dates appear.\n\n"
        f"Reason: {reason}\n\n"
        "To re-derive the movie id if Cinemark changed it:\n"
        f"  curl -s -A 'Mozilla/5.0' '{cfg['movie_url']}' | grep -o 'data-movieid=\"[0-9]*\"'\n\n"
        f"Then update movie_id in config.toml (currently {cfg['movie_id']}).\n\n"
        "-- odyssey-checker"
    )
    try:
        send_email(cfg, "Odyssey watcher may be broken", body)
        state["health_alert_sent"] = True
    except Exception as exc:
        log.error("could not send health alert: %s", exc)


def send_heartbeat_if_due(cfg: dict, state: dict, now: datetime) -> int:
    """A daily 'still alive' mail.

    Nothing running inside GitHub Actions can tell you that the schedule itself
    stopped firing -- a run that never happens produces no failure to notify
    on. A daily heartbeat inverts that: silence becomes the signal.
    """
    last = state.get("last_heartbeat")
    if last:
        try:
            if now - datetime.fromisoformat(last) < HEARTBEAT_INTERVAL:
                return 0
        except ValueError:
            pass

    latest = state.get("latest_date")
    pretty_latest = pretty(date.fromisoformat(latest)) if latest else "unknown"
    try:
        send_email(
            cfg,
            f"Odyssey watcher alive - latest date still {pretty_latest}",
            f"Checked {cfg['theater_name']} and found nothing new.\n"
            f"Latest available date: {pretty_latest}\n"
            f"Checked at: {now.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            "You get this once a day. If it stops arriving, the checker has "
            "stopped running -- look at the Actions tab.\n\n-- odyssey-checker",
        )
        state["last_heartbeat"] = now.isoformat()
    except Exception as exc:
        # A missed heartbeat is cosmetic; never fail the run over it.
        log.warning("could not send heartbeat: %s", exc)
    return 0


def print_status(state: dict) -> None:
    print(f"latest known date : {state.get('latest_date')}")
    print(f"dates tracked     : {len(state.get('known_dates', []))}")
    print(f"alerted dates     : {', '.join(state.get('alerted_dates', [])) or '(none)'}")
    print(f"pending alerts    : {', '.join(state.get('pending_alerts', [])) or '(none)'}")
    print(f"consecutive fails : {state.get('consecutive_failures')}")
    print(f"last success      : {state.get('last_success')}")
    print(f"last heartbeat    : {state.get('last_heartbeat')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                       help="check and report, but send nothing and save nothing")
    parser.add_argument("--test-email", action="store_true",
                       help="send a sample alert and exit")
    parser.add_argument("--force-alert", action="store_true",
                       help="treat the latest date as new (end-to-end test)")
    parser.add_argument("--reset-baseline", action="store_true",
                       help="re-seed the watermark from the site's current answer")
    parser.add_argument("--status", action="store_true",
                       help="print current state and exit")
    parser.add_argument("--no-jitter", action="store_true",
                       help="skip the randomized start delay")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    cfg = load_config()
    store = FileStateStore()
    state = load_state(cfg, store)

    if args.status:
        print_status(state)
        return 0

    if args.test_email:
        send_email(cfg, "Odyssey watcher test",
                   "If you are reading this, SMTP works.\n\n-- odyssey-checker")
        return 0

    if args.reset_baseline:
        dates = fetch_show_dates(cfg)
        state.update(latest_date=dates[-1].isoformat(),
                     known_dates=[d.isoformat() for d in dates],
                     alerted_dates=[], pending_alerts=[])
        save_state(state, store)
        log.info("baseline reset to %s", dates[-1])
        return 0

    # Spread the request across a minute so we are not hitting the site at a
    # fixed second of every hour.
    if not args.no_jitter and not args.dry_run:
        delay = random.uniform(0, 60)
        log.debug("sleeping %.1fs before fetching", delay)
        time.sleep(delay)

    force = args.force_alert or os.environ.get("FORCE_ALERT") == "true"
    return run_check(cfg, state, store, dry_run=args.dry_run, force_alert=force)


if __name__ == "__main__":
    sys.exit(main())
