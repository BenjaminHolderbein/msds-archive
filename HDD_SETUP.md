# HDD setup runbook

> **Note — this runbook assumes the cmux reference setup** (a `cmux` browser
> surface for Phase A, an external HDD at `/Volumes/ColdStore`). cmux is **not**
> a hard requirement: only Phase A drives a browser, and it can run on Chrome
> (incl. the Claude Chrome extension), a Playwright/Puppeteer/Selenium driver,
> or manual DevTools extraction instead. If you're not on cmux, read
> [README → *Adapting the browser driver*](README.md#adapting-the-browser-driver-cmux-chrome-or-manual)
> first, then treat the cmux-specific steps below as the pattern to translate.

**Audience:** a Claude Code session opened in `msds-archive/` after the user
plugs in a freshly-arrived 2 TB external HDD. Goal: validate the drive,
format it, format-test it, then start the archive run.

The user can hand you this prompt:

> "The HDD just arrived. Run `HDD_SETUP.md`."

Work top-to-bottom. **Stop and confirm with the user before anything
destructive** (Step 3 erases the drive). Don't skip steps to save time —
each one catches a different way the run can fail.

---

## Step 0. Pre-checks (no destructive ops)

```bash
diskutil list
```

Expect a new external disk (USB connection, ~2 TB). It will show up as
something like `/dev/disk4` or `/dev/disk5`. The number depends on what
else is plugged in.

**Show the `diskutil list` output to the user verbatim and ask:**

> "Confirm which `/dev/diskN` is the new HDD. I see [list candidates with
> size + bus]. Reply with the disk number."

Set `HDD_DISK=/dev/diskN` after the user confirms. **Do not guess.**
Picking wrong = wiping the user's work data.

If `smartctl` isn't installed:
```bash
brew install smartmontools
```

## Step 1. SMART health check

```bash
sudo smartctl -a "$HDD_DISK"
```

Look for:
- **Power_On_Hours** — used drives often have 1k-30k hours; >50k is concerning.
- **Reallocated_Sector_Ct** — should be 0 or very low. Anything >100 is bad.
- **Current_Pending_Sector** — must be 0.
- **Offline_Uncorrectable** — must be 0.
- The bottom-line **"SMART overall-health self-assessment test result"** — must be **PASSED**.

If any of those red flags hit, **stop and tell the user**. Used eBay drives
sometimes ship pre-failed; don't commit the archive to a bad disk.

## Step 2. Quick read benchmark (still non-destructive)

```bash
sudo dd if="$HDD_DISK" of=/dev/null bs=1m count=1024 2>&1
```

Should sustain **>80 MB/s** on a healthy 2 TB SATA drive in a USB 3
enclosure. <40 MB/s suggests USB 2 connection (replug into a different
port) or a flaky enclosure/cable. Surface report the speed to the user.

## Step 3. Format ⚠ destructive ⚠

**Confirm with the user one more time before this step.** Show them the
disk identifier and size. **The decision is already made: APFS
Case-sensitive.** Don't ask the user to pick a filesystem — they've
chosen.

Format with volume label `ColdStore`:

```bash
# APFS Case-sensitive, no encryption (encryption adds CPU overhead during
# the multi-hour write; user can FileVault later if they want).
diskutil eraseDisk "Case-sensitive APFS" ColdStore GPT "$HDD_DISK"
```

### Reading the archive from Windows later

(Don't surface this unless the user asks — it's documented for the future.)
APFS isn't natively readable on Windows, but there are known paths:

1. **WSL2 + USB passthrough + open-source `apfs-fuse`** — free, read-only.
   Requires WSL2 set up and USB passthrough enabled (`usbipd-win`).
2. **Plug into any Mac, copy what's needed off** to a USB stick or over
   the network. The pragmatic option for one-off recovery.
3. **Paragon's APFS for Windows** — paid (~$50), 10-day free trial.
   Read/write.

Linux read-only is straightforward via the open-source `apfs-fuse`
package.

Confirm the format succeeded:
```bash
diskutil info /Volumes/ColdStore | grep -E "File System|Mount Point|Volume Total Space|Read-Only"
```

