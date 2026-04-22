# Northway Eleven B4 Watcher

Polls the Eagle Rock availability page every 15 minutes. Pushes a phone
notification the moment a new B4 unit is listed.

## One-time setup

### 1. Install the ntfy app and pick a topic

1. Install **ntfy** from the App Store or Google Play.
2. In the app, tap the `+` and subscribe to a new topic.

   **Important:** this repo is public, so treat the topic name like a
   password. ntfy topics have no auth — anyone who knows the name can
   send you notifications and read what's published. Make yours a long
   random string, e.g. `nw11-b4-k7f2q9x3m8vp`. Never paste it into the
   code, a commit message, or an issue.
3. Test it: on your computer, run
   `curl -d "hello" ntfy.sh/your-topic-name` — your phone should buzz
   within a few seconds.

### 2. Add your topic name as a secret

On the repo page: **Settings → Secrets and variables → Actions → New
repository secret**.

- Name: `NTFY_TOPIC`
- Value: your topic name (e.g. `nw11-b4-k7f2q9x3m8vp`)

GitHub Actions automatically masks secret values in run logs, so the
topic name won't be exposed there as long as it only ever comes from
`${{ secrets.NTFY_TOPIC }}`.

### 3. Run it once manually

**Actions tab → Check B4 availability → Run workflow**. Watch the logs.
You should see something like:

```
Found 1 B4 unit(s): [{'apt': '...', 'available': '07/15/2026', 'price': '$...'}]
NEW: [...]
Sent ntfy notification to ***
```

On the first run *every* B4 unit counts as "new," so you'll get one
notification for the July unit that's already listed. Every subsequent
run only pings you for units it hasn't seen before.

## How it works

- `check.py` uses Playwright (a headless Chromium) to load the page, find
  the B4 section, and parse out apartment number + available date + price
  for each listed unit.
- It compares against `state.json` (the last run's results, committed to
  the repo).
- Any apartment number not in the previous state triggers a push via
  `POST https://ntfy.sh/<your-topic>`.
- The workflow then commits the updated `state.json` so the next run has
  fresh context.

## If something breaks

The scraper is defensive but the site's markup can change. If a run finds
zero units when you know there should be some, the workflow uploads the
rendered HTML as a `debug-page` artifact — download it from the Actions
run, open it, and adjust the selectors in `extract_b4_units`.

## Poll frequency and GitHub's cron quirks

Cron is set to `*/15 * * * *` (every 15 min). GitHub Actions often delays
scheduled runs 5–15 minutes under load, so real-world latency is
15–30 minutes. Public repos get unlimited Actions minutes, so poll
frequency isn't a billing concern.

## Stopping it

When you sign a lease: archive or delete the repo, or disable the
workflow under **Actions → Check B4 availability → ⋯ → Disable
workflow**.