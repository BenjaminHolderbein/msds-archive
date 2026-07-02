#!/usr/bin/env python3
"""
Phase A — Panopto manifest extraction across Canvas courses.

For each course:
  1. Navigate the cmux browser surface to /courses/<id>/external_tools/119349.
  2. Re-target the LTI launch form to _top and submit (works around WKWebView
     third-party cookie SSO failure inside the embedded iframe).
  3. Wait until the URL is on usfca.hosted.panopto.com.
  4. Read the folder ID from location.hash.
  5. Call Data.svc/GetSessions then DeliveryInfo.aspx per session,
     including SRT captions inline.
  6. Atomically write <output_dir>/<course-slug>.json. A previously-good
     manifest is preserved on per-course failure (the summary stub is only
     written when there is no prior file to keep).

Usage:
  ./phase_a.py --output ./manifests                       # auto-detect surface,
                                                          # all available+completed
  ./phase_a.py --surface surface:1 --output ./manifests
  ./phase_a.py --output ./manifests --course-ids 1631774,1631766
"""
from __future__ import annotations

import argparse, json, os, re, subprocess, sys, time, urllib.error, urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
EXTRACT_JS = SCRIPT_DIR / 'extract_manifest.js'

CANVAS_BASE = 'https://usfca.instructure.com'
PANOPTO_LTI_TOOL_ID = '119349'  # USF's global Panopto LTI; same across all courses


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
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


def canvas_get(path: str, token: str) -> list[dict]:
    """Paginated Canvas GET. Raises CanvasAuthError on 401, RuntimeError on others."""
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
                raise CanvasAuthError(
                    f'Canvas rejected token (401) on {path}. '
                    'Refresh the canvas-api-token Keychain entry.'
                )
            raise RuntimeError(f'Canvas {e.code} on {path}: {e.read()[:200]!r}')
    return out


class CanvasAuthError(Exception):
    pass


def cmux(surface: str, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ['cmux', 'browser', surface, *args],
        capture_output=True, text=True, timeout=timeout,
    )


def browser_eval(surface: str, js: str, timeout: int = 120, retries: int = 2) -> str:
    """cmux eval can transiently fail post-navigation; retry briefly."""
    last_err = ''
    for attempt in range(retries + 1):
        r = cmux(surface, 'eval', js, timeout=timeout)
        if r.returncode == 0:
            return r.stdout.strip()
        last_err = r.stderr.strip()
        time.sleep(1.0 + attempt)
    raise RuntimeError(f'eval failed after {retries + 1} attempts: {last_err}')


def get_url(surface: str) -> str:
    return browser_eval(surface, 'location.href').strip('"')


