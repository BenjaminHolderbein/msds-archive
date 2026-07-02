#!/usr/bin/env python3
"""
Phase C — Mirror everything Canvas exposes about a course.

For each course we capture:
  - course meta (incl. syllabus body, term, teachers, tabs)
  - modules (full structure with items)
  - assignments (full bodies + attached files + my submissions)
  - pages (full HTML bodies)
  - quizzes (metadata; questions when accessible)
  - discussions (topic + entire reply tree via /view)
  - announcements (same shape as discussions)
  - rubrics (when accessible)
  - groups (metadata only; group internals are typically student-private)
  - files (every file ID discovered across all of the above)

Each course lands in its own subdirectory. Re-runs are idempotent: file
downloads skip when local size matches Canvas's reported size *and* local
mtime is >= Canvas's `updated_at`. JSON metadata is rewritten unconditionally
(it is cheap and lets us see drift in `_state.json`).

Usage:
  ./phase_c.py --output ./content [--course-ids 1631791,1623951] [--workers 4]

Optional flags:
  --skip-quizzes      Skip quiz fetch (often slow + access-restricted).
  --skip-submissions  Don't download my own submitted files.
  --skip-groups       Skip group metadata.
"""
from __future__ import annotations

import argparse, datetime, hashlib, json, os, re, subprocess, sys, time
import urllib.parse, urllib.request
import concurrent.futures
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CANVAS_BASE = 'https://usfca.instructure.com'

SAFE_FS = re.compile(r'[^\w\-. ]+')
# Match course-context file references only. Bare /users/<id>/files or
# /groups/<id>/files reference user/group blobs we usually can't fetch via
# the per-course endpoint, and downloading them spams 404s.
FILE_REF_RE = re.compile(
    r'/courses/\d+/files/(\d+)(?:/(?:download|preview))?', re.IGNORECASE
)


class CanvasAuthError(Exception):
    """Raised on first 401 — Canvas token has been invalidated; abort the run."""


def fs_safe(name: str, max_len: int = 120) -> str:
    return SAFE_FS.sub('_', name).strip()[:max_len] or 'unnamed'


def slugify(name: str) -> str:
    s = re.sub(r'[^\w\s-]', '', name).strip().lower()
    return re.sub(r'[\s_-]+', '-', s)[:80]


def get_canvas_token() -> str:
    token = os.environ.get('CANVAS_API_TOKEN')
    if token:
        return token
    r = subprocess.run(
        ['security', 'find-generic-password', '-a', os.environ['USER'],
         '-s', 'canvas-api-token', '-w'],
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


# -------- Canvas API client (paginated, retrying, rate-limit aware) --------

@dataclass
class CanvasClient:
    token: str
    base: str = CANVAS_BASE
    timeout: int = 60

    def _req(self, url: str) -> tuple[int, dict, bytes]:
        req = urllib.request.Request(
            url, headers={'Authorization': f'Bearer {self.token}'},
        )
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return r.status, dict(r.headers), r.read()
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    # Token gone — abort the whole run; do not waste retries.
                    raise CanvasAuthError(
                        f'Canvas rejected token (401) on {url}. '
                        'Refresh the canvas-api-token Keychain entry.'
                    )
                if e.code == 429:
                    # Honor Retry-After when present.
                    retry_after = e.headers.get('Retry-After')
                    delay = int(retry_after) if (retry_after or '').isdigit() else 2 ** attempt
                    if attempt < 4:
                        time.sleep(min(delay, 60))
                        continue
                if e.code in (500, 502, 503, 504) and attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                body = e.read()
                return e.code, dict(e.headers), body
            except (urllib.error.URLError, TimeoutError):
                if attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise
        # Defensive — exhausted all retries on a retryable error path.
        return 0, {}, b''

    def get_json(self, path: str) -> tuple[int, Any]:
        url = path if path.startswith('http') else f'{self.base}{path}'
        status, _, body = self._req(url)
        try:
            return status, json.loads(body) if body else None
        except json.JSONDecodeError:
            return status, {'_raw': body[:500].decode('utf-8', 'replace')}

    def get_paginated(self, path: str) -> tuple[int, list[Any]]:
        """Follow Canvas's Link: rel=next pagination. Returns (last_status, items)."""
        url = path if path.startswith('http') else f'{self.base}{path}'
        out: list[Any] = []
        last_status = 200
        while url:
            status, headers, body = self._req(url)
            last_status = status
            if status >= 400:
                return status, out
            try:
                page = json.loads(body) if body else []
            except json.JSONDecodeError:
                return status, out
            if isinstance(page, list):
                out.extend(page)
            else:
                # error wrapped as object — caller will see status
                return status, out
            link = headers.get('Link', '')
            m = re.search(r'<([^>]+)>;\s*rel="next"', link)
            url = m.group(1) if m else None
        return last_status, out


# -------- per-resource fetchers --------

def _is_ok(status: int) -> bool:
    return 200 <= status < 300


def discover_file_ids(html: str | None) -> set[str]:
    if not html:
        return set()
    return set(FILE_REF_RE.findall(html))


def _parse_iso(s: str | None) -> float | None:
    """Canvas timestamps are ISO 8601 with trailing Z. Return Unix seconds, or None."""
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError):
        return None


