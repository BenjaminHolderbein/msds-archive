#!/usr/bin/env python3
"""
Resumable, parallel downloader for a Panopto manifest.json.

Usage:
  ./download.py <manifest.json> <output_dir> [--mode <mode>] [--workers N]

Modes (default: audio+screen):
  audio+screen   download podcast → extract audio.m4a → delete podcast;
                 download the screen-capture stream as screen.mp4;
                 skip the camera stream entirely. Best for future ASR /
                 visual indexing at modest storage cost (~675 MB/hr).
  podcast        download just podcast.mp4 (smallest, watchable composite,
                 ~470 MB/hr). Camera-overlay can obscure slide content.
  all            podcast.mp4 + every secondary stream (cam + screen).
                 Highest fidelity, biggest storage (~2.4 GB/hr).

Per-session layout:
  <output_dir>/<session_id>__<sanitized_name>/
    audio.m4a              (audio+screen mode)
    screen.mp4             (audio+screen mode, when a screen stream exists)
    podcast.mp4            (podcast or all mode; also kept by audio+screen
                            when no separate screen stream exists)
    stream_<n>.mp4         (all mode only)
    captions_<lang>.srt    (one per available caption language)
    meta.json              (the manifest entry as written by Phase A)
"""
from __future__ import annotations

import argparse, json, os, re, subprocess, sys, concurrent.futures
from pathlib import Path

SAFE = re.compile(r'[^\w\-. ]+')

# Panopto's URL pattern for the screen capture stream contains `.object.hls`;
# the camera stream does not. Detected at download time so we don't need
# Phase A to label streams.
SCREEN_STREAM_MARKER = '.object.hls'


def sanitize(name: str) -> str:
    return SAFE.sub('_', name).strip()[:120] or 'unnamed'


def head_size(url: str) -> int | None:
    """Return Content-Length, or None if HEAD didn't surface one."""
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


def _atomic(dest: Path) -> Path:
    return dest.with_suffix(dest.suffix + '.partial')


