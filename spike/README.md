# Milestone 1 — scraper spike

This directory answers one question and then gets deleted:

> **Can we reliably pull structured post data out of X's internal GraphQL responses,
> without parsing the DOM?**

Everything else in [`SPEC.md`](../SPEC.md) reads from SQLite and doesn't care where the
data came from — so if the answer is no, only this directory gets rewritten.

## Running it

Two steps. The first is one-time.

```bash
# 1. Create/log into the burner account. Opens a real browser window.
#    Do NOT use your personal account.
./spike/dump_timeline.py --login

# 2. Point it at a timeline and capture.
./spike/dump_timeline.py --url https://x.com/i/lists/<LIST_ID> --verbose
```

Before step 2 you need a timeline URL. Either:

- **Preferred** — make a private List, add the accounts you want to track, and use
  `https://x.com/i/lists/<id>`. This is the real design: every tracked account in a
  single chronological page load.
- **Quick check** — any profile works for proving interception:
  `https://x.com/karpathy`. The script handles the `UserTweets` endpoint too.

## What success looks like

```
========================================================================
GRAPHQL OPERATIONS SEEN
========================================================================
   3x  ListLatestTweetsTimeline <-- timeline op
   1x  UserByScreenName
   ...

========================================================================
PARSED 87 UNIQUE POSTS from 4 captured responses
========================================================================

[---] 1952... @karpathy  Wed Aug 05 14:22:33 +0000 2026
      the thing about RL fine-tuning that nobody mentions is…
```

Real post IDs, real handles, real timestamps, non-empty text. That's the gate passed —
proceed to milestone 2 (SQLite persistence).

**Failure modes and what they mean:**

| Symptom | Meaning |
|---|---|
| `redirected to login` | Session expired. Re-run `--login`. |
| Operations listed, but none marked `<-- timeline op` | X renamed the operation. Add the new name to `TIMELINE_OPS` — the fix is one line, which is why the script prints every operation it sees. |
| Timeline op captured, but `PARSED 0` | Payload shape changed. The raw JSON is in `out/<timestamp>/` — inspect and adjust `parse_tweet()`. |
| No operations at all | Interception itself failed. This is the one that invalidates the approach. |

## Output

`out/<UTC timestamp>/` per run:

- `NNN-<OperationName>.json` — every intercepted GraphQL response, unmodified
- `_parsed.json` — the flattened posts

Raw bodies are kept deliberately: when parsing breaks later, you can fix the parser and
replay against saved payloads instead of re-scraping. Both the profile and this output
directory are gitignored — the profile holds live session cookies.

## Notes on the parser

It does **not** hardcode the `instructions[]` path. It walks the entire response
collecting anything shaped like a tweet node, because that path differs between list,
profile, and home timelines and X reshuffles it periodically.

One known side effect: retweeted originals are nested inside the retweeting post, so a
retweet yields two entries — the retweet (flagged `R`) and the original. Harmless for the
spike, and Stage 1 drops bare retweets anyway. Worth revisiting at milestone 2.

Parser behavior is covered by an offline test against a synthetic payload — new-style vs.
legacy `screen_name` placement, `note_tweet` long posts beating truncated `full_text`,
reply/quote/retweet flagging, and URL expansion.