@dataclass
class ArchiveStats:
    course_id: int
    course_name: str
    counts: dict[str, int] = field(default_factory=dict)
    files_new: int = 0
    files_skipped: int = 0
    files_failed: int = 0
    bytes_downloaded: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class CourseArchiver:
    client: CanvasClient
    course_id: int
    course_name: str
    course_dir: Path
    self_user_id: int
    skip_quizzes: bool = False
    skip_submissions: bool = False
    skip_groups: bool = False
    file_workers: int = 4

    def run(self) -> ArchiveStats:
        stats = ArchiveStats(self.course_id, self.course_name)
        self.course_dir.mkdir(parents=True, exist_ok=True)
        ((self.course_dir / 'files') / 'by_id').mkdir(parents=True, exist_ok=True)

        discovered_file_ids: set[str] = set()
        # Track HTML bodies we've parsed, so file download can resolve them later.
        captured_htmls: list[str] = []

        def add_html(*chunks: str | None) -> None:
            for c in chunks:
                if c:
                    captured_htmls.append(c)

        # 1. Course meta + syllabus
        status, course = self.client.get_json(
            f'/api/v1/courses/{self.course_id}'
            '?include[]=syllabus_body&include[]=teachers&include[]=term&include[]=tabs'
        )
        if _is_ok(status) and isinstance(course, dict):
            self._write_json('course.json', course)
            if course.get('syllabus_body'):
                (self.course_dir / 'syllabus.html').write_text(course['syllabus_body'])
                add_html(course['syllabus_body'])
            stats.counts['course'] = 1
        else:
            stats.errors.append(f'course meta: HTTP {status}')

        # 2. Tabs (we already have them in course; but fetch standalone too for clarity)
        status, tabs = self.client.get_paginated(
            f'/api/v1/courses/{self.course_id}/tabs'
        )
        if _is_ok(status):
            self._write_json('tabs.json', tabs)
            stats.counts['tabs'] = len(tabs)

        # 3. Folders metadata (so we can map file_id → folder full_name later)
        status, folders = self.client.get_paginated(
            f'/api/v1/courses/{self.course_id}/folders?per_page=100'
        )
        folder_paths: dict[int, str] = {}
        if _is_ok(status):
            self._write_json('folders.json', folders)
            folder_paths = {
                f['id']: f.get('full_name') or f.get('name') or '' for f in folders
            }
            stats.counts['folders'] = len(folders)

        # 4. Modules with items + content_details
        status, modules = self.client.get_paginated(
            f'/api/v1/courses/{self.course_id}/modules'
            '?per_page=100&include[]=items&include[]=content_details'
        )
        if _is_ok(status):
            self._write_json('modules.json', modules)
            stats.counts['modules'] = len(modules)
            for m in modules:
                for it in (m.get('items') or []):
                    if it.get('type') == 'File' and it.get('content_id'):
                        discovered_file_ids.add(str(it['content_id']))
        else:
            stats.errors.append(f'modules: HTTP {status}')

        # 5. Assignments (full bodies + my submissions + file refs)
        assignments_dir = self.course_dir / 'assignments'
        assignments_dir.mkdir(exist_ok=True)
        status, assignments = self.client.get_paginated(
            f'/api/v1/courses/{self.course_id}/assignments?per_page=100'
        )
        if _is_ok(status):
            self._write_json('assignments/_index.json', assignments)
            stats.counts['assignments'] = len(assignments)
            for a in assignments:
                aid, name = a['id'], a.get('name') or f'assignment-{aid}'
                base = f'{aid}__{slugify(name)}'
                self._write_json(f'assignments/{base}.json', a)
                if a.get('description'):
                    (assignments_dir / f'{base}.html').write_text(a['description'])
                    discovered_file_ids |= discover_file_ids(a['description'])
                add_html(a.get('description'))
                # My submission for this assignment, including instructor
                # feedback (comments + rubric_assessment) which is otherwise lost.
                if not self.skip_submissions:
                    sub_path = (
                        f'/api/v1/courses/{self.course_id}/assignments/{aid}'
                        f'/submissions/self'
                        f'?include[]=submission_history'
                        f'&include[]=submission_comments'
                        f'&include[]=rubric_assessment'
                    )
                    s_status, sub = self.client.get_json(sub_path)
                    if _is_ok(s_status) and isinstance(sub, dict):
                        self._write_json(f'assignments/{base}__submission.json', sub)
                        for att in (sub.get('attachments') or []):
                            self._download_submission_attachment(
                                att, base, stats, refresh_path=sub_path,
                            )
                        for hist in (sub.get('submission_history') or []):
                            for att in (hist.get('attachments') or []):
                                self._download_submission_attachment(
                                    att, base, stats, refresh_path=sub_path,
                                )
        else:
            stats.errors.append(f'assignments: HTTP {status}')

        # 6. Pages (full bodies + file refs)
        if self._tab_enabled(course, 'pages'):
            pages_dir = self.course_dir / 'pages'
            pages_dir.mkdir(exist_ok=True)
            status, pages = self.client.get_paginated(
                f'/api/v1/courses/{self.course_id}/pages?per_page=100'
            )
            if _is_ok(status):
                self._write_json('pages/_index.json', pages)
                stats.counts['pages'] = len(pages)
                for p in pages:
                    url = p.get('url') or str(p.get('page_id'))
                    f_status, full = self.client.get_json(
                        f'/api/v1/courses/{self.course_id}/pages/{url}'
                    )
                    if _is_ok(f_status) and isinstance(full, dict):
                        slug = fs_safe(url)
                        self._write_json(f'pages/{slug}.json', full)
                        if full.get('body'):
                            (pages_dir / f'{slug}.html').write_text(full['body'])
                            discovered_file_ids |= discover_file_ids(full['body'])
                        add_html(full.get('body'))
            else:
                stats.errors.append(f'pages: HTTP {status}')

        # 7. Quizzes
        if not self.skip_quizzes:
            quizzes_dir = self.course_dir / 'quizzes'
            quizzes_dir.mkdir(exist_ok=True)
            status, quizzes = self.client.get_paginated(
                f'/api/v1/courses/{self.course_id}/quizzes?per_page=100'
            )
            if _is_ok(status):
                self._write_json('quizzes/_index.json', quizzes)
                stats.counts['quizzes'] = len(quizzes)
                for q in quizzes:
                    qid, name = q['id'], q.get('title') or f'quiz-{qid}'
                    base = f'{qid}__{slugify(name)}'
                    self._write_json(f'quizzes/{base}.json', q)
                    if q.get('description'):
                        discovered_file_ids |= discover_file_ids(q['description'])
                    add_html(q.get('description'))
                    # questions (often locked)
                    qs_status, qs = self.client.get_paginated(
                        f'/api/v1/courses/{self.course_id}/quizzes/{qid}/questions'
                        '?per_page=100'
                    )
                    if _is_ok(qs_status):
                        self._write_json(f'quizzes/{base}__questions.json', qs)

        # 8. Discussions (with full thread view)
        disc_dir = self.course_dir / 'discussions'
        disc_dir.mkdir(exist_ok=True)
        status, discussions = self.client.get_paginated(
            f'/api/v1/courses/{self.course_id}/discussion_topics?per_page=100'
        )
        if _is_ok(status):
            self._write_json('discussions/_index.json', discussions)
            stats.counts['discussions'] = len(discussions)
            for d in discussions:
                did, name = d['id'], d.get('title') or f'discussion-{did}'
                base = f'{did}__{slugify(name)}'
                self._write_json(f'discussions/{base}.json', d)
                add_html(d.get('message'))
                discovered_file_ids |= discover_file_ids(d.get('message'))
                v_status, view = self.client.get_json(
                    f'/api/v1/courses/{self.course_id}/discussion_topics/{did}/view'
                )
                if _is_ok(v_status):
                    self._write_json(f'discussions/{base}__view.json', view)
                    # Walk replies for inline file refs
                    if isinstance(view, dict):
                        for entry in (view.get('view') or []):
                            for reply in entry.get('replies', []) or []:
                                add_html(reply.get('message'))
                                discovered_file_ids |= discover_file_ids(
                                    reply.get('message')
                                )

        # 9. Announcements
        ann_dir = self.course_dir / 'announcements'
        ann_dir.mkdir(exist_ok=True)
        status, announcements = self.client.get_paginated(
            f'/api/v1/courses/{self.course_id}/discussion_topics'
            '?only_announcements=true&per_page=100'
        )
        if _is_ok(status):
            self._write_json('announcements/_index.json', announcements)
            stats.counts['announcements'] = len(announcements)
            for a in announcements:
                did, name = a['id'], a.get('title') or f'announcement-{did}'
                base = f'{did}__{slugify(name)}'
                self._write_json(f'announcements/{base}.json', a)
                add_html(a.get('message'))
                discovered_file_ids |= discover_file_ids(a.get('message'))

        # 10. Rubrics
        status, rubrics = self.client.get_paginated(
            f'/api/v1/courses/{self.course_id}/rubrics?per_page=100'
        )
        if _is_ok(status):
            self._write_json('rubrics.json', rubrics)
            stats.counts['rubrics'] = len(rubrics)

        # 11. Groups (metadata only)
        if not self.skip_groups:
            status, groups = self.client.get_paginated(
                f'/api/v1/courses/{self.course_id}/groups?per_page=100'
            )
            if _is_ok(status):
                self._write_json('groups.json', groups)
                stats.counts['groups'] = len(groups)

        # 12. Files: download every discovered file ID + build index
        files_index: dict[str, dict] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.file_workers) as ex:
            futures = {
                ex.submit(self._fetch_and_download_file, fid, folder_paths): fid
                for fid in discovered_file_ids
            }
            for fut in concurrent.futures.as_completed(futures):
                fid = futures[fut]
                try:
                    info, action, nbytes = fut.result()
                    if info:
                        files_index[fid] = info
                    if action == 'new':
                        stats.files_new += 1
                        stats.bytes_downloaded += nbytes
                    elif action == 'skip':
                        stats.files_skipped += 1
                    elif action == 'fail':
                        stats.files_failed += 1
                except Exception as e:
                    stats.files_failed += 1
                    stats.errors.append(f'file {fid}: {e!r}')
        self._write_json('files/files_index.json', files_index)
        stats.counts['files'] = len(files_index)

        # 13. State
        self._write_json('_state.json', {
            'last_run': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'counts': stats.counts,
            'files_new': stats.files_new,
            'files_skipped': stats.files_skipped,
            'files_failed': stats.files_failed,
            'bytes_downloaded': stats.bytes_downloaded,
            'errors': stats.errors,
        })
        return stats

    # -- helpers --

    def _write_json(self, rel: str, data: Any) -> None:
        p = self.course_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + '.tmp')
        tmp.write_text(json.dumps(data, indent=2, default=str))
        tmp.rename(p)

    def _tab_enabled(self, course: dict | None, label_substring: str) -> bool:
        if not isinstance(course, dict):
            return True  # be permissive: try the endpoint
        for t in course.get('tabs') or []:
            if label_substring in (t.get('label', '').lower()
                                   + ' ' + t.get('id', '').lower()):
                return not t.get('hidden', False)
        return True

    def _fetch_and_download_file(
        self, file_id: str, folder_paths: dict[int, str]
    ) -> tuple[dict | None, str, int]:
        status, meta = self.client.get_json(
            f'/api/v1/courses/{self.course_id}/files/{file_id}'
        )
        if not _is_ok(status) or not isinstance(meta, dict):
            return None, 'fail', 0
        display = meta.get('display_name') or meta.get('filename') or f'file-{file_id}'
        url = meta.get('url')
        if not url:
            return ({**meta, 'archive_status': 'no_url'}, 'fail', 0)
        dest = self.course_dir / 'files' / 'by_id' / f'{file_id}__{fs_safe(display)}'
        expected_size = meta.get('size')
        # Skip when local size matches AND local file is at least as new as Canvas's
        # updated_at. Catches re-uploads that don't change size.
        if dest.exists() and expected_size and dest.stat().st_size == expected_size:
            updated = _parse_iso(meta.get('updated_at'))
            local_mtime = dest.stat().st_mtime
            if updated is None or local_mtime >= updated:
                return self._file_info(meta, folder_paths, dest), 'skip', 0
        partial = dest.with_suffix(dest.suffix + '.partial')
        if partial.exists():
            partial.unlink()
        r = subprocess.run(
            ['curl', '-fL', '-sS', '-C', '-', '--retry', '5', '--retry-delay', '2',
             '-o', str(partial), url],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            if partial.exists():
                partial.unlink()
            return ({**meta, 'archive_status': 'download_failed',
                     'archive_error': r.stderr.strip()[-200:]}, 'fail', 0)
        if expected_size and partial.stat().st_size != expected_size:
            partial.unlink()
            return ({**meta, 'archive_status': 'size_mismatch'}, 'fail', 0)
        partial.rename(dest)
        size = dest.stat().st_size
        return self._file_info(meta, folder_paths, dest), 'new', size

    def _file_info(
        self, meta: dict, folder_paths: dict[int, str],
        dest: Path, sha: str | None = None,
    ) -> dict:
        return {
            'id': meta.get('id'),
            'display_name': meta.get('display_name'),
            'filename': meta.get('filename'),
            'size': meta.get('size'),
            'updated_at': meta.get('updated_at'),
            'content_type': meta.get('content-type'),
            'folder_id': meta.get('folder_id'),
            'folder_path': folder_paths.get(meta.get('folder_id'), ''),
            'local_path': str(dest.relative_to(self.course_dir)),
        }

    def _download_submission_attachment(
        self, att: dict, assignment_base: str, stats: ArchiveStats,
        refresh_path: str | None = None,
    ) -> None:
        url = att.get('url')
        if not url:
            return
        sub_dir = self.course_dir / 'submissions'
        sub_dir.mkdir(exist_ok=True)
        name = att.get('display_name') or att.get('filename') or f'att-{att.get("id")}'
        dest = sub_dir / f'{assignment_base}__{fs_safe(name)}'
        expected = att.get('size')
        if dest.exists() and expected and dest.stat().st_size == expected:
            stats.files_skipped += 1
            return

        att_id = att.get('id')

        def _attempt(target_url: str) -> int:
            partial = dest.with_suffix(dest.suffix + '.partial')
            if partial.exists():
                partial.unlink()
            r = subprocess.run(
                ['curl', '-fL', '-sS', '-C', '-', '--retry', '5', '--retry-delay', '2',
                 '-o', str(partial), target_url],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                if partial.exists():
                    partial.unlink()
                return r.returncode
            partial.rename(dest)
            return 0

        rc = _attempt(url)
        if rc != 0 and refresh_path:
            # Presigned URL likely expired during a long run — re-fetch and retry once.
            s, fresh = self.client.get_json(refresh_path)
            if _is_ok(s) and isinstance(fresh, dict):
                pool = list(fresh.get('attachments') or [])
                for h in fresh.get('submission_history') or []:
                    pool.extend(h.get('attachments') or [])
                fresh_url = next(
                    (a.get('url') for a in pool if a.get('id') == att_id and a.get('url')),
                    None,
                )
                if fresh_url:
                    rc = _attempt(fresh_url)
        if rc != 0:
            stats.files_failed += 1
            stats.errors.append(f'submission attachment {name}: curl rc={rc}')
            return
        stats.files_new += 1
        stats.bytes_downloaded += dest.stat().st_size


# -------- main --------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', required=True, help='archive root directory')
    ap.add_argument('--course-ids', help='comma-separated subset')
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--skip-quizzes', action='store_true')
    ap.add_argument('--skip-submissions', action='store_true')
    ap.add_argument('--skip-groups', action='store_true')
    args = ap.parse_args()

    client = CanvasClient(token=get_canvas_token())
    out_root = Path(args.output)
    out_root.mkdir(parents=True, exist_ok=True)

    try:
        self_status, self_user = client.get_json('/api/v1/users/self')
    except CanvasAuthError as e:
        sys.exit(str(e))
    if not _is_ok(self_status):
        sys.exit(f'cannot fetch self user: HTTP {self_status}')
    self_user_id = self_user['id']

    # Course list — by default, available + completed (so concluded MSDS terms
    # are mirrored too).
    try:
        if args.course_ids:
            wanted = {int(x) for x in args.course_ids.split(',')}
            courses = []
            for cid in wanted:
                s, c = client.get_json(f'/api/v1/courses/{cid}')
                if _is_ok(s) and isinstance(c, dict):
                    courses.append(c)
                else:
                    print(f'  [skip] course {cid}: HTTP {s}', flush=True)
        else:
            _, courses = client.get_paginated(
                '/api/v1/courses?per_page=100'
                '&state[]=available&state[]=completed'
            )
            courses = [c for c in courses if 'name' in c]
    except CanvasAuthError as e:
        sys.exit(str(e))

    print(f'phase C | {len(courses)} courses → {out_root}', flush=True)
    rollup = []
    fatal_failures = 0
    for c in courses:
        cid, cname = c['id'], c.get('name', f'course-{c["id"]}')
        course_dir = out_root / f'{cid}-{slugify(cname)}'
        try:
            arch = CourseArchiver(
                client=client,
                course_id=cid,
                course_name=cname,
                course_dir=course_dir,
                self_user_id=self_user_id,
                skip_quizzes=args.skip_quizzes,
                skip_submissions=args.skip_submissions,
                skip_groups=args.skip_groups,
                file_workers=args.workers,
            )
            stats = arch.run()
            mb = stats.bytes_downloaded / 1e6
            print(f'  [ok] {cid}  files +{stats.files_new}/skip {stats.files_skipped}'
                  f'/fail {stats.files_failed}  ({mb:>6.1f} MB)  {cname}'
                  + (f'  [{len(stats.errors)} errs]' if stats.errors else ''),
                  flush=True)
            rollup.append({
                'course_id': cid, 'name': cname,
                'counts': stats.counts,
                'files_new': stats.files_new,
                'files_skipped': stats.files_skipped,
                'files_failed': stats.files_failed,
                'mb_downloaded': round(mb, 1),
                'errors_count': len(stats.errors),
            })
            if stats.files_failed:
                fatal_failures += 1
        except CanvasAuthError as e:
            sys.exit(str(e))
        except Exception as e:
            print(f'  [!!] {cid}  FAILED: {e!r}', flush=True)
            rollup.append({'course_id': cid, 'name': cname, 'fatal': repr(e)})
            fatal_failures += 1

    tmp = out_root / '_rollup.json.tmp'
    tmp.write_text(json.dumps(rollup, indent=2))
    tmp.rename(out_root / '_rollup.json')
    total_new = sum(r.get('files_new', 0) for r in rollup)
    total_mb = sum(r.get('mb_downloaded', 0) for r in rollup)
    print(f'done | {total_new} new files | {total_mb:.1f} MB | '
          f'{fatal_failures} courses with failures')
    sys.exit(1 if fatal_failures else 0)


if __name__ == '__main__':
    main()