def download_mp4(url: str, dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    expected = head_size(url)
    if expected is None:
        return f'FAIL {dest.name}: HEAD did not return Content-Length'
    if expected <= 0:
        return f'FAIL {dest.name}: Content-Length={expected}'
    if dest.exists() and dest.stat().st_size == expected:
        return f'skip {dest.name} ({expected:,} B)'

    partial = _atomic(dest)
    if dest.exists() and dest.stat().st_size != expected:
        dest.unlink()
    r = subprocess.run(
        ['curl', '-fL', '-C', '-', '--retry', '5', '--retry-delay', '2',
         '-o', str(partial), url],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return f'FAIL {dest.name}: {r.stderr.strip()[-200:]}'
    if not partial.exists() or partial.stat().st_size != expected:
        actual = partial.stat().st_size if partial.exists() else 0
        return f'FAIL {dest.name}: size mismatch (got {actual:,}, expected {expected:,})'
    partial.rename(dest)
    return f'ok   {dest.name} ({dest.stat().st_size:,} B)'


def download_hls(url: str, dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return f'skip {dest.name}'
    partial = _atomic(dest)
    if partial.exists():
        partial.unlink()
    r = subprocess.run(
        ['ffmpeg', '-y', '-loglevel', 'error', '-i', url, '-c', 'copy',
         '-bsf:a', 'aac_adtstoasc', '-f', 'mp4', str(partial)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        if partial.exists():
            partial.unlink()
        return f'FAIL {dest.name}: {r.stderr.strip()[-200:]}'
    partial.rename(dest)
    return f'ok   {dest.name} ({dest.stat().st_size:,} B)'


def extract_audio(src: Path, dest: Path) -> str:
    """Copy the audio track of `src` to `dest` losslessly (no re-encode)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return f'skip {dest.name}'
    partial = _atomic(dest)
    if partial.exists():
        partial.unlink()
    r = subprocess.run(
        ['ffmpeg', '-y', '-loglevel', 'error', '-i', str(src),
         '-vn', '-c:a', 'copy', '-f', 'mp4', str(partial)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        if partial.exists():
            partial.unlink()
        return f'FAIL {dest.name}: {r.stderr.strip()[-200:]}'
    partial.rename(dest)
    return f'ok   {dest.name} ({dest.stat().st_size:,} B)'


def write_caption(srt: str, dest: Path) -> str:
    if dest.exists() and dest.stat().st_size == len(srt.encode('utf-8')):
        return f'skip {dest.name}'
    partial = _atomic(dest)
    partial.write_text(srt, encoding='utf-8')
    partial.rename(dest)
    return f'ok   {dest.name} ({dest.stat().st_size:,} B)'


def _find_screen_stream(streams: list[dict]) -> dict | None:
    for s in streams or []:
        if SCREEN_STREAM_MARKER in (s.get('url') or ''):
            return s
    return None


def process(session: dict, out_root: Path, mode: str) -> list[str]:
    sid = session.get('id', 'unknown')
    if not session.get('podcast'):
        return [f'-- skip {sid} (no stream)']
    folder = out_root / f'{sid}__{sanitize(session.get("name", "unnamed"))}'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'meta.json').write_text(json.dumps(session, indent=2))
    results: list[str] = []

    # Captions are independent of mode and tiny — always write.
    for c in session.get('captions') or []:
        if isinstance(c, dict) and c.get('srt'):
            lang = sanitize(str(c.get('language', 'unknown')))
            results.append(write_caption(c['srt'], folder / f'captions_{lang}.srt'))

    podcast_path = folder / 'podcast.mp4'
    audio_path = folder / 'audio.m4a'
    screen_path = folder / 'screen.mp4'

    if mode == 'podcast':
        results.insert(0, download_mp4(session['podcast'], podcast_path))
        return results

    if mode == 'all':
        results.insert(0, download_mp4(session['podcast'], podcast_path))
        for i, s in enumerate(session.get('streams') or []):
            results.append(download_hls(s['url'], folder / f'stream_{i}.mp4'))
        return results

    # mode == 'audio+screen' (default)
    screen_stream = _find_screen_stream(session.get('streams') or [])

    # Audio: extract from podcast (smallest source). Skip if already present.
    if audio_path.exists() and audio_path.stat().st_size > 0:
        results.append(f'skip {audio_path.name}')
    else:
        # Need the podcast on disk transiently to extract from. Don't
        # re-download if it's already there from a prior run.
        had_podcast = podcast_path.exists() and podcast_path.stat().st_size > 0
        if not had_podcast:
            r = download_mp4(session['podcast'], podcast_path)
            results.append(r)
            if r.startswith('FAIL'):
                return results
        results.append(extract_audio(podcast_path, audio_path))
        # Free the ~500 MB the podcast cost us, *only* if we have a screen
        # stream to keep the visuals in. If there is no screen stream the
        # podcast is the only visual we'll ever have for this session.
        if audio_path.exists() and audio_path.stat().st_size > 0 and screen_stream:
            podcast_path.unlink(missing_ok=True)

    if screen_stream:
        results.append(download_hls(screen_stream['url'], screen_path))
    elif not (audio_path.exists() and audio_path.stat().st_size > 0):
        # Pathological: no screen and no audio either. Note it.
        results.append(f'WARN {sid}: no screen stream and no audio extracted')
    # else: no screen stream, but we kept the podcast as fallback visuals

    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('manifest')
    ap.add_argument('output_dir')
    ap.add_argument('--mode', choices=['audio+screen', 'podcast', 'all'],
                    default='audio+screen',
                    help='which artifacts to keep per session (default: audio+screen)')
    # Back-compat alias for the old --include-streams flag.
    ap.add_argument('--include-streams', action='store_true',
                    help='deprecated; equivalent to --mode all')
    ap.add_argument('--workers', type=int, default=4)
    args = ap.parse_args()

    mode = 'all' if args.include_streams else args.mode

    data = json.loads(Path(args.manifest).read_text())
    out_root = Path(args.output_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    sessions = data.get('manifest', [])
    print(f'{len(sessions)} sessions → {out_root}  (mode={mode})', flush=True)
    failures = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(process, s, out_root, mode): s for s in sessions}
        for f in concurrent.futures.as_completed(futures):
            for line in f.result():
                if line.startswith('FAIL'):
                    failures += 1
                print(line, flush=True)
    print(f'done | {failures} failed')
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