The mount point should be exactly `/Volumes/ColdStore` and Read-Only should
be `No`.

## Step 4. Sustained write benchmark

Real-world write speed matters more than the read benchmark — the archive
run is write-bound on this drive.

```bash
time dd if=/dev/zero of=/Volumes/ColdStore/_writetest bs=1m count=4096 2>&1
rm /Volumes/ColdStore/_writetest
```

Expect **>60 MB/s sustained** for a healthy spinning HDD. If you get
<30 MB/s, something's wrong (USB 2 fallback, dying drive, bad cable).
The archive run downloads ~950 GB; at 30 MB/s that takes ~9 hours **of
just writing**, on top of the network bottleneck.

## Step 5. Run the pre-flight test suite

```bash
cd /path/to/msds-archive
python3 test_setup.py --output /Volumes/ColdStore/msds-archive/
```

This is the real test that the archive run will work end-to-end. Expect
~15 seconds, all green. Surface any FAIL lines to the user with the
suggested fix.

Common things this catches at this stage:
- `output_volume_real` — drive actually mounted at `/Volumes/ColdStore/` ✓
- `output_writable_and_space` — ≥50 GB free (it'll be ~1.86 TB on a fresh format)
- `disk_budget_vs_estimate` — sampled HEADs across saved manifests project
  ~600 GB needed; 2 TB drive has comfortable margin ✓
- `browser_surface_alive` — cmux browser is logged into Canvas
- `e2e_panopto_roundtrip` — one real Panopto launch end-to-end

If anything fails here, fix it before Step 6. The README's "Hard-won facts"
section explains the specific failure modes.

## Step 6. Kick off the archive run

This is the long one. **Real-world timing (first full run, 2026-06-05):** 19
MSDS courses / 419 sessions / ~550 GB took **~12 hours at a steady ~14 MB/s** —
not the ~7 h a naive 400 Mb/s estimate implies. Throughput is gated by the
network plus per-session ffmpeg audio extraction, not the drive (which benched
at 163 MB/s write). Tell the user the ETA and that it's safe to walk away or
sleep.

**Output goes in a subfolder, `msds-archive/`, on the HDD** — not the volume
root — so the rest of the 2 TB stays free for other use.

**⚠ Scope — read before running.** The default course discovery grabs *every*
enrolled course in `available`/`completed` state, which on this account is
~55 courses: the entire undergrad career, PE/gen-ed classes, and empty admin
shells (Orientation, Workplace Violence training, …) — **not** just the MSDS
program. To restrict to the MSDS program, pass `--course-ids`. Also **exclude
any course you're still mid-term in** (e.g. the current Special Topics course,
MSDS 631) and add it after the term ends. List the candidate courses with:

```bash
TOKEN=$(security find-generic-password -a "$USER" -s canvas-api-token -w)
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://usfca.instructure.com/api/v1/courses?per_page=100&include[]=term&state[]=available&state[]=completed" \
  | python3 -c 'import sys,json; [print(c["id"], c.get("term",{}).get("name"), c["name"]) for c in json.load(sys.stdin) if "name" in c]'
```

**⚠ The run MUST happen inside a live cmux terminal — never detached/headless.**
Phase A drives the Panopto browser through the cmux daemon, and that daemon is
only reachable from a real cmux *surface*. A `nohup` / `setsid` / `Bash
run_in_background` process that outlives its spawning shell **cannot** reach it:
`cmux tree` comes back empty, so `find_canvas_surface()` finds nothing and the
run dies at preflight with "No cmux browser surface found". (We burned three
launches learning this — it is *the* gotcha. See the troubleshooting table.)

**Interactive run** — just run it foreground in the user's own terminal pane:

```bash
cd /path/to/msds-archive
caffeinate -ims python3 -u archive.py --output /Volumes/ColdStore/msds-archive/ \
  --course-ids <ids> 2>&1 | tee /Volumes/ColdStore/msds-archive/_archive_run.log
```

