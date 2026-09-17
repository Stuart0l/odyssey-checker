"""Azure Functions entry point: run the Odyssey check on a timer.

All the logic lives in check_odyssey.py; this module only supplies the trigger
and points the checker at blob storage instead of a local file.

Why this exists at all: GitHub Actions' scheduler turned out to drop roughly
three of every four hourly slots (observed gaps of 2-8 hours over four days),
which is fine for a best-effort job and useless for a watcher whose whole value
is noticing something quickly. An Azure timer trigger is a real scheduler.
"""

import logging
import os
import random
import time

import azure.functions as func

import check_odyssey as core

app = func.FunctionApp()

STATE_CONTAINER = "odyssey"
STATE_BLOB = "state.json"


@app.timer_trigger(
    arg_name="timer",
    # NCRONTAB is {second} {minute} {hour} {day} {month} {day-of-week}, UTC.
    # Minute 23 rather than 0: nothing depends on it, but it keeps us off the
    # top-of-hour stampede that every other scheduled job in the world uses.
    schedule="0 23 * * * *",
    run_on_startup=False,
    # Catches up a run that was missed while the app was stopped or redeploying
    # -- the same role Persistent=true plays for a systemd timer.
    use_monitor=True,
)
def odyssey_check(timer: func.TimerRequest) -> None:
    logging.info("odyssey check starting (past_due=%s)", timer.past_due)

    # Small spread so we are not hitting Cinemark at an identical second every
    # hour. Cheap: well inside the Consumption free grant.
    time.sleep(random.uniform(0, 30))

    cfg = core.load_config()
    store = core.BlobStateStore(
        os.environ["AzureWebJobsStorage"], STATE_CONTAINER, STATE_BLOB)
    state = core.load_state(cfg, store)

    exit_code = core.run_check(
        cfg, state, store,
        dry_run=False,
        force_alert=os.environ.get("FORCE_ALERT") == "true",
    )

    # Surface failures to Azure as failed invocations, so the platform's own
    # monitoring reflects them instead of every run looking green.
    if exit_code:
        raise RuntimeError(
            f"odyssey check finished with exit code {exit_code} "
            "(1 = fetch failed, 2 = alert queued but could not be emailed)")

    logging.info("odyssey check finished cleanly")
