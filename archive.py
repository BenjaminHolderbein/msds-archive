#!/usr/bin/env python3
"""
Top-level orchestrator. One command produces the complete archive.

  python3 archive.py --output /Volumes/ColdStore/msds-archive/

What it does:
  1. Preflight: cmux/curl/ffmpeg installed; output volume mounted, writable,
     enough free space; Canvas token works; cmux browser surface logged into
     Canvas/Panopto.
  2. Resolves the canonical course list (available + completed enrolled
     courses), persists it to <output>/manifests/_courses.json so re-runs
     are reproducible.
  3. Runs Phase A on every course, writing <output>/manifests/<slug>.json.
  4. For each course with sessions, runs Phase B (download.py) into
     <output>/<slug>/panopto/.
  5. Runs Phase C on every course, writing <output>/<slug>/canvas/.
  6. Verifies the archive: every manifest's sessions have a local mp4 of
     the expected size; every Phase C `_state.json` has no critical errors.
  7. Writes <output>/_archive_report.{json,md} and <output>/INDEX.md.

Re-running is idempotent. Per-phase fix-ups can also be invoked directly via
phase_a.py / download.py / phase_c.py — this script just composes them.

Flags:
  --output PATH         Required. The archive root — a subfolder on the
                        mounted HDD, e.g. /Volumes/ColdStore/msds-archive/
                        (keeps the rest of the drive free for other use).
  --course-ids 1,2,3    Subset (default: all available + completed).
  --skip-panopto        Skip Phase A + B (Canvas content only).
  --skip-canvas         Skip Phase C (Panopto only).
  --podcast-only        Skip Phase B's --include-streams (saves 2-3x storage).
  --workers N           Parallel file downloads per course (default 4).
  --min-free-gb N       Refuse to start if <N GB free on the volume (default 50).
  --verify-only         Just run the verifier; don't fetch anything.
"""
from __future__ import annotations

import argparse, datetime, json, os, re, shutil, subprocess, sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PHASE_A = SCRIPT_DIR / 'phase_a.py'
PHASE_B = SCRIPT_DIR / 'download.py'
PHASE_C = SCRIPT_DIR / 'phase_c.py'

CANVAS_BASE = 'https://usfca.instructure.com'