**Unattended / agent-driven overnight run** — spawn a dedicated cmux terminal
pane and drive it, so the run owns a real cmux surface while the agent's own
session stays free to monitor (keep the Mac on power, **lid open** — clamshell
sleep still pauses it):

```bash
# 1. create a terminal pane in the current workspace; note its surface ref
cmux --json new-pane --type terminal --direction down --focus false      # -> surface:N
# 2. type the command into that pane and press Enter
cmux send --surface surface:N 'caffeinate -ims python3 -u archive.py --output /Volumes/ColdStore/msds-archive/ --course-ids <ids> 2>&1 | tee /Volumes/ColdStore/msds-archive/_archive_run.log'
cmux send-key --surface surface:N Enter
```

Then monitor by reading the tee'd `_archive_run.log` (and/or `cmux read-screen
--surface surface:N`) plus per-course `_state.json`. **First check ~60–90 s
post-launch** to confirm preflight passed — it exits fast if the surface isn't
detected. Do **not** use `Bash run_in_background` for the run itself (harness
timeout caps will sever it, and it's detached anyway). The user should see
Phase A scroll through the selected courses (browser-driven, ~5-10 min total),
then Phase B downloading lectures, then Phase C mirroring Canvas.

Monitoring tips (from the first run): a per-course log tail plus an hourly
"did bytes grow?" heartbeat works well. Two filter gotchas — (a) don't put a
bare `401` or `Error` in a log-grep; byte counts like `672,001,401` false-match
and a successful `ok …mp4` line looks like an error. (b) a monitor that runs
`pgrep -f "archive.py …"` matches *its own* command line (which contains that
string) and so never detects the real process exiting — match `python3 -u
archive.py`, or exclude self.

The orchestrator calls `cmux set-progress` at every phase boundary and
on each course during Phase B, so the **cmux workspace progress bar**
shows live overall status (e.g. "Phase B: 12/22 Distributed Data Systems
- 01 (Spring 2026)"). Tell the user that's the easiest way to monitor
without tailing logs.

Don't try to babysit the multi-hour download by polling logs. The user
will check on it themselves via the progress bar; you should be available
for questions if something goes wrong.

## Step 7. After the run completes

```bash
python3 archive.py --output /Volumes/ColdStore/msds-archive/ --verify-only
```

Should exit 0 and produce:
- `/Volumes/ColdStore/msds-archive/INDEX.md` — human-readable course list with counts
- `/Volumes/ColdStore/msds-archive/_archive_report.md` — verification report
- `/Volumes/ColdStore/msds-archive/_archive_report.json` — same, machine-readable

Open `INDEX.md` in the markdown viewer:
```bash
cmux markdown open /Volumes/ColdStore/msds-archive/INDEX.md
```

Spot-check one lecture by playing the audio + screen pair side-by-side
(QuickTime can open both). Confirm captions appear next to mp4s. If the
report shows `courses_with_errors: 0`, the archive is complete.

---

## What can go wrong, and what to do

| symptom | likely cause | fix |
|---|---|---|
| "no cmux browser surface on Canvas/Panopto" in test_setup | logged out | open `cmux browser open https://usfca.instructure.com` and log in via SSO |
| "Canvas rejected token (401)" | Keychain token expired | issue a new token in Canvas Account → Approved Integrations, save with `security add-generic-password -a $USER -s canvas-api-token -w '<token>'` |
| Phase A hangs on one course for >2 min | LTI launch race or surface state | Ctrl-C; re-run phase_a.py with `PHASE_A_DEBUG=1 --course-ids <stuck>` to see where it hung; the orchestrator preserves prior good manifests |
| Phase B `FAIL <name>: HEAD did not return Content-Length` | CloudFront URL rotated | the manifest is stale; re-run Phase A for that course (`python3 phase_a.py --course-ids <id> --output /Volumes/ColdStore/msds-archive/manifests`) then resume Phase B |
| Drive disconnects mid-run | USB power flake | replug, run `archive.py` again — everything is resumable; downloads pick up from `.partial` files |
| `_archive_report.md` shows missing mp4s | per-session ffmpeg failure | check `_state.json` per course for the specific session; usually rerunning resolves it |
| `ls`/writes on `/Volumes/ColdStore` return "Operation not permitted" (EPERM) | macOS TCC: cmux lacks Full Disk Access (persists even with `sudo`) | System Settings → Privacy & Security → Full Disk Access → enable **cmux**, then **quit & reopen cmux** — the grant loads only at app launch |
| `archive.py` exits immediately at preflight with "No cmux browser surface found" but the surface *is* logged in | the run was launched **detached/headless** (`nohup`/`setsid`/`Bash` background) — a process that isn't a live cmux surface can't reach the cmux daemon, so `cmux tree` is empty | launch the run **inside a real cmux terminal pane** (`cmux new-pane --type terminal` then `cmux send`), not detached — see Step 6. Also confirm a browser surface is logged into Canvas/Panopto |
| verify reports "N canvas files failed" and a Phase C re-run doesn't fix them | the referenced files return **HTTP 404 — deleted from Canvas upstream** (dangling links in old page/assignment HTML). Distinct from transient presigned-URL expiry, which a re-run *does* fix | unrecoverable by any means (the files no longer exist). Confirm with a direct `/courses/<cid>/files/<fid>` probe; if 404, accept as upstream rot. On the first run, 7 such files (Distributed Computing, Experiments) were genuinely gone |

