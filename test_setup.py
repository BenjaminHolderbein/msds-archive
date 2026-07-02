#!/usr/bin/env python3
"""
Pre-flight test suite. Run before kicking off `archive.py` for a long
unattended run, so failures that would only surface mid-run get caught up
front (target: under 60s total).

  python3 test_setup.py --output /Volumes/ColdStore/msds-archive/

Exits non-zero if any test fails. With --bail, stops at first failure.
"""
from __future__ import annotations

import argparse, datetime, json, os, re, shutil, subprocess, sys, time
import urllib.error, urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

# Reuse internals from the pipeline so tests stay in sync with what runs.
from archive import (  # type: ignore
    CANVAS_BASE, get_token, canvas_get, find_canvas_surface,
    head_size, slugify,
)

GREEN, RED, YELLOW, RESET = '\033[92m', '\033[91m', '\033[93m', '\033[0m'
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = RESET = ''

# A non-interactive CloudFront URL we can probe — pulled from a known manifest
# at runtime, with a hardcoded backstop in case no manifests exist yet.
FALLBACK_PROBE_URL = (
    'https://d2y36twrtb17ty.cloudfront.net/sessions/'
    '267f3348-706d-4fca-ab60-b41d01550b91/'
    'c0258389-3cc8-4667-9f6c-b41d01550b96-682d7cf2-4342-4a4e-9113-b44701601226.mp4'
)


# --------------------------------------------------------------- harness ---

class Skip(Exception):
    pass