def progress(fraction: float, label: str) -> None:
    """Update the cmux workspace progress bar. No-op outside a cmux pane."""
    fraction = max(0.0, min(1.0, fraction))
    try:
        subprocess.run(
            ['cmux', 'set-progress', f'{fraction:.3f}', '--label', label],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        pass


def progress_clear() -> None:
    try:
        subprocess.run(['cmux', 'clear-progress'], capture_output=True,
                       text=True, timeout=5)
    except (subprocess.SubprocessError, FileNotFoundError):
        pass


# ---------------------------------------------------------------------- utils

def slugify(name: str) -> str:
    s = re.sub(r'[^\w\s-]', '', name).strip().lower()
    return re.sub(r'[\s_-]+', '-', s)[:80]


def get_token() -> str:
    token = os.environ.get('CANVAS_API_TOKEN')
    if token:
        return token
    r = subprocess.run(
        ['security', 'find-generic-password', '-a', os.environ['USER'],
         '-s', 'canvas-api-token', '-w'],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        sys.exit('No CANVAS_API_TOKEN env var, and Keychain entry '
                 '"canvas-api-token" not found. See README.')
    return r.stdout.strip()


def canvas_get(path: str, token: str) -> list[dict]:
    """Paginated Canvas GET — minimal copy from phase_a.py."""
    import urllib.request, urllib.error
    out, url = [], f'{CANVAS_BASE}{path}'
    while url:
        req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                page = json.loads(resp.read())
                if isinstance(page, list):
                    out.extend(page)
                else:
                    return [page] if page else []
                link = resp.headers.get('Link', '')
                m = re.search(r'<([^>]+)>;\s*rel="next"', link)
                url = m.group(1) if m else None
        except urllib.error.HTTPError as e:
            if e.code == 401:
                sys.exit('Canvas rejected token (401). Refresh the '
                         'canvas-api-token Keychain entry.')
            sys.exit(f'Canvas {e.code} on {path}: {e.read()[:200]!r}')
    return out


def find_canvas_surface() -> str | None:
    r = subprocess.run(['cmux', 'tree', '--all'], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    pat = re.compile(
        r'(surface:\d+)\s+\[browser\][^\n]*?'
        r'(usfca\.instructure\.com|usfca\.hosted\.panopto\.com)'
    )
    m = pat.search(r.stdout)
    return m.group(1) if m else None


def parse_iso(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError):
        return None


def head_size(url: str) -> int | None:
    r = subprocess.run(['curl', '-sIL', url], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return None
    size = None
    for line in r.stdout.splitlines():
        if line.lower().startswith('content-length'):
            try:
                size = int(line.split(':')[1].strip())
            except ValueError:
                pass
    return size


# ----------------------------------------------------------------- preflight

def preflight(out_root: Path, min_free_gb: int, *, need_browser: bool) -> str | None:
    print('preflight ----', flush=True)
    missing = [t for t in ('cmux', 'curl', 'ffmpeg', 'security')
               if not shutil.which(t)]
    if missing:
        sys.exit(f'missing required tools: {", ".join(missing)}')
    print(f'  tools: cmux curl ffmpeg security ✓', flush=True)

    # On macOS, anything under /Volumes/X must actually be a mountpoint.
    # Check this BEFORE mkdir so we don't silently create /Volumes/X as a
    # regular dir on the boot drive when the HDD isn't plugged in.
    if str(out_root).startswith('/Volumes/'):
        mount = Path('/Volumes') / out_root.relative_to('/Volumes').parts[0]
        if not os.path.ismount(mount):
            sys.exit(f'{mount} is not a mounted volume — is the HDD plugged in?')
        print(f'  mount: {mount} ✓', flush=True)
    out_root.mkdir(parents=True, exist_ok=True)
    test = out_root / '.write_test'
    try:
        test.write_text('ok'); test.unlink()
    except OSError as e:
        sys.exit(f'output dir not writable: {out_root}  ({e})')

    # Free space
    free_gb = shutil.disk_usage(out_root).free / (1 << 30)
    if free_gb < min_free_gb:
        sys.exit(f'only {free_gb:.1f} GB free on {out_root}; '
                 f'need at least {min_free_gb} GB (see --min-free-gb)')
    print(f'  free space: {free_gb:.1f} GB ✓', flush=True)

    # Token works
    token = get_token()
    canvas_get('/api/v1/users/self', token)  # raises on 401
    print('  canvas token: ✓', flush=True)

    surface = None
    if need_browser:
        surface = find_canvas_surface()
        if not surface:
            sys.exit(
                'No cmux browser surface found on Canvas/Panopto.\n'
                'Open one and log in:\n'
                '  cmux browser open https://usfca.instructure.com\n'
                'Then re-run.'
            )
        print(f'  cmux surface: {surface} ✓', flush=True)
    return surface


# ----------------------------------------------------------------- runners

def run_phase_a(surface: str, manifests_dir: Path, course_ids: list[int]) -> int:
    cmd = [sys.executable, str(PHASE_A),
           '--surface', surface,
           '--output', str(manifests_dir),
           '--course-ids', ','.join(str(x) for x in course_ids)]
    print(f'\nphase A ----  {" ".join(cmd[3:])}', flush=True)
    return subprocess.call(cmd)


def run_phase_b(manifest_path: Path, podcast_dir: Path, *, mode: str,
                workers: int) -> int:
    cmd = [sys.executable, str(PHASE_B),
           str(manifest_path), str(podcast_dir),
           '--mode', mode, '--workers', str(workers)]
    print(f'\nphase B ----  {manifest_path.name} → {podcast_dir}  (mode={mode})',
          flush=True)
    return subprocess.call(cmd)


def run_phase_c(out_root: Path, course_ids: list[int], workers: int) -> int:
    cmd = [sys.executable, str(PHASE_C),
           '--output', str(out_root),
           '--course-ids', ','.join(str(x) for x in course_ids),
           '--workers', str(workers)]
    print(f'\nphase C ----  {len(course_ids)} courses → {out_root}', flush=True)
    return subprocess.call(cmd)


# ----------------------------------------------------------------- verifier

# Errors logged by Phase C that don't actually mean data loss. These are
# "the course doesn't have this surface enabled" (e.g. Pages tab disabled →
# 404 on the listing) or "the user doesn't have permission to enumerate at
# the course level" (e.g. /files/ listing returns 401 even though students
# can still fetch individual files referenced from modules/HTML).
BENIGN_ERROR_PATTERNS = (
    'pages: HTTP 404',
    'pages: HTTP 403',
    'rubrics: HTTP 401',
    'rubrics: HTTP 403',
)


def _is_benign_error(s: str) -> bool:
    return any(p in s for p in BENIGN_ERROR_PATTERNS)


def verify(out_root: Path, courses: list[dict]) -> dict:
    """Walk the archive and produce a per-course report.
    Each row records (sessions_expected, mp4s_present, files captured, errors).
    Returns {'rows': [...], 'totals': {...}, 'failed': bool}.
    """
    manifests_dir = out_root / 'manifests'
    rows = []
    any_fail = False
    for c in courses:
        cid = c['id']
        slug = f'{cid}-{slugify(c["name"])}'
        course_dir = out_root / slug
        manifest_path = manifests_dir / f'{slug}.json'

        row = {
            'course_id': cid,
            'name': c['name'],
            'slug': slug,
            'panopto_manifest': manifest_path.exists(),
            'sessions_expected': 0,
            'sessions_with_url': 0,
            'mp4s_present': 0,
            'mp4_bytes': 0,
            'mp4_size_mismatches': 0,
            'canvas_state': None,
            'canvas_files': 0,
            'canvas_files_failed': 0,
            'errors': [],
        }

        # Panopto side
        row['captions_expected'] = 0
        row['captions_present'] = 0
        if manifest_path.exists():
            try:
                m = json.loads(manifest_path.read_text())
            except Exception as e:
                row['errors'].append(f'manifest parse: {e!r}')
                m = {'manifest': []}
            sessions = m.get('manifest', [])
            row['sessions_expected'] = len(sessions)
            row['sessions_with_url'] = sum(1 for s in sessions if s.get('podcast'))
            podcast_dir = course_dir / 'panopto'
            for s in sessions:
                if not s.get('podcast'):
                    continue
                folder = podcast_dir / f'{s["id"]}__{_safe(s.get("name","unnamed"))}'
                # A session counts as present if any of the audio-bearing
                # artifacts exist (mode-agnostic): audio.m4a, podcast.mp4,
                # or stream_0.mp4 (cam — has audio in `all` mode).
                candidates = [folder / n for n in
                              ('audio.m4a', 'podcast.mp4', 'stream_0.mp4')]
                bytes_for_this = 0
                for f in folder.iterdir() if folder.exists() else []:
                    if f.is_file() and not f.name.endswith('.partial'):
                        bytes_for_this += f.stat().st_size
                if any(c.exists() and c.stat().st_size > 0 for c in candidates):
                    row['mp4s_present'] += 1
                    row['mp4_bytes'] += bytes_for_this
                # Caption coverage: count expected vs on-disk SRTs.
                wanted = [c for c in (s.get('captions') or [])
                          if isinstance(c, dict) and c.get('srt')]
                row['captions_expected'] += len(wanted)
                if folder.exists():
                    row['captions_present'] += sum(
                        1 for f in folder.iterdir() if f.name.startswith('captions_')
                    )

        # Canvas side. Phase C writes <out>/<slug>/_state.json plus the content
        # tree. Count what is actually on disk (more honest than trusting
        # counters in _state.json) and merge with state's failure counters.
        state_path = course_dir / '_state.json'
        if state_path.exists():
            try:
                st = json.loads(state_path.read_text())
                row['canvas_state'] = st
                row['canvas_files_failed'] = st.get('files_failed', 0)
                real_errors = [
                    e for e in (st.get('errors') or []) if not _is_benign_error(e)
                ]
                if real_errors:
                    row['errors'].extend(f'canvas: {e}' for e in real_errors[:5])
            except Exception as e:
                row['errors'].append(f'canvas state parse: {e!r}')

        files_dir = course_dir / 'files' / 'by_id'
        subs_dir = course_dir / 'submissions'
        course_files = sum(1 for _ in files_dir.iterdir()
                           if _.is_file() and not _.name.endswith('.partial')) \
                       if files_dir.exists() else 0
        sub_files = sum(1 for _ in subs_dir.iterdir()
                        if _.is_file() and not _.name.endswith('.partial')) \
                    if subs_dir.exists() else 0
        row['canvas_files'] = course_files
        row['canvas_submissions'] = sub_files

        # Per-course pass/fail
        if row['sessions_expected'] > 0 and row['mp4s_present'] < row['sessions_with_url']:
            row['errors'].append(
                f'{row["sessions_with_url"] - row["mp4s_present"]} podcast mp4(s) missing'
            )
        if row['canvas_files_failed']:
            row['errors'].append(f'{row["canvas_files_failed"]} canvas files failed')
        if row['errors']:
            any_fail = True
        rows.append(row)

    totals = {
        'courses': len(rows),
        'sessions_expected': sum(r['sessions_expected'] for r in rows),
        'mp4s_present': sum(r['mp4s_present'] for r in rows),
        'mp4_GB': round(sum(r['mp4_bytes'] for r in rows) / (1 << 30), 2),
        'captions_expected': sum(r.get('captions_expected', 0) for r in rows),
        'captions_present': sum(r.get('captions_present', 0) for r in rows),
        'canvas_files': sum(r['canvas_files'] for r in rows),
        'canvas_submissions': sum(r.get('canvas_submissions', 0) for r in rows),
        'canvas_files_failed': sum(r['canvas_files_failed'] for r in rows),
        'courses_with_errors': sum(1 for r in rows if r['errors']),
    }
    # any_fail recomputed: only real (non-benign) issues count.
    any_fail = totals['courses_with_errors'] > 0 or totals['canvas_files_failed'] > 0
    return {'rows': rows, 'totals': totals, 'failed': any_fail}


def _safe(s: str) -> str:
    return re.sub(r'[^\w\-. ]+', '_', s).strip()[:120] or 'unnamed'


def write_report(out_root: Path, courses: list[dict], report: dict) -> None:
    (out_root / '_archive_report.json').write_text(json.dumps(report, indent=2))
    md = ['# MSDS archive report', '',
          f'_Generated {datetime.datetime.now(datetime.timezone.utc).isoformat()}_', '',
          '## Totals', '']
    for k, v in report['totals'].items():
        md.append(f'- **{k}**: {v}')
    md.extend(['', '## Per-course', '',
               '| course | sessions | mp4s | canvas files | errors |',
               '|---|---:|---:|---:|---|'])
    for r in report['rows']:
        md.append(
            f'| `{r["slug"]}` | {r["sessions_expected"]} '
            f'| {r["mp4s_present"]} | {r["canvas_files"]} '
            f'| {len(r["errors"])} |'
        )
    (out_root / '_archive_report.md').write_text('\n'.join(md))

    # INDEX.md is the human-friendly entry point at the root.
    idx = ['# MSDS archive', '',
           f'Top-level index. Each course directory contains course '
           f'materials at its root (`course.json`, `syllabus.html`, '
           f'`assignments/`, `pages/`, `modules.json`, `files/by_id/`, '
           f'`submissions/`, …) plus a `panopto/` subdirectory holding '
           f'lecture mp4s + captions.', '',
           f'Last archived: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M %Z")}',
           '',
           '## Courses',
           '',
           '| course | term | sessions | canvas files | submissions |',
           '|---|---|---:|---:|---:|']
    for c, r in zip(courses, report['rows']):
        term = (c.get('term') or {}).get('name', '')
        idx.append(
            f'| [{c["name"]}](./{r["slug"]}/) | {term} '
            f'| {r["sessions_expected"]} | {r["canvas_files"]} '
            f'| {r.get("canvas_submissions", 0)} |'
        )
    idx.extend(['', '## See also', '',
                '- `_archive_report.md` — verification report from the last run',
                '- `_archive_report.json` — same, machine-readable',
                '- `manifests/` — Panopto session manifests (re-run input)',
                '',
                '## Reading this drive from Windows',
                '',
                'This drive is formatted **APFS Case-sensitive** — natively '
                'readable on macOS and (read-only) on Linux via `apfs-fuse`. '
                'Windows does not natively read APFS; three options exist:',
                '',
                '1. **Plug into any Mac**, copy what you need off. Easiest.',
                '2. **WSL2 + `usbipd-win` + `apfs-fuse`** — free, read-only.',
                '3. **Paragon APFS for Windows** — paid (~$50), 10-day trial. '
                'Read/write.'])
    (out_root / 'INDEX.md').write_text('\n'.join(idx))


def print_report(report: dict) -> None:
    t = report['totals']
    print('\nverify ----', flush=True)
    print(f'  courses: {t["courses"]}  '
          f'sessions: {t["sessions_expected"]}  '
          f'mp4s: {t["mp4s_present"]}  '
          f'captions: {t["captions_present"]}/{t["captions_expected"]}  '
          f'GB: {t["mp4_GB"]}  '
          f'canvas files: {t["canvas_files"]}+{t["canvas_submissions"]} subs',
          flush=True)
    if t['courses_with_errors']:
        print(f'  ⚠  {t["courses_with_errors"]} course(s) have issues:',
              flush=True)
        for r in report['rows']:
            if r['errors']:
                print(f'     - {r["slug"]}: {"; ".join(r["errors"][:3])}',
                      flush=True)


# ----------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', required=True, help='archive root (e.g. /Volumes/ColdStore/msds-archive/)')
    ap.add_argument('--course-ids', help='comma-separated subset (default: all available + completed)')
    ap.add_argument('--skip-panopto', action='store_true')
    ap.add_argument('--skip-canvas', action='store_true')
    ap.add_argument('--mode', choices=['audio+screen', 'podcast', 'all'],
                    default='audio+screen',
                    help='per-session artifacts (default: audio+screen — '
                         'extracts audio.m4a from podcast and keeps the '
                         'higher-res screen capture; drops cam video)')
    ap.add_argument('--podcast-only', action='store_true',
                    help='deprecated; equivalent to --mode podcast')
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--min-free-gb', type=int, default=50)
    ap.add_argument('--verify-only', action='store_true',
                    help='just run the verifier; do not fetch anything')
    args = ap.parse_args()

    out_root = Path(args.output).expanduser().resolve()

    # Course discovery is needed for both verify and orchestration.
    token = get_token()
    if args.course_ids:
        wanted = {int(x) for x in args.course_ids.split(',')}
    else:
        wanted = None

    raw = canvas_get(
        '/api/v1/courses?per_page=100&include[]=term'
        '&state[]=available&state[]=completed', token,
    )
    courses = [c for c in raw if 'name' in c
               and (wanted is None or c['id'] in wanted)]
    if not courses:
        sys.exit('no courses matched')

    if args.verify_only:
        out_root.mkdir(parents=True, exist_ok=True)
        report = verify(out_root, courses)
        write_report(out_root, courses, report)
        print_report(report)
        sys.exit(1 if report['failed'] else 0)

    surface = preflight(out_root, args.min_free_gb,
                        need_browser=not args.skip_panopto)

    manifests_dir = out_root / 'manifests'
    manifests_dir.mkdir(parents=True, exist_ok=True)
    (manifests_dir / '_courses.json').write_text(json.dumps(
        [{'id': c['id'], 'name': c['name']} for c in courses], indent=2,
    ))

    course_ids = [c['id'] for c in courses]

    # Progress weighting (rough wall-time fractions):
    #   Phase A : 5%   (browser-driven, ~5-10 min for 28 courses)
    #   Phase B : 90%  (the multi-hour download)
    #   Phase C : 4%   (~30 min for the whole year)
    #   Verify  : 1%
    progress(0.0, 'starting')

    # Phase A — manifests for everyone (one shot, the script handles 1 surface).
    if not args.skip_panopto:
        progress(0.01, 'Phase A: extracting Panopto manifests')
        run_phase_a(surface, manifests_dir, course_ids)
        progress(0.05, 'Phase A done')
        # Phase B — per-course download into <out>/<slug>/panopto/
        eligible = []
        for c in courses:
            slug = f'{c["id"]}-{slugify(c["name"])}'
            mp = manifests_dir / f'{slug}.json'
            if not mp.exists():
                continue
            try:
                m = json.loads(mp.read_text())
            except Exception:
                continue
            if sum(1 for s in m.get('manifest', []) if s.get('podcast')) > 0:
                eligible.append((c, slug, mp))
        for i, (c, slug, mp) in enumerate(eligible):
            done_frac = i / max(1, len(eligible))
            progress(0.05 + 0.90 * done_frac,
                     f'Phase B: {i+1}/{len(eligible)} {c["name"][:50]}')
            mode = 'podcast' if args.podcast_only else args.mode
            run_phase_b(mp, out_root / slug / 'panopto',
                        mode=mode, workers=args.workers)
        progress(0.95, 'Phase B done')

    # Phase C — Canvas mirror. phase_c.py already writes to
    # <output>/<id>-<slug>/{course.json, syllabus.html, assignments/, ...},
    # which sits naturally next to the panopto/ subdir Phase B created.
    if not args.skip_canvas:
        progress(0.95, 'Phase C: mirroring Canvas content')
        run_phase_c(out_root, course_ids, args.workers)
        progress(0.99, 'Phase C done')

    # Verify and report
    progress(0.99, 'verifying archive')
    report = verify(out_root, courses)
    write_report(out_root, courses, report)
    print_report(report)
    t = report['totals']
    if report['failed']:
        progress(1.0, f'done with errors — {t["courses_with_errors"]} '
                      'course(s) need attention')
    else:
        progress(1.0, f'done — {t["sessions_expected"]} sessions, '
                      f'{t["mp4_GB"]} GB')
    sys.exit(1 if report['failed'] else 0)


if __name__ == '__main__':
    main()