The whole pipeline is designed to be re-run-safe. **When in doubt: re-run
the same command.** Idempotency is the whole point.

---

## Run history

- **2026-06-05 — first full run, complete.** 19 MSDS-program courses (Summer
  2025 → Summer 2026; MSDS 631 / current-term Generative AI deliberately
  excluded to add later). Result: **419/419 Panopto sessions**, 396/396
  captions, **519 Canvas files + 164 submissions**, **~552 GB** at
  `/Volumes/ColdStore/msds-archive/`. ~12 h wall-clock at ~14 MB/s. Ran inside a
  cmux terminal pane (`surface:20`) after detached/headless launches failed at
  preflight. Only residue: **7 Canvas files HTTP 404 (deleted upstream)** in
  Distributed Computing (3) and Experiments in Data Science (4) — unrecoverable.
  Practicum I has no Panopto recordings (project course) — expected.
- **2026-06-06 — post-processing.** (1) **Ghost-session cleanup:** detected
  canceled-but-recorded sessions via near-empty transcripts (real lecture ≈110
  wpm / ~13k words; ghosts <5 wpm / <100 words — see `_ghost_sessions.md`).
  Removed **30** (19 empty-room + 11 canceled/test), keeping 11 borderline that
  reviewed as real (slide-deck video + topical transcript fragments).
  Manifests/rollup pruned to match: **419 → 389 sessions**, ~40 GB reclaimed.
  (2) **Combined video:** ran `combine_av.sh` to mux each `screen.mp4`+`audio.m4a`
  into a single watchable `combined.mp4` (lossless), keeping `audio.m4a`, dropping
  `screen.mp4`. Final: 376 `combined.mp4` + 13 audio-only sessions, verified
  389/389 present. Note: do NOT mux deletion candidates first (wasted work) —
  exclude them via `/tmp/ghost_dirs.txt`.
- **2026-07-15 — MSDS 631 added (term ended), archive now complete.** Ran
  `archive.py --course-ids 1633557 --max-mbps 200` (new bandwidth-cap flag so
  the network stayed usable). 19 sessions / 37.4 h downloaded (19/19 mp4s +
  captions, 26 Canvas files + 14 submissions); ghost check removed 2
  empty-room recordings (kept 1 weak-mic borderline) → **17 lectures,
  ~26 GB**; `combine_av.sh` muxed all 17. Full-archive verify: **20 courses,
  406 sessions, ~570 GB**; only residue remains the 7 upstream-404 Canvas
  files from the first run. Bandwidth-cap gotcha discovered: ffmpeg's
  `-readrate` over-throttles Panopto HLS (~1.5x realtime regardless of the
  requested multiple), and unthrottled HLS blows past any cap — so
  `download.py` now fetches the variant's single byterange `fragmented.mp4`
  with `curl --limit-rate` and remuxes locally.