def find_canvas_surface() -> str | None:
    """Return a cmux browser surface ref already on usfca.instructure.com or
    usfca.hosted.panopto.com (SSO is shared, either is fine)."""
    r = subprocess.run(['cmux', 'tree', '--all'], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    pat = re.compile(
        r'(surface:\d+)\s+\[browser\][^\n]*?(usfca\.instructure\.com|usfca\.hosted\.panopto\.com)'
    )
    m = pat.search(r.stdout)
    return m.group(1) if m else None


def _dbg(msg: str) -> None:
    if os.environ.get('PHASE_A_DEBUG'):
        print(f'    . {msg}', flush=True)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(text)
    tmp.rename(path)


def launch_panopto(surface: str, course_id: int) -> str | None:
    """Navigate to a Canvas course's Panopto external tool launch page, then
    re-target the LTI form at _top so SSO completes in the top-level document.
    Returns the resulting Panopto URL, or None if the course has no Panopto tool.
    """
    url = f'{CANVAS_BASE}/courses/{course_id}/external_tools/{PANOPTO_LTI_TOOL_ID}'
    cmux(surface, 'navigate', url)
    expected = f'/courses/{course_id}/external_tools'
    for _ in range(40):
        u = get_url(surface)
        if expected in u:
            break
        time.sleep(0.5)
    else:
        _dbg(f'never landed on canvas course page, last url: {get_url(surface)}')
        return None

    # If we ended up on USF's CAS login, the browser session is dead.
    if 'usfcas.usfca.edu' in get_url(surface) or '/login/cas' in get_url(surface):
        raise RuntimeError(
            'Browser surface lost its Canvas session (redirected to CAS login). '
            'Log into Canvas in the cmux browser and re-run.'
        )

    cmux(surface, 'wait', '--load-state', 'complete', '--timeout-ms', '20000')
    _dbg(f'on canvas: {get_url(surface)}')

    form_id = None
    for _ in range(20):
        r = browser_eval(
            surface,
            "(() => { const f = document.querySelector('form[action*=\"panopto.com\"]');"
            "return f ? f.id : null; })()",
        )
        form_id = r.strip().strip('"')
        if form_id and form_id != 'null':
            break
        time.sleep(0.5)
    else:
        _dbg('no panopto form found')
        return None
    _dbg(f'form: {form_id}')

    browser_eval(
        surface,
        f"const f = document.getElementById('{form_id}'); f.target = '_top'; f.submit(); 'ok'",
    )
    for _ in range(30):
        u = get_url(surface)
        if 'panopto.com' in u:
            cmux(surface, 'wait', '--load-state', 'complete', '--timeout-ms', '15000')
            _dbg(f'on panopto: {u}')
            return u
        time.sleep(0.5)
    _dbg(f'never landed on panopto, last url: {get_url(surface)}')
    return None


def extract_manifest(surface: str) -> dict:
    js = EXTRACT_JS.read_text()
    out = browser_eval(surface, js, timeout=240)
    return json.loads(out)


def process_course(surface: str, course: dict, out_dir: Path) -> dict:
    cid, cname = course['id'], course['name']
    slug = f'{cid}-{slugify(cname)}'
    dest = out_dir / f'{slug}.json'
    summary = {'course_id': cid, 'name': cname, 'slug': slug, 'panopto': None,
               'sessions': 0, 'sessions_errored': 0, 'hours': 0.0, 'new': 0,
               'error': None, 'preserved_prior': False}

    prior_ids: set[str] = set()
    prior_existed = dest.exists()
    if prior_existed:
        try:
            prior = json.loads(dest.read_text())
            prior_ids = {s['id'] for s in (prior.get('manifest') or []) if s.get('id')}
        except Exception:
            pass

    try:
        panopto_url = launch_panopto(surface, cid)
        if not panopto_url:
            summary['error'] = 'no_panopto_tool'
            # Only write a stub if there is no prior manifest to preserve.
            if not prior_existed:
                _atomic_write(dest, json.dumps({**summary, 'manifest': []}, indent=2))
            else:
                summary['preserved_prior'] = True
            return summary
        summary['panopto'] = panopto_url
        m = extract_manifest(surface)
        m['course_id'] = cid
        m['course_name'] = cname
        m['panopto_url'] = panopto_url
        _atomic_write(dest, json.dumps(m, indent=2))
        summary['sessions'] = m.get('count', 0)
        summary['sessions_errored'] = m.get('sessionsErrored', 0)
        summary['hours'] = round(
            sum(x.get('duration', 0) for x in m.get('manifest', [])) / 3600, 1
        )
        new_ids = {s['id'] for s in m.get('manifest', []) if s.get('id')} - prior_ids
        summary['new'] = len(new_ids)
    except Exception as e:
        summary['error'] = repr(e)
        # Preserve any prior good manifest on failure.
        if not prior_existed:
            _atomic_write(dest, json.dumps(summary, indent=2))
        else:
            summary['preserved_prior'] = True
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--surface',
                    help='cmux browser surface ref (auto-detected if omitted)')
    ap.add_argument('--output', required=True, help='manifest output directory')
    ap.add_argument('--course-ids',
                    help='comma-separated subset of course IDs '
                         '(else all available + completed enrolled courses)')
    ap.add_argument('--skip-existing', action='store_true',
                    help='skip courses whose manifest already exists with no error')
    args = ap.parse_args()

    surface = args.surface or find_canvas_surface()
    if not surface:
        sys.exit(
            'No cmux browser surface found on Canvas/Panopto. Open one with:\n'
            '  cmux browser open https://usfca.instructure.com\n'
            'and log in, then re-run.'
        )

    token = get_token()
    try:
        if args.course_ids:
            wanted = {int(x) for x in args.course_ids.split(',')}
            all_courses = canvas_get(
                '/api/v1/courses?per_page=100&state[]=available&state[]=completed',
                token,
            )
            courses = [c for c in all_courses if c.get('id') in wanted and 'name' in c]
        else:
            all_courses = canvas_get(
                '/api/v1/courses?per_page=100&state[]=available&state[]=completed',
                token,
            )
            courses = [c for c in all_courses if 'name' in c]
    except CanvasAuthError as e:
        sys.exit(str(e))

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.skip_existing:
        before = len(courses)
        courses = [
            c for c in courses
            if not _has_clean_manifest(out_dir / f'{c["id"]}-{slugify(c["name"])}.json')
        ]
        print(f'skip-existing: {before - len(courses)} courses already done',
              flush=True)

    print(f'phase A | surface={surface} | courses={len(courses)} → {out_dir}',
          flush=True)
    rollup = []
    for c in courses:
        r = process_course(surface, c, out_dir)
        rollup.append(r)
        flag = '!!' if r['error'] else 'ok'
        delta = f' (+{r["new"]} new)' if r.get('new') else ''
        err = ''
        if r.get('sessions_errored'):
            err += f' [{r["sessions_errored"]} session errs]'
        if r.get('error'):
            err += f' ({r["error"]})'
            if r.get('preserved_prior'):
                err += ' [prior preserved]'
        print(f'  [{flag}] {r["course_id"]:>8}  {r["sessions"]:>3} sessions  '
              f'{r["hours"]:>5.1f} h  {r["name"]}{delta}{err}',
              flush=True)

    _atomic_write(out_dir / '_rollup.json', json.dumps(rollup, indent=2))
    total_h = sum(r['hours'] for r in rollup)
    total_s = sum(r['sessions'] for r in rollup)
    failed = sum(1 for r in rollup if r['error'])
    print(f'done | {total_s} sessions | {total_h:.1f} hours total | {failed} failed')
    sys.exit(1 if failed else 0)


def _has_clean_manifest(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        d = json.loads(path.read_text())
    except Exception:
        return False
    # The manifest file itself doesn't carry an error key (the rollup does);
    # a clean file has a "manifest" array (possibly empty for no-recordings courses).
    return isinstance(d, dict) and 'manifest' in d


if __name__ == '__main__':
    main()