def _shell(*args: str, timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def cmux_eval(surface: str, js: str, timeout: int = 30) -> str:
    r = _shell('cmux', 'browser', surface, 'eval', js, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f'cmux eval failed: {r.stderr.strip()}')
    return r.stdout.strip().strip('"')


def _load_any_manifest() -> dict | None:
    mdir = SCRIPT_DIR / 'manifests'
    if not mdir.exists():
        return None
    for p in sorted(mdir.glob('*.json')):
        if p.name.startswith('_'):
            continue
        try:
            d = json.loads(p.read_text())
            if d.get('manifest'):
                return d
        except Exception:
            continue
    return None


def _smallest_panopto_course() -> tuple[int, str] | None:
    """Pick the smallest course-by-session-count from existing manifests, so the
    one-course probes finish fast. Falls back to None if nothing on disk."""
    mdir = SCRIPT_DIR / 'manifests'
    if not mdir.exists():
        return None
    best = None
    for p in sorted(mdir.glob('*.json')):
        if p.name.startswith('_'):
            continue
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        cid = d.get('course_id')
        n = d.get('count', 0)
        if cid is None:
            continue
        # Prefer 0 sessions (admin courses) — fastest browser probe.
        if best is None or n < best[2]:
            best = (cid, d.get('course_name', f'course-{cid}'), n)
    return (best[0], best[1]) if best else None


# --------------------------------------------------------------- tests ---

def t_tools_on_path(_args, _ctx) -> str:
    missing = [t for t in ('cmux', 'curl', 'ffmpeg', 'security')
               if not shutil.which(t)]
    if missing:
        raise RuntimeError(f'missing on PATH: {", ".join(missing)}')
    return 'cmux curl ffmpeg security ✓'


def t_ffmpeg_executes(_args, _ctx) -> str:
    r = _shell('ffmpeg', '-version', timeout=5)
    if r.returncode != 0 or 'ffmpeg version' not in r.stdout:
        raise RuntimeError(f'ffmpeg -version failed: {r.stderr[:200]!r}')
    return r.stdout.splitlines()[0]


def t_output_volume_real(args, _ctx) -> str:
    out = Path(args.output)
    if str(out).startswith('/Volumes/'):
        mount = Path('/Volumes') / out.relative_to('/Volumes').parts[0]
        if not os.path.ismount(mount):
            raise RuntimeError(
                f'{mount} is NOT a mounted volume — is the HDD plugged in?'
            )
        return f'mountpoint {mount} ✓'
    return f'(local path; not /Volumes/) {out}'


def t_output_writable_and_space(args, _ctx) -> str:
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    test = out / '.test_setup_write'
    try:
        test.write_text('ok')
        test.unlink()
    except OSError as e:
        raise RuntimeError(f'cannot write to {out}: {e}')
    free_gb = shutil.disk_usage(out).free / (1 << 30)
    if free_gb < args.min_free_gb:
        raise RuntimeError(
            f'only {free_gb:.1f} GB free at {out}; '
            f'wanted ≥ {args.min_free_gb} GB (lower with --min-free-gb)'
        )
    return f'writable; {free_gb:.1f} GB free'


def t_keychain_token(_args, ctx) -> str:
    token = get_token()
    if not token or len(token) < 20 or any(c.isspace() for c in token):
        raise RuntimeError('Keychain returned empty/short/whitespace-tainted token')
    ctx['token'] = token
    return f'token length {len(token)} ✓ (not printed)'


def t_canvas_https_reachable(_args, _ctx) -> str:
    r = _shell('curl', '-sI', '-m', '5', f'{CANVAS_BASE}/login', timeout=8)
    if r.returncode != 0 or not re.search(r'HTTP/[\d.]+\s+(200|301|302)', r.stdout):
        raise RuntimeError('Canvas /login did not return a normal status. '
                           'Check VPN, captive portal, or DNS.')
    return r.stdout.splitlines()[0]


def t_cloudfront_head(_args, ctx) -> str:
    manifest = _load_any_manifest()
    url = next(
        (s.get('podcast') for s in (manifest or {}).get('manifest', []) if s.get('podcast')),
        FALLBACK_PROBE_URL,
    )
    ctx['probe_podcast_url'] = url
    sz = head_size(url)
    if sz is None or sz <= 0:
        raise RuntimeError(
            f'CloudFront HEAD returned no Content-Length for {url[:80]}…'
        )
    return f'Content-Length {sz:,} bytes ✓'


def t_canvas_token_self(_args, ctx) -> str:
    token = ctx['token']
    data = canvas_get('/api/v1/users/self', token)
    if not data or not data[0].get('id'):
        raise RuntimeError('users/self returned no id field')
    ctx['self_id'] = data[0]['id']
    return f'user id {data[0]["id"]} ({data[0].get("name","?")})'


def t_canvas_token_courses_scope(_args, ctx) -> str:
    token = ctx['token']
    # per_page=100 keeps this to ~1 request even for users with many courses;
    # canvas_get otherwise paginates through all of them.
    data = canvas_get(
        '/api/v1/courses?per_page=100&state[]=available&state[]=completed',
        token,
    )
    visible = [c for c in data if 'name' in c]
    if not visible:
        raise RuntimeError('courses listing returned 0 entries — '
                           'token may lack the right scope, or no enrollments')
    ctx['courses_sample'] = visible
    return f'{len(visible)} courses visible (first: {visible[0]["name"][:50]})'


def t_canvas_pagination(_args, ctx) -> str:
    """Confirm Link: rel=next is followed. Hit a small per_page on a course
    with many assignments so the second page actually gets fetched."""
    token = ctx['token']
    # Reuse a course from the cached samples; pick one with ≥3 assignments.
    candidates = ctx.get('courses_sample') or canvas_get(
        '/api/v1/courses?per_page=20&state[]=available&state[]=completed', token,
    )
    for c in candidates:
        cid = c.get('id')
        if cid is None:
            continue
        try:
            data = canvas_get(
                f'/api/v1/courses/{cid}/assignments?per_page=2', token,
            )
            if len(data) > 2:
                return f'paginated past page 1 ({len(data)} items, course {cid})'
        except SystemExit:
            continue
    raise Skip('no course has >2 assignments to test pagination')


def t_canvas_token_file_scope(_args, ctx) -> str:
    """Try fetching one Canvas file by id, end-to-end. Discovers a real
    file_id from any saved manifest's underlying course."""
    token = ctx['token']
    # Try to find a file_id from a Phase C state file or modules.json on disk.
    file_id = None
    course_id = None
    for state in SCRIPT_DIR.parent.rglob('files_index.json'):
        try:
            d = json.loads(state.read_text())
            for k, v in d.items():
                if v.get('id'):
                    file_id = v['id']
                    # course id is in the parent dir name like "1631791-..."
                    parent = state.parent.parent.name
                    m = re.match(r'(\d+)-', parent)
                    if m:
                        course_id = int(m.group(1))
                    break
            if file_id:
                break
        except Exception:
            continue
    if not file_id or not course_id:
        raise Skip('no files_index.json on disk to source a file_id from')
    data = canvas_get(f'/api/v1/courses/{course_id}/files/{file_id}', token)
    if not data or not data[0].get('url'):
        raise RuntimeError(f'file {file_id} fetched but no presigned url returned')
    ctx['probe_file_url'] = data[0]['url']
    ctx['probe_file_size'] = data[0].get('size')
    return f'file {file_id} ✓ ({data[0].get("display_name","?")[:50]})'


def t_canvas_token_submission_scope(_args, ctx) -> str:
    """Confirm submissions/self with submission_comments + rubric_assessment
    works — these scopes are different from the basic profile read."""
    token = ctx['token']
    candidates = ctx.get('courses_sample') or []
    for c in candidates:
        cid = c.get('id')
        try:
            assignments = canvas_get(
                f'/api/v1/courses/{cid}/assignments?per_page=1', token,
            )
        except SystemExit:
            continue
        if not assignments:
            continue
        aid = assignments[0]['id']
        try:
            data = canvas_get(
                f'/api/v1/courses/{cid}/assignments/{aid}/submissions/self'
                '?include[]=submission_comments&include[]=rubric_assessment',
                token,
            )
        except SystemExit:
            continue
        if data:
            return f'submission scope ✓ (course {cid}, assignment {aid})'
    raise Skip('no course has assignments to test submission scope')


def t_disk_budget_vs_estimate(args, ctx) -> str:
    """Sample a few podcast HEADs across saved manifests, extrapolate per-hour
    bytes, project for the full hours total, compare to free space."""
    if args.min_free_gb < 50:
        # User explicitly relaxed the floor — they're testing, not doing the
        # real run. Don't block on a multi-hundred-GB budget calculation.
        raise Skip(f'--min-free-gb {args.min_free_gb} < 50 (test mode)')
    out = Path(args.output)
    free_gb = shutil.disk_usage(out).free / (1 << 30)
    mdir = SCRIPT_DIR / 'manifests'
    if not mdir.exists():
        raise Skip('no manifests on disk to estimate from')
    podcast_urls: list[tuple[str, float]] = []
    total_hours = 0.0
    for p in sorted(mdir.glob('*.json')):
        if p.name.startswith('_'):
            continue
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        for s in d.get('manifest') or []:
            if s.get('podcast') and s.get('duration', 0) > 0:
                podcast_urls.append((s['podcast'], s['duration']))
        total_hours += sum(s.get('duration', 0) for s in d.get('manifest') or []) / 3600
    if not podcast_urls:
        raise Skip('no podcast URLs in manifests yet to sample')
    # Sample up to 5 URLs in parallel; extrapolate MB/hour.
    import concurrent.futures
    sample = podcast_urls[:: max(1, len(podcast_urls) // 5)][:5]
    sizes = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        for s, dur in zip(sample, [d for _, d in sample]):
            sizes.append((ex.submit(head_size, s[0]).result() or 0, dur))
    bytes_per_hour = (
        sum(b / (h / 3600) for b, h in sizes if b and h) / max(1, len([1 for b, h in sizes if b]))
    )
    est_gb = (bytes_per_hour * total_hours) / (1 << 30)
    if not args.skip_panopto:
        # canvas content adds a small constant (~10 GB upper bound for whole MSDS year)
        est_gb += min(10, 0.5 * len(list(mdir.glob('*.json'))))
    margin = 1.25  # require headroom
    if free_gb < est_gb * margin:
        raise RuntimeError(
            f'free space {free_gb:.1f} GB < estimated {est_gb:.0f} GB × '
            f'{margin}x margin = {est_gb*margin:.0f} GB. '
            'Pick a bigger drive or pass --podcast-only / fewer courses.'
        )
    return (f'~{est_gb:.0f} GB needed (sampled {len(sizes)} URLs); '
            f'{free_gb:.1f} GB free ✓')


def t_cmux_ping(_args, _ctx) -> str:
    r = _shell('cmux', 'tree', '--all', timeout=5)
    if r.returncode != 0:
        raise RuntimeError(f'cmux tree --all failed: {r.stderr.strip()[:200]}')
    return f'cmux daemon responded'


def t_browser_surface_alive(_args, ctx) -> str:
    surface = find_canvas_surface()
    if not surface:
        raise RuntimeError(
            'no cmux browser surface on Canvas/Panopto; open one with:\n'
            '  cmux browser open https://usfca.instructure.com\n'
            '  …then log in.'
        )
    ctx['surface'] = surface
    url = cmux_eval(surface, 'location.href')
    if 'usfcas.usfca.edu' in url or '/login/cas' in url:
        raise RuntimeError(
            f'surface {surface} is on CAS login ({url}). Re-authenticate '
            'in the cmux browser and re-run.'
        )
    return f'{surface} on {url[:80]}'


def t_canvas_dom_queryable(args, ctx) -> str:
    """Navigate surface to Canvas root and confirm we get a real DOM, not a
    blank/zombie page. Uses the existing surface from t_browser_surface_alive."""
    surface = ctx['surface']
    _shell('cmux', 'browser', surface, 'navigate',
           f'{CANVAS_BASE}/', timeout=10)
    # Wait for URL to actually settle on Canvas root.
    for _ in range(20):
        u = cmux_eval(surface, 'location.href')
        if 'usfca.instructure.com' in u and '/login/cas' not in u:
            break
        time.sleep(0.5)
    else:
        raise RuntimeError(f'surface never settled on Canvas; last url {u}')
    has_nav = cmux_eval(
        surface,
        "document.querySelector('#global_nav, #left-side, body') ? 'ok' : 'no'",
    )
    if has_nav != 'ok':
        raise RuntimeError('Canvas DOM not queryable via eval')
    return 'global_nav present ✓'


def t_e2e_panopto_roundtrip(_args, ctx) -> str:
    """The contract test: launch Panopto LTI for one course, confirm we
    land on Panopto with a folder hash, and one Data.svc round-trip works."""
    surface = ctx['surface']
    pick = _smallest_panopto_course()
    if not pick:
        raise Skip('no manifests on disk to pick a probe course from')
    cid, _ = pick
    # Use phase_a's launch_panopto for fidelity.
    from phase_a import launch_panopto  # type: ignore
    panopto_url = launch_panopto(surface, cid)
    if not panopto_url or 'panopto.com' not in panopto_url:
        raise RuntimeError(f'launch_panopto({cid}) did not land on Panopto: '
                           f'{panopto_url!r}')
    if 'folderID' not in panopto_url:
        raise RuntimeError(f'no folderID in URL hash: {panopto_url}')
    # One round-trip into Panopto's Data.svc to prove auth carries.
    js = (
        "fetch('/Panopto/Services/Data.svc/GetSessions', {"
        "method:'POST',"
        "headers:{'content-type':'application/json'},"
        "body: JSON.stringify({queryParameters:{folderID: "
        "decodeURIComponent(location.hash).match(/folderID=\"?([0-9a-f-]+)\"?/i)[1],"
        "page:0,maxResults:1,sortAscending:true,sortColumn:1,getFolderData:true}})"
        "}).then(r=>r.json()).then(d => d.d ? 'ok' : 'no_d_field')"
    )
    out = cmux_eval(surface, js, timeout=20)
    if out != 'ok':
        raise RuntimeError(f'Data.svc/GetSessions: {out}')
    return f'course {cid} → Panopto folder + Data.svc round-trip ✓'


def t_caption_srt_roundtrip(_args, ctx) -> str:
    """Confirm one caption fetch returns valid SRT. Catches Panopto
    rate-limits / silent caption fetch failures."""
    surface = ctx.get('surface')
    if not surface:
        raise Skip('browser surface not validated')
    # Pick a session id with captions from any manifest on disk.
    pick = None
    for p in (SCRIPT_DIR / 'manifests').glob('*.json'):
        if p.name.startswith('_'):
            continue
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        for s in d.get('manifest') or []:
            for c in (s.get('captions') or []):
                # Tolerate legacy shape (list of language ints) vs new shape
                # (list of {language, srt} dicts).
                if isinstance(c, dict) and c.get('srt'):
                    pick = (s['id'], c)
                    break
            if pick:
                break
        if pick:
            break
    if not pick:
        raise Skip('no manifest sessions on disk have captions to validate')
    sid, cap = pick
    srt = cap['srt']
    if not re.match(r'^\d+\n\d\d:\d\d:\d\d', srt):
        raise RuntimeError('saved SRT does not start with a valid cue')
    return f'session {sid[:8]} captions parse as SRT ✓ ({len(srt):,} bytes)'


def t_canvas_file_range_fetch(_args, ctx) -> str:
    url = ctx.get('probe_file_url')
    if not url:
        raise Skip('no probe Canvas file url (file scope test was skipped)')
    r = _shell('curl', '-fLsS', '-m', '15', '--range', '0-1023',
               '-o', '/dev/null', '-w', '%{http_code}', url, timeout=20)
    # Canvas redirects to a presigned S3-like URL; -L follows it. We expect
    # the final code to be 200/206.
    if r.returncode != 0 or r.stdout.strip() not in ('200', '206'):
        raise RuntimeError(
            f'range-fetch curl rc={r.returncode} final HTTP {r.stdout!r}'
        )
    return f'range-fetch returned HTTP {r.stdout.strip()} ✓'


def t_panopto_url_no_signature(_args, ctx) -> str:
    """README hard-won fact #4: CloudFront URLs are unsigned. If USF flips this
    behavior, manifests extracted today will silently 403 tomorrow."""
    url = ctx.get('probe_podcast_url')
    if not url:
        raise Skip('no podcast URL probed earlier')
    bad = ('Signature=', 'Expires=', 'X-Amz-Signature', 'Key-Pair-Id=', 'Policy=')
    found = [m for m in bad if m in url]
    if found:
        raise RuntimeError(f'CloudFront URL appears signed (markers: {found}); '
                           'manifests will expire — re-extract Phase A nightly')
    return 'unsigned ✓'


def t_no_orphan_partials(args, _ctx) -> str:
    out = Path(args.output)
    if not out.exists():
        return '(no output dir yet)'
    orphans = list(out.rglob('*.partial'))
    if orphans:
        sample = ', '.join(p.name for p in orphans[:3])
        raise RuntimeError(
            f'{len(orphans)} stale .partial files (e.g. {sample}). '
            f'`find {out} -name "*.partial" -delete` to clean.'
        )
    return 'no .partial leftovers'


def t_long_filename(args, _ctx) -> str:
    """Confirm worst-case Panopto path length writes OK on the target volume."""
    out = Path(args.output)
    if not out.exists():
        out.mkdir(parents=True, exist_ok=True)
    long_slug = '1234567-' + 'a' * 72
    long_session = ('b' * 32) + '__' + ('c' * 120)
    p = out / long_slug / 'panopto' / long_session
    try:
        p.mkdir(parents=True, exist_ok=True)
        f = p / 'podcast.mp4'
        f.write_bytes(b'x')
        f.unlink()
    finally:
        # Best-effort cleanup — don't pollute the real archive.
        try: p.rmdir()
        except OSError: pass
        try: p.parent.rmdir()
        except OSError: pass
        try: p.parent.parent.rmdir()
        except OSError: pass
    return f'{len(str(p))}-char path ok'


# --------------------------------------------------------------- main ---

# Ordered: cheapest first; each tier depends only on previous tiers' ctx.
TESTS = [
    ('tools_on_path',                 t_tools_on_path),
    ('ffmpeg_executes',               t_ffmpeg_executes),
    ('output_volume_real',            t_output_volume_real),
    ('output_writable_and_space',     t_output_writable_and_space),
    ('keychain_token',                t_keychain_token),
    ('canvas_https_reachable',        t_canvas_https_reachable),
    ('cloudfront_head',               t_cloudfront_head),
    ('canvas_token_self',             t_canvas_token_self),
    ('canvas_token_courses_scope',    t_canvas_token_courses_scope),
    ('canvas_pagination',             t_canvas_pagination),
    ('canvas_token_file_scope',       t_canvas_token_file_scope),
    ('canvas_token_submission_scope', t_canvas_token_submission_scope),
    ('disk_budget_vs_estimate',       t_disk_budget_vs_estimate),
    ('cmux_ping',                     t_cmux_ping),
    ('browser_surface_alive',         t_browser_surface_alive),
    ('canvas_dom_queryable',          t_canvas_dom_queryable),
    ('e2e_panopto_roundtrip',         t_e2e_panopto_roundtrip),
    ('caption_srt_roundtrip',         t_caption_srt_roundtrip),
    ('canvas_file_range_fetch',       t_canvas_file_range_fetch),
    ('panopto_url_no_signature',      t_panopto_url_no_signature),
    ('no_orphan_partials',            t_no_orphan_partials),
    ('long_filename',                 t_long_filename),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', required=True,
                    help='archive root that the real run will use')
    ap.add_argument('--min-free-gb', type=int, default=50)
    ap.add_argument('--skip-panopto', action='store_true',
                    help='skip Panopto-specific tests (use if running Canvas-only)')
    ap.add_argument('--bail', action='store_true',
                    help='stop at first failure')
    args = ap.parse_args()

    panopto_specific = {
        'cmux_ping', 'browser_surface_alive', 'canvas_dom_queryable',
        'e2e_panopto_roundtrip', 'caption_srt_roundtrip',
        'panopto_url_no_signature', 'cloudfront_head',
    }

    ctx: dict = {}
    started = time.time()
    pass_n = fail_n = skip_n = 0
    failures: list[tuple[str, str]] = []

    for name, fn in TESTS:
        if args.skip_panopto and name in panopto_specific:
            print(f'  {YELLOW}skip{RESET}  {name}  (--skip-panopto)', flush=True)
            skip_n += 1
            continue
        t0 = time.time()
        try:
            msg = fn(args, ctx)
            dt = time.time() - t0
            print(f'  {GREEN}ok  {RESET}  {name:<32}  {dt:>5.1f}s  {msg}', flush=True)
            pass_n += 1
        except Skip as e:
            dt = time.time() - t0
            print(f'  {YELLOW}skip{RESET}  {name:<32}  {dt:>5.1f}s  {e}', flush=True)
            skip_n += 1
        except Exception as e:
            dt = time.time() - t0
            print(f'  {RED}FAIL{RESET}  {name:<32}  {dt:>5.1f}s  {e}', flush=True)
            fail_n += 1
            failures.append((name, str(e)))
            if args.bail:
                break

    total = time.time() - started
    print(f'\n{pass_n} passed | {skip_n} skipped | {fail_n} failed | '
          f'{total:.1f}s total')
    if failures:
        print('\nFailures:')
        for n, m in failures:
            print(f'  - {n}: {m}')
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
