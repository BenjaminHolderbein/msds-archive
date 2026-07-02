# msds-archive

Pipeline for archiving a year's worth of USF MSDS course material before losing
platform access — Panopto lecture recordings *and* everything Canvas exposes
about each course (assignments, pages, modules, syllabus, announcements,
discussions, files, my submitted work, instructor feedback / rubric scores).
Output: a local mirror that future tools can transcribe, embed, and query.

> **Status:** Complete (run 2026-06-05, post-processed 2026-06-06) — 19
> MSDS-program courses, **389 Panopto lectures** as `combined.mp4` (video+audio,
> after removing 30 canceled/empty "ghost" recordings) + captions, 519 Canvas
> files + 164 submissions, at `/Volumes/ColdStore/msds-archive/`. (7 Canvas files
> were HTTP 404 / deleted upstream; any still-in-progress course is best
> excluded and re-run once its term ends — see `HDD_SETUP.md` → Scope.)
> Details in `HDD_SETUP.md` → Run history; ghost list in `_ghost_sessions.md`.

## Requirements

- **macOS** (uses the Keychain via `security`, and the cmux browser for Phase A).
- **Python 3.9+** — standard library only, no `pip install` needed.
- **CLI tools on `PATH`:** `curl` and `ffmpeg` (for muxing). Phase A also needs
  a browser that can run JS in an authenticated Canvas/Panopto session — the
  reference setup uses [`cmux`](https://cmux.app), but that's **not a hard
  requirement**; see [Adapting the browser driver](#adapting-the-browser-driver-cmux-chrome-or-manual).
- A **Canvas API token** stored in the macOS Keychain (see [Canvas auth](#canvas-auth)).
- Somewhere to put ~50 GB–2 TB depending on `--mode` (an external HDD in the
  original run).

> **Note:** This archives *your own* enrolled coursework and submitted work
> using your own Canvas token — it does not bypass any access control. Only
> download material you're entitled to, and mind your institution's acceptable
> use policy. The archived output (lectures, course files) is not redistributed
> by this repo and stays local.

## One-shot use

```bash
# 1. Smoke test in ~15s — catches the failures that would otherwise
#    surface mid-run (token expired, HDD unmounted, surface logged out,
#    Canvas scopes missing, disk budget too tight, …).
python3 test_setup.py --output /Volumes/ColdStore/msds-archive/

# 2. Run the archive (hours; resumable).
python3 archive.py --output /Volumes/ColdStore/msds-archive/
```

If `test_setup.py` exits non-zero, fix the reported issues before starting
the long run.

That's it. The orchestrator preflights everything (tools installed, volume
mounted, free space, Canvas token works, cmux browser surface is logged in),
discovers your courses, runs Phase A → Phase B → Phase C across all of them,
verifies the result, and writes `INDEX.md` + `_archive_report.{md,json}` at
the volume root.

Useful flags:

- `--course-ids 1631791,1631781` — restrict to a subset.
- `--mode {audio+screen,podcast,all}` — per-session artifacts (default
  `audio+screen`; see [Per-session modes](#per-session-modes) below).
- `--skip-panopto` / `--skip-canvas` — exercise just one half of the pipeline.
- `--verify-only` — re-run only the verifier; report what's there vs expected.
- `--min-free-gb N` — refuse to start if <N GB free (default 50).
- `--workers N` — parallel file downloads per course (default 4).

Re-running is safe: existing files are skipped by size+`updated_at`,
manifests overwrite atomically, and a previously-good manifest is **preserved**
on per-course failure (you don't lose yesterday's data because today's run
hit a transient issue on that course).

The three underlying scripts (`phase_a.py`, `download.py`, `phase_c.py`) are
also runnable directly — `archive.py` just composes them.

## Pipeline phases

- **Phase A — Panopto manifest extraction (browser-driven).** For each Canvas
  course, drive the cmux browser through the Panopto LTI launch, capture every
  session's stream URL, secondary streams, and **inline SRT captions**, write
  `manifests/<course-slug>.json`. Fast (seconds per course), needs an
  authenticated cmux browser surface.
- **Phase B — Panopto download (shell-driven).** Read manifests, `curl` the
  CloudFront MP4s in parallel with resume; write captions next to the mp4.
  Atomic `.partial` swap; refuses to "skip" files whose Content-Length can't
  be verified.
- **Phase C — Canvas content mirror (API-driven).** Hit the Canvas REST API to
  pull course meta, syllabus, modules, assignments, pages, quizzes,
  discussions, announcements, rubrics, groups, every referenced file, my
  submissions *with submission_comments + rubric_assessment* (instructor
  feedback), and refresh expired presigned URLs on transient failure.
  Aborts immediately on the first 401 (token expired) instead of burning
  N×retries × M×courses of noise.

## Files

- `archive.py` — Top-level orchestrator. **Default entry point.**
- `test_setup.py` — Pre-flight smoke suite (run before `archive.py`).
- `HDD_SETUP.md` — Step-by-step runbook for first-time HDD setup (format,
  validate, kick off the run). Read this on HDD day.
- `phase_a.py` — Phase A driver (browser).
- `extract_manifest.js` — Snippet `phase_a.py` evals inside the Panopto page.
- `download.py` — Phase B downloader (resumable, parallel, captions).
- `phase_c.py` — Phase C Canvas content mirror.
- `_smoketest/` — Earlier proofs (one full session downloaded).

## Output layout (under `<archive-root>/`)

```
INDEX.md                         # human-friendly course index
_archive_report.{md,json}        # verifier output
manifests/
  _courses.json                  # canonical course list (id + name)
  _rollup.json                   # per-course Panopto summary
  <id>-<slug>.json               # per-course Panopto manifest
<id>-<slug>/                     # one dir per course
  course.json                    # Canvas course meta + syllabus body
  syllabus.html                  # extracted syllabus
  tabs.json                      # which content surfaces are enabled
  folders.json                   # folder hierarchy metadata
  modules.json                   # full module structure with items
  assignments/<id>__<slug>.{json,html}
                                 # full assignment record + extracted HTML body
  assignments/<id>__<slug>__submission.json
                                 # my submission incl. submission_comments + rubric
  pages/<slug>.{json,html}
  quizzes/<id>__<slug>.json (+ __questions.json when accessible)
  discussions/<id>__<slug>.json + __view.json
  announcements/<id>__<slug>.json
  rubrics.json
  groups.json
  files/
    by_id/<id>__<filename>       # flat by-file-id; deduped
    files_index.json             # id → {filename, folder_path, size, ...}
  submissions/<assignment_id>__<slug>__<filename>
  panopto/                       # Phase B output
    <session_id>__<name>/
      combined.mp4               # post-processing: screen video + audio muxed into one watchable file
      audio.m4a                  # extracted from podcast; kept for ASR/transcription
      screen.mp4                 # raw screen, 1280x800, no audio — present pre-combine; dropped after combine
      podcast.mp4                # `--mode podcast` or `--mode all`
      stream_<n>.mp4             # `--mode all` only (cam + screen, raw)
      captions_<lang>.srt        # one per available language
      meta.json                  # full manifest entry for this session
  _state.json                    # last_run, counts, failures
```

## Hard-won facts (don't relearn these)

1. **The cmux browser is WKWebView, not Chromium.** Most extension/CDP tricks
   don't apply. JS injection via `cmux browser <surface> eval "<js>"` does work.
2. **The Panopto LTI iframe fails to sign in inside cmux** ("We were unable to
   sign… Click here to sign in"). The third-party cookie handoff doesn't
   complete in the embedded iframe. **Workaround:** find the LTI launch form
   inside the Canvas page (`form[action*="panopto.com"]`), set `form.target =
   "_top"`, then `form.submit()`. The whole tab navigates to Panopto, SSO
   completes, and the URL hash carries the folder ID. `phase_a.py` already does
   this.
3. **Don't `navigate about:blank` between courses on this surface — it can hang
   WKWebView.** `phase_a.py` polls `location.href` instead.
4. **CloudFront MP4s require zero auth.** Once you have the URL from
   `DeliveryInfo.aspx`, `curl -C -` it without cookies. No HLS keys, no signed
   URLs, no `.panobf` (USF serves plain `.mp4` for the podcast stream + HLS
   manifests for secondary cam/slides).
5. **USF's Panopto LTI tool ID is `119349`** across all courses (it's installed
   at account scope). Used as the literal `external_tools/119349` segment.
6. **`Data.svc/GetSessions` returns ALL sessions in one shot** (just pass
   `maxResults: 1000`). Don't fight the UI's 25-per-page pagination.
7. **Run `DeliveryInfo.aspx` calls in parallel via `Promise.all`** inside the
   eval — sequential `await`s blow past cmux's eval timeout on courses with
   many sessions.
8. **`cmux ... eval` occasionally fails the first time with "Timed out waiting
   for JavaScript result"** right after a navigation, then succeeds on retry.
   `phase_a.py`'s `browser_eval` already retries twice with backoff.

## macOS & external-drive setup (hard-won)

1. **cmux.app must have Full Disk Access** (System Settings → Privacy &
   Security → Full Disk Access → cmux) to read or write an external volume
   like `/Volumes/ColdStore`. Without it, even `ls` on the volume returns
   `Operation not permitted` (EPERM) — this is a macOS TCC gate, *not* a
   drive fault, and it persists even with `sudo`. **Full Disk Access is read
   only at app launch, so quit and reopen cmux after granting it** — every
   shell command inherits the running cmux process's permission set.
2. **SMART health checks don't work over USB on macOS.** The USB–SATA
   bridge doesn't pass them through (`smartctl` reports "Operation not
   supported by device" / "Not a device of type 'scsi'", regardless of
   `-d sat`/`usbjmicron`/etc.). Substitute a raw read scan —
   `sudo dd if=/dev/diskN of=/dev/null bs=1m` across the start, middle, and
   end of the disk — and watch for I/O errors as the health signal.
3. **Run `archive.py` from inside a live cmux terminal — never detached or
   headless.** Phase A drives the Panopto browser via the cmux daemon, which is
   only reachable from a real cmux *surface*. A `nohup` / `setsid` / `Bash
   run_in_background` process that outlives its spawning shell can't reach it
   (`cmux tree` returns empty), so `find_canvas_surface()` finds nothing and the
   run dies at preflight with "No cmux browser surface found" — even with a
   browser surface logged in. For unattended/overnight runs, spawn a dedicated
   pane (`cmux new-pane --type terminal`) and drive it with `cmux send` /
   `cmux send-key`; monitor via the tee'd log. See `HDD_SETUP.md` Step 6.

## Canvas auth

Read the token from macOS Keychain at runtime — **never** print, log, or commit it.

```bash
export CANVAS_API_URL="https://usfca.instructure.com"
export CANVAS_API_TOKEN=$(security find-generic-password -a "$USER" -s canvas-api-token -w)
```

`phase_a.py` does this automatically via `get_token()` if `CANVAS_API_TOKEN` is
unset.

## Finding the cmux browser surface

The cmux browser surface must be logged into Canvas (`usfca.instructure.com`).
If a fresh cmux instance has no browser surface, open one once:
`cmux --json browser open https://usfca.instructure.com` and log in by hand
(SSO/2FA). It persists across runs.

To find an existing logged-in surface:

```bash
# Match either the Canvas or Panopto host (SSO is shared, so a surface parked
# on Panopto is fine — phase_a.py will navigate it to Canvas as needed).
cmux tree --all | grep -E "surface.*browser.*(instructure\.com|panopto\.com)"
```

The ref looks like `surface:1` or `surface:13`. Pass it to `phase_a.py` as
`--surface surface:N`.

## Adapting the browser driver (cmux, Chrome, or manual)

**Only Phase A touches a browser at all.** Phase B is plain `curl` against
CloudFront and Phase C is the Canvas REST API with your token — neither knows
what cmux is. So "de-cmux-ing" this project means replacing exactly one thin
shim in `phase_a.py`. That file talks to the browser through four small
primitives, all near the top:

| Primitive | What it must do |
|---|---|
| `browser_eval(surface, js)` | Run `js` in the authenticated page, return its (JSON-stringified) result. **The only one that matters** — everything else is convenience. |
| `cmux(surface, 'navigate', url)` | Point the tab at `url`. |
| `cmux(surface, 'wait', '--load-state', 'complete', …)` | Block until the page finishes loading. |
| `find_canvas_surface()` | Locate/return a tab already logged into Canvas or Panopto. |

Point those four at any driver that can evaluate JS inside a browser session
that's already signed into your school's Canvas + Panopto SSO. Options, roughly
in order of effort:

- **Claude's Chrome extension / "Claude in Chrome."** A natural fit: it exposes
  a `navigate` and a JavaScript-evaluation tool (`javascript_tool`) against your
  real logged-in Chrome tab — a near drop-in for `navigate` + `browser_eval`.
  You can either rewrite the four primitives to call those MCP tools, or just
  hand `extract_manifest.js` (plus the per-course launch steps below) to Claude
  and let it drive the extraction interactively.
- **A CDP driver — Playwright / Puppeteer / Selenium.** Launch (or attach to)
  Chrome with your normal profile so the Canvas/Panopto cookies are present,
  then map `browser_eval` → `page.evaluate()` and `navigate` → `page.goto()`.
  Headed is fine; SSO/2FA is a one-time manual login in that profile.
- **Fully manual (no driver).** Open the course's Panopto folder in a normal
  browser, paste the body of `extract_manifest.js` into DevTools → Console, and
  save the printed JSON as `manifests/<course-slug>.json`. Tedious across 20
  courses but requires zero tooling, and Phases B/C then run untouched.

**Chrome vs. cmux caveat:** several "Hard-won facts" above are WKWebView-specific
(cmux's engine). In real Chrome the LTI-iframe third-party-cookie failure (fact
#2) and the `about:blank` hang (fact #3) may not occur — but the fix for #2
(`form.target = "_top"; form.submit()`) is harmless if applied anyway, and it's
the mechanism `launch_panopto()` already uses, so leave it in.

## Running an individual phase

The orchestrator delegates to three scripts; each is fine to run on its own.

```bash
# Phase A only (just refresh manifests):
python3 phase_a.py --output ./manifests
# Add PHASE_A_DEBUG=1 for per-step traces; --course-ids 1,2 for a subset.

# Phase B only (download from an existing manifest):
python3 download.py ./manifests/1631791-distributed-data-systems-01-spring-2026.json \
  /Volumes/ColdStore/msds-archive/1631791-distributed-data-systems-01-spring-2026/panopto/ \
  --include-streams

# Phase C only (Canvas mirror):
python3 phase_c.py --output /Volumes/ColdStore/msds-archive/ --course-ids 1631791
```

## Phase C content surfaces captured

Per course, Phase C pulls: course meta + syllabus body, tabs, folders metadata,
modules (with items), assignments (full HTML + my submissions including
**submission_comments + rubric_assessment** for instructor feedback), pages,
quizzes (incl. questions when accessible), discussions (full thread view),
announcements, rubrics, groups metadata, every referenced file
(`/courses/<id>/files/<id>` discovered via module items + HTML body refs),
and my submission attachments.

Skipped on purpose: people listings (privacy), inbox conversations (not course
content), live external-tool persistent state (Poll Everywhere, Lucid).

### Phase C hard-won facts

1. **`/courses/<id>/files` is locked for students** at USF (401). Same for
   `/folders/<id>/files` and `/courses/<id>/folders/root`. The
   `/courses/<id>/folders` *listing* IS accessible — captured for path
   metadata — and file IDs are discovered from inline references in HTML
   bodies and module items, then fetched individually via
   `/courses/<cid>/files/<file_id>`.
2. **Pages tab is sometimes disabled per course** → `/pages` returns 404.
   Logged, downgraded to "benign" by the verifier.
3. **Submission attachment URLs are presigned and short-lived.** A long run
   can outlive them; `phase_c.py` re-fetches submission JSON on first failure
   to grab a fresh URL before giving up.
4. **First Canvas 401 aborts the whole run** with a clear "refresh the
   Keychain entry" message — no point burning N×retries × M×courses on a dead
   token.

## Per-session modes

Panopto records each session as a *podcast* (a 720p composited mp4 with cam
overlay + screen + audio in one file) plus a set of *streams* (the original
1080p cam-with-audio recording, and a screen-only capture). The screen
stream has no audio track — only the cam stream and the podcast carry it.

| `--mode` | what's kept per session | bytes/hr | full year (~860 hr) |
|---|---|---|---|
| **`audio+screen`** *(default)* | extract audio.m4a from podcast → drop podcast; keep the screen stream | ~675 MB | **~580 GB** |
| `podcast` | composite `podcast.mp4` only (watchable as-is) | ~470 MB | ~400 GB |
| `all` | podcast + every secondary stream (cam + screen) | ~2.4 GB | ~2.0 TB |

Why `audio+screen` is the default for an AI-archive use case:
- ASR works on `audio.m4a` — no cam pixels needed for transcripts.
- The screen stream is higher resolution (1280×800) than the podcast's
  composite (720p) and has no cam-overlay covering slide content — better
  for OCR / multimodal indexing.
- Cam video (talking head) is the highest-bitrate, lowest-information stream
  for downstream processing — dropped entirely.

Canvas content adds ~5–10 GB across the year regardless of mode. Captions
add <50 MB total.

A 1 TB HDD is comfortable for the default mode; 2 TB gives headroom if you
later switch to `--mode all`.

### Combining into one watchable file (post-processing)

`audio+screen` leaves `audio.m4a` + silent `screen.mp4` *separate* (ideal for
ASR/OCR, not for watching). To get a single watchable `combined.mp4` per
session, mux them losslessly (no re-encode, seconds per file):

```bash
ffmpeg -nostdin -y -i screen.mp4 -i audio.m4a -c copy -map 0:v:0 -map 1:a:0 combined.mp4
```

`combine_av.sh` batches this across the whole archive (verifies both streams
land before dropping the standalone `screen.mp4`, keeps `audio.m4a` for ASR;
resumable; skips a session-dir exclusion list in `/tmp/ghost_dirs.txt`).
**Gotcha:** always pass `ffmpeg -nostdin` inside a read-loop — otherwise ffmpeg
consumes the loop's stdin (the file list) and corrupts iteration.

## Prior art

The Panopto manifest-extraction approach (`extract_manifest.js`) was informed by
[**Panopto-Video-DL**](https://github.com/Panopto-Video-DL/Panopto-Video-DL-browser)
(MIT), a browser userscript for downloading individual Panopto videos. This
project doesn't vendor or depend on it — it re-implements the stream discovery
for batch, headless-ish use inside the cmux browser — but that userscript is a
good reference if you want to grab a single lecture by hand.

## License

MIT — see [`LICENSE`](LICENSE).
