# odyssey-checker

Watches **Cinemark Seven Bridges and IMAX** (Woodridge, IL) for newly-released
show dates of *The Odyssey* in **IMAX 70mm**, and emails as soon as one appears.

Runs hourly as an Azure Function on a timer trigger. No server to maintain, no
browser, and the checker itself has no dependencies.

Current watermark: **2026-09-30**. Anything later triggers an alert.

## How it works

Cinemark renders showtimes client-side from an Umbraco endpoint that returns
base64-encoded JSON:

```
GET /umbraco/surface/showtimes/GetShowDatesbyTheaterIdMovieId
    ?cinemarkMovieId=104867&theaterId=276
```

`check_odyssey.py` calls it, decodes the date list, and compares the newest date
against the watermark in `state.json`. If the theatre has added a later date, it
fetches that date's showtimes and emails you with direct seat-map links.

The comparison is against a stored watermark, not against "what changed since
last run", so **missed runs cost nothing** — a skipped hour or a whole skipped
day is recovered automatically by the next run.

State lives in a blob in the Function App's storage account.

### Why not GitHub Actions

It was, for four days. GitHub's scheduled triggers are explicitly best-effort,
and in practice they dropped about three of every four hourly slots: 22 runs
where there should have been ~96, with gaps of 2 to 7.8 hours. Every run that
did fire succeeded -- the checker was never the problem, the scheduler was.
An Azure timer trigger is a real scheduler, so the hourly requirement is
actually met. The workflow in `.github/` is kept as a manually-triggerable
backup (`workflow_dispatch` runs are not throttled).

## Setup

1. Create a Gmail **app password**: Google Account → Security → 2-Step
   Verification (must be on) → App passwords → "Mail". A normal account
   password will not authenticate over SMTP. `SMTP_FROM` must be the Gmail
   account that issued the password -- it doubles as the SMTP username, and
   Gmail rejects a `From` that isn't the authenticated account or a verified
   alias. `SMTP_TO` can be any address.

2. Set the three values as Function App settings:

   ```sh
   az functionapp config appsettings set -g odyssey-checker-rg -n <app-name> \
     --settings SMTP_PASSWORD='...' SMTP_FROM='...' SMTP_TO='...'
   ```

3. Deploy and confirm:

   ```sh
   az functionapp deployment source config-zip \
     -g odyssey-checker-rg -n <app-name> --src app.zip --build-remote true
   az functionapp logs tail -g odyssey-checker-rg -n <app-name>
   ```

The same three values are also GitHub repo secrets, for the backup workflow.

## Running locally

```sh
python3 check_odyssey.py --dry-run --verbose   # check, print, change nothing
python3 check_odyssey.py --status              # what state currently says
SMTP_PASSWORD=... SMTP_FROM=... SMTP_TO=... \
  python3 check_odyssey.py --test-email        # prove SMTP works
python3 check_odyssey.py --reset-baseline      # re-seed the watermark
```

Needs Python 3.11+ (for stdlib `tomllib`). On macOS, the python.org build often
ships without CA certificates — run `Install Certificates.command`, or use the
Homebrew python, otherwise every fetch fails with `CERTIFICATE_VERIFY_FAILED`.

## When something goes wrong

Failures are handled in tiers, each catching what the one below can't:

| What fails | What happens |
|---|---|
| A single request | 3 retries with backoff; then the next hourly run |
| A whole run | Non-zero exit raises, so it shows as a failed invocation in Azure |
| A run is skipped entirely | Nothing to do — the next run re-reads everything. `use_monitor=True` also replays a schedule missed while the app was down |
| The email won't send | Date is queued in `pending_alerts` and retried every run until it sends |
| Cinemark changes the movie id | Empty result is detected and you get a "watcher may be broken" email |
| The schedule stops firing | Daily heartbeat email — if it stops arriving, go look |

The movie-id case is the subtle one: an empty result looks exactly like "no new
dates", so it's treated as an error rather than a quiet success.

**If you get the "watcher may be broken" email**, re-derive the ids:

```sh
curl -s -A 'Mozilla/5.0' https://www.cinemark.com/movies/the-odyssey-imax-70mm \
  | grep -o 'data-movieid="[0-9]*"'
```

and update `movie_id` in `config.toml`. Note that IMAX 70mm has its own movie
id, distinct from the standard release — using the wrong one silently watches
the wrong format.

## Watching a different movie or theatre

Everything is in `config.toml`. The theatre id comes from any theatre page:

```sh
curl -s -A 'Mozilla/5.0' https://www.cinemark.com/theatres/il-woodridge/cinemark-seven-bridges-and-imax \
  | grep -o 'TheaterId=[0-9]*' | head -1
```

Cinemark's Cloudflare rejects the default Python user-agent with a 403, so the
`-A` flag above isn't optional — and neither is the UA header the script sends.

## Teardown

Everything lives in one resource group:

```sh
az group delete -n odyssey-checker-rg --yes
```

## Caveats

- The endpoint is Cinemark's own public AJAX call, not a contracted API. It can
  change shape without notice — which is what the health alert is for.
- Timer schedules are UTC unless `WEBSITE_TIME_ZONE` is set. Irrelevant at
  hourly cadence, but worth knowing before changing the cron.
