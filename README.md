# AI Signal Tracker

[![CI](https://github.com/josephs-ai/twitter-info-grabber/workflows/CI/badge.svg)](https://github.com/josephs-ai/twitter-info-grabber/actions)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

Follows a curated set of AI researchers on X and surfaces only the posts that
are both **new** and **worth reading** — then extracts the actual findings, so
you get the information rather than a reading list.

A recent day: 2,635 posts collected, 3 surfaced. One of them:

> **Training a single Transformer layer during RL post-training recovers most of
> the gains from full-parameter RL, with high-contribution layers concentrated in
> the middle 40–60% of the network.**
>
> - The pattern holds across 7 models, 3 RL algorithms, and math, code, and agentic tasks
> - Layer importance rankings stay correlated across datasets and tasks
>
> `1` layers trained · `40–60%` network depth · `7` models · `3` RL algorithms
>
> *Why it matters: post-training compute and memory can drop sharply by updating
> one selected layer instead of all parameters.*

That post came from an account with a few hundred followers. Nobody would have
told you to follow them.

---

## Read this before you start

**It scrapes X, which is against their terms of service.** Use a throwaway
account, never your own. Realistic worst case is that account getting
suspended. Collection is deliberately slow and low-volume, which helps, but the
risk is yours.

**It needs an Anthropic API key.** Roughly **$5/month** at ~70 judgements a day.
The system prompt is cached, so most calls cost a fraction of a cent.

**It is not useful on day one.** Novelty is measured against your own corpus, so
until there is a corpus, everything looks new. Give it two or three days of
collection before judging the output.

**It is a personal tool.** The data is the product; this repo is just the
machinery. Expect to edit `seeds.txt` and tune the judge to your taste.

---

## Setup

Python 3.11+. A desktop session is needed for the app and for signing in.

```bash
git clone https://github.com/josephs-ai/twitter-info-grabber.git
cd twitter-info-grabber
python3 bootstrap.py          # any platform
```

Then add your key to `.env` and sign in to a burner account:

| | Linux / macOS | Windows |
|---|---|---|
| sign in | `./run login` | `run.cmd login` |
| first collection | `./run collect --all` | `run.cmd collect --all` |
| full pipeline | `./run daily` | `run.cmd daily` |
| desktop app | `./run app` | `run.cmd app` |

`./run` and `run.cmd` are thin wrappers around `python -m tracker`, which works
directly if you prefer.

### Scheduling

`daily` is idempotent, so run it as often as you like. Three times a day is
plenty.

**Linux / macOS** — `crontab -e`:

```
0 7,13,19 * * * /full/path/to/twitter-info-grabber/run daily >> /full/path/to/logs/daily.log 2>&1
```

Use absolute paths; cron does not run from your project directory.

**Windows** — Task Scheduler, or from PowerShell:

```powershell
schtasks /create /tn "AI Signal" /tr "C:\path\to\run.cmd daily" /sc daily /st 07:00
```

### Platform notes

Tested in CI on **Linux, macOS and Windows**, against Python 3.11 and 3.13.

The one genuinely platform-specific piece is the desktop app's GUI backend:
pywebview uses WebKit2GTK on Linux, Cocoa on macOS, and WebView2 on Windows.
The macOS and Windows dependencies come from PyPI automatically; Linux needs
system packages pip cannot provide (`python3-gi`, `gir1.2-webkit2-4.1`).

Everything else — collection, dedup, judging, extraction — is Python, SQLite
and a headless browser.

Run the same checks locally:

```bash
python tests/smoke.py
```

---

## How it works

Nine stages. Cheap deterministic filters first, the model only at the narrow end.

| Stage | What it does |
|---|---|
| `collect` | Drives a browser, intercepts X's internal GraphQL responses. Not DOM scraping — the JSON shape is far more stable than the markup. |
| `replies` | Mines conversations under tracked posts. Discovery *and* content: a sharp correction can outrank the post it replies to. |
| `suggest` | Harvests who tracked accounts follow, ranked by how many of them follow each candidate. |
| `links` | Resolves URLs, so "great paper: `<link>`" carries signal. arXiv abstracts get a dedicated path. |
| `amplify` | Counts how many tracked accounts independently shared each original. Corroboration you cannot see post-by-post. |
| `threads` | Stitches self-threads into one unit. A 12-post thread is one idea, not twelve. |
| `dedup` | Embeds every post twice — lexical and semantic — and takes the max. Near-duplicates drop before any API call. |
| `judge` | Scores novelty and value separately, seeing the complete unit: thread, quoted post, link content, images, amplifier count. |
| `extract` | Pulls the headline, claims, figures, and entities out of whatever survived. |

Two ideas do most of the work.

**Novelty is relative to your corpus, not to a model's training data.** Asking a
model "is this new?" gets you a judgement against a months-stale snapshot with
no idea what you have already been shown. Instead, each candidate is compared
against everything collected in a rolling window; borderline cases go to the
judge *with the similar prior posts attached*, turning a vague question into a
grounded comparison.

**Nothing is ever deleted.** Dropped posts keep their similarity score, nearest
match, and drop reason. "What did the filter throw away, and was any of it
good?" stays a query rather than a guess.

---

## Teaching it your taste

The judge ships with a generic idea of what matters. Yours is specific.

```bash
./run review --rate     # walk through decisions, rate each
./run app               # or use the Rate queue view
```

Ratings become few-shot examples in the judge's prompt. **Disagreements teach it
most** — a post it skipped that you rated useful is a correction, and those are
weighted first. After editing the prompt, bump `PROMPT_VERSION` in
`tracker/judge.py` and run `./run replay` to see which verdicts changed.

---

## Commands

```
run login | collect | replies | suggest | links | amplify | threads
    dedup | judge | extract | digest | review | rate | replay
    accounts | candidates | stats | doctor
```

`./run doctor` when something seems wrong. `./run <cmd> --help` for options.
Most stages have a dry-run mode and print what they *would* do first.

---

## Things that will bite you

**The session expires.** `./run doctor` tells you; `./run login` fixes it.

**X renames GraphQL operations occasionally.** Collection logs every operation
it sees, so the fix is usually adding one string to `TIMELINE_OPS` in
`tracker/parse.py`.

**Some accounts restrict who can view their posts.** Their threads come back as
tombstones. Following them from the burner usually fixes it.

**Container churn breaks Chromium.** If `ERR_NETWORK_CHANGED` appears
repeatedly, something on your machine is creating and destroying network
interfaces — Docker restart loops are the usual cause. Chromium treats each as
a network change and aborts in-flight requests.

---

## Layout

```
tracker/     the pipeline, one module per stage
ui/          desktop app front end (plain HTML, no build step)
spike/       the throwaway proof that GraphQL interception works
schema.sql   the database, heavily commented
SPEC.md      the original design and why each decision was made
seeds.txt    who is tracked — edit this
```

Everything reads from one SQLite file. Swap the collection layer and nothing
downstream notices.

---

## Contributing

The parts most likely to need work from someone other than me:

- **Collection breaks when X changes.** If an operation gets renamed, the fix is
  usually one string in `tracker/parse.py` — `collect` logs every GraphQL
  operation it sees precisely so this stays a one-liner.
- **The lexical + semantic embedding pair is a compromise.** It avoids a 2.5GB
  torch install at some cost in accuracy. `tracker/embed.py` takes any backend
  exposing `encode()`; a real sentence-transformer would drop straight in.
- **Only Linux is tested.** pywebview supports macOS and Windows, and nothing
  else is platform-specific, but nobody has tried it.

Keep the two invariants: **nothing is ever deleted** (dropped posts keep their
reason so filtering stays auditable), and **cheap filters run before expensive
ones** so model calls stay at the narrow end of the funnel.

---

## License

[Apache License 2.0](LICENSE). Use it, fork it, ship it — keep the notice.

The license covers this code. It does not grant you rights to the posts it
collects, and it is not permission to violate X's terms of service; that call is
yours.
