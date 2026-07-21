import asyncio
import json
import logging
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Path to Python that has yt-dlp installed
_YT_DLP_PYTHON = os.getenv('YT_DLP_PYTHON', 'D:/tools/anaconda/python.exe')


# ---------------------------------------------------------------------------
# YouTube search via yt-dlp — structured JSON, no browser needed
# ---------------------------------------------------------------------------


def _extract_core_subject(topic: str) -> str:
    """Strip noise words from query for cleaner YouTube search."""
    noise = [
        "what are the", "what is the", "what is", "what are",
        "how to", "how do", "how does", "why does", "why do",
        "tell me about", "latest", "recent", "best", "top",
        "please", "explain", "compare", "difference between",
    ]
    text = topic.lower().strip().rstrip("?!.")
    for w in sorted(noise, key=len, reverse=True):
        if text.startswith(w):
            text = text[len(w):].strip()
            break
    # Cap at 6 words — YouTube search degrades with long queries
    words = text.split()
    if len(words) > 6:
        text = " ".join(words[:6])
    return text


async def search_youtube_ytdlp(query: str, max_results: int = 10) -> list[dict]:
    """Search YouTube via yt-dlp, return structured video metadata.

    Uses yt-dlp --dump-json ytsearchN:query for full metadata
    (views, likes, upload date, channel, duration, description).

    Args:
        query: Search query
        max_results: Max videos to return

    Returns:
        List of dicts with title, url, channel, views, likes,
        comments, duration, description, upload_date
    """
    core = _extract_core_subject(query)
    try:
        cmd = [
            _YT_DLP_PYTHON, "-m", "yt_dlp",
            "--ignore-config",
            "--no-cookies-from-browser",
            f"ytsearch{max_results}:{core}",
            "--dump-json",
            "--no-warnings",
            "--no-download",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)

        if proc.returncode != 0:
            logger.debug(f"yt-dlp search failed: {stderr.decode('utf-8', errors='replace')[:200]}")
            return []

        videos = []
        for line in stdout.decode("utf-8", errors="replace").strip().split("\n"):
            if not line.strip():
                continue
            try:
                v = json.loads(line)
            except json.JSONDecodeError:
                continue

            video_id = v.get("id", "")
            if not video_id:
                continue

            # Parse upload date
            upload_date = v.get("upload_date", "")  # YYYYMMDD
            date_str = ""
            if upload_date and len(upload_date) == 8:
                try:
                    date_str = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"
                except (ValueError, IndexError):
                    pass

            # Parse duration
            duration_s = v.get("duration", 0)
            duration_str = ""
            if duration_s:
                mins = int(duration_s) // 60
                secs = int(duration_s) % 60
                duration_str = f"{mins}:{secs:02d}"

            videos.append({
                "title": v.get("title", ""),
                "url": v.get("webpage_url", f"https://www.youtube.com/watch?v={video_id}"),
                "video_id": video_id,
                "channel": v.get("channel", v.get("uploader", "")),
                "views": int(v.get("view_count", 0) or 0),
                "likes": int(v.get("like_count", 0) or 0),
                "comments": int(v.get("comment_count", 0) or 0),
                "duration": duration_str,
                "description": (v.get("description", "") or "")[:500],
                "upload_date": date_str,
            })

        logger.info(f"youtube (yt-dlp): {len(videos)} results for '{core}'")
        return videos

    except asyncio.TimeoutError:
        logger.debug(f"yt-dlp search timeout for '{core}'")
        return []
    except Exception as e:
        logger.debug(f"yt-dlp search error: {e}")
        return []


def extract_transcript_highlights(transcript: str, topic: str, limit: int = 5) -> list[str]:
    """Extract quotable highlights from a transcript.

    Filters filler (subscribe, welcome back, etc.), scores sentences by
    specificity (numbers, proper nouns, topic relevance), returns top N.
    """
    if not transcript:
        return []

    sentences = re.split(r'(?<=[.!?])\s+', transcript)

    # Fallback for punctuation-free transcripts
    if len(sentences) <= 1 and len(transcript.split()) > 50:
        words = transcript.split()
        sentences = [' '.join(words[i:i+20]) for i in range(0, len(words), 20)]

    filler = [
        r"^(hey |hi |what's up|welcome back|in today's video|don't forget to)",
        r"(subscribe|like and comment|hit the bell|check out the link|down below)",
        r"^(so |and |but |okay |alright |um |uh )",
        r"(thanks for watching|see you (next|in the)|bye)",
    ]

    topic_words = [w.lower() for w in topic.lower().split() if len(w) > 2]

    candidates = []
    for sent in sentences:
        sent = sent.strip()
        words = sent.split()
        if len(words) < 8 or len(words) > 50:
            continue
        if any(re.search(p, sent, re.IGNORECASE) for p in filler):
            continue

        score = 0
        if re.search(r'\d', sent):
            score += 2
        if re.search(r'[A-Z][a-z]+', sent):
            score += 1
        if '?' in sent:
            score += 1
        sent_lower = sent.lower()
        if any(w in sent_lower for w in topic_words):
            score += 2

        candidates.append((score, sent))

    candidates.sort(key=lambda x: -x[0])
    return [sent for _, sent in candidates[:limit]]


def _parse_vtt_text(vtt_content: str) -> str:
    """Parse VTT/SRT subtitle content into plain text."""
    lines = vtt_content.split('\n')
    text_lines = []
    in_header = True
    for line in lines:
        line = line.strip()
        if in_header and not line:
            continue  # skip blank lines until first timestamp
        if '-->' in line:
            in_header = False
            continue
        if in_header:
            continue  # skip WEBVTT, Kind:, Language:, etc.
        if re.match(r'^\d+$', line):
            continue
        if line.startswith('position:') or line.startswith('align:'):
            continue
        # Remove timestamp tags like <00:00:01.040> and <c>...</c>
        line = re.sub(r'<[^>]+>', '', line)
        line = line.strip()
        if line:
            text_lines.append(line)
    # VTT has repeated lines (each line is a cumulative update), deduplicate
    unique = []
    for line in text_lines:
        if not unique or line != unique[-1]:
            unique.append(line)
    return ' '.join(unique)


async def fetch_youtube_transcript(video_id: str, max_chars: int = 400) -> Optional[str]:
    """Fetch YouTube video transcript via yt-dlp subprocess.

    Uses yt-dlp to download auto-generated subtitles, then parses the VTT file.

    Args:
        video_id: YouTube video ID (e.g., 's_o8dwzRlu4')
        max_chars: Maximum characters to return

    Returns:
        Transcript text or None
    """
    try:
        video_url = f'https://www.youtube.com/watch?v={video_id}'
        tmpdir = tempfile.mkdtemp()
        output_tpl = os.path.join(tmpdir, '%(id)s.%(lang)s.%(sub_ext)s')

        cmd = [
            _YT_DLP_PYTHON, '-m', 'yt_dlp',
            '--write-sub', '--write-auto-sub',
            '--sub-lang', 'zh,en',
            '--sub-format', 'txt',
            '--skip-download',
            '--no-warnings',
            '--no-check-certificates',
            '--output', output_tpl,
            video_url,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)

        if proc.returncode != 0:
            err = stderr.decode('utf-8', errors='replace')
            logger.debug(f"yt-dlp failed for {video_id}: {err[:200]}")
            return None

        # Find subtitle files
        files = os.listdir(tmpdir)
        sub_files = [f for f in files if f.endswith(('.vtt', '.srt', '.txt'))]
        if not sub_files:
            logger.debug(f"No subtitle file found for {video_id}")
            return None

        # Prefer Chinese, then English, then any
        sub_files.sort(key=lambda f: (
            0 if 'zh' in f.lower() else 1 if 'en' in f.lower() else 2,
            f
        ))

        for sub_file in sub_files:
            fpath = os.path.join(tmpdir, sub_file)
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    content = f.read()
                if not content or len(content) < 20:
                    continue
                text = _parse_vtt_text(content)
                if text and len(text) >= 20:
                    lang = 'en'
                    if 'zh' in sub_file.lower():
                        lang = 'zh'
                    elif 'en' in sub_file.lower():
                        lang = 'en'
                    logger.info(f"YouTube transcript extracted for {video_id} ({lang}, {len(text)} chars)")
                    return text[:max_chars]
            except Exception as e:
                logger.debug(f"Error reading subtitle file {sub_file}: {e}")
                continue

        return None

    except asyncio.TimeoutError:
        logger.debug(f"yt-dlp timeout for {video_id}")
        return None
    except Exception as e:
        logger.debug(f"fetch_youtube_transcript({video_id}): error: {e}")
        return None


async def fetch_bilibili_transcript(video_url: str, cdp=None, max_chars: int = 400) -> Optional[str]:
    """Fetch Bilibili video transcript via CDP browser.

    Bilibili subtitles require browser cookies for API access.
    """
    try:
        from app.sources.cdp_client import _get_cdp

        if cdp is None:
            cdp = await _get_cdp(source_name='bilibili')

        await cdp.send('Page.navigate', {'url': video_url})
        await asyncio.sleep(5)

        js = """(function() {
            try {
                var data = window.__INITIAL_STATE__ || {};
                var videoData = data.videoData || {};
                var cid = videoData.cid;
                if (!cid) return null;
                return 'https://api.bilibili.com/x/v1/dm/subtitle?cid=' + cid;
            } catch(e) {
                return null;
            }
        })()"""

        result = await cdp.send('Runtime.evaluate', {
            'expression': js,
            'returnByValue': True,
        })
        rval = result.get('result', {}).get('result', {})
        subtitle_api = rval.get('value')

        if not subtitle_api:
            return None

        download_js = f"""(async function() {{
            try {{
                var resp = await fetch("{subtitle_api}");
                var json = await resp.json();
                var subs = json.data || [];
                for (var i = 0; i < subs.length; i++) {{
                    var sub = subs[i];
                    if (sub.lan && (sub.lan.indexOf('zh') !== -1 || sub.lan.indexOf('en') !== -1)) {{
                        var subResp = await fetch(sub.subtitle_url);
                        var subJson = await subResp.json();
                        return subJson.body ? subJson.body.map(function(s) {{ return s.content; }}).join(' ') : null;
                    }}
                }}
                if (subs.length > 0) {{
                    var subResp = await fetch(subs[0].subtitle_url);
                    var subJson = await subResp.json();
                    return subJson.body ? subJson.body.map(function(s) {{ return s.content; }}).join(' ') : null;
                }}
                return null;
            }} catch(e) {{
                return null;
            }}
        }})()"""

        result = await cdp.send('Runtime.evaluate', {
            'expression': download_js,
            'awaitPromise': True,
            'returnByValue': True,
        })
        rval = result.get('result', {}).get('result', {})
        content = rval.get('value')

        if not content:
            return None

        text = content.strip()
        if len(text) < 20:
            return None

        return text[:max_chars]

    except Exception as e:
        logger.debug(f"fetch_bilibili_transcript({video_url[:60]}): error: {e}")
        return None


async def enrich_results_with_transcripts(results: list[dict], source: str, max_videos: int = 5):
    """Enrich search results with transcript text.

    For YouTube: uses yt-dlp subprocess to download auto-generated subtitles.
    For Bilibili: uses CDP browser to extract subtitle data from page.

    Args:
        results: List of result dicts with 'url' and 'content' fields
        source: Source name ('youtube' or 'bilibili')
        max_videos: Max number of videos to enrich (default: top 5)
    """
    if source == 'youtube':
        return await _enrich_youtube_transcripts(results, max_videos)
    elif source == 'bilibili':
        return await _enrich_bilibili_transcripts(results, max_videos)


async def _enrich_youtube_transcripts(results: list[dict], max_videos: int):
    """Enrich YouTube results with transcripts using yt-dlp (parallel)."""
    total_timeout = 45.0
    start = asyncio.get_event_loop().time()

    # Extract video IDs for all candidates
    candidates = []
    for r in results[:max_videos]:
        url = r.get('url', '')
        video_id_match = re.search(r'[?&]v=([A-Za-z0-9_-]{11})', url)
        if video_id_match:
            candidates.append((r, video_id_match.group(1)))

    if not candidates:
        return

    async def _fetch_one(r, video_id):
        try:
            transcript = await asyncio.wait_for(
                fetch_youtube_transcript(video_id),
                timeout=15.0,
            )
            return r, transcript
        except asyncio.TimeoutError:
            return r, None
        except Exception:
            return r, None

    # Run all transcript fetches in parallel
    tasks = [asyncio.create_task(_fetch_one(r, vid)) for r, vid in candidates]
    enriched_count = 0

    for coro in asyncio.as_completed(tasks):
        elapsed = asyncio.get_event_loop().time() - start
        if elapsed > total_timeout:
            logger.debug(f"YouTube transcript enrichment: timeout after {elapsed:.0f}s")
            break
        r, transcript = await coro
        if transcript:
            existing = r.get('content', '')
            prefix = ' | Transcript: ' if existing else 'Transcript: '
            r['content'] = existing + prefix + transcript[:400]
            enriched_count += 1
            logger.info(f"YouTube transcript enriched: {transcript[:80]}...")

    if enriched_count:
        logger.info(f"YouTube transcript enriched {enriched_count}/{min(max_videos, len(results))} results")


async def _enrich_bilibili_transcripts(results: list[dict], max_videos: int):
    """Enrich Bilibili results with transcripts using CDP (parallel)."""
    try:
        from app.sources.cdp_client import _get_cdp
        cdp = await _get_cdp(source_name='bilibili')
    except Exception as e:
        logger.debug(f"enrich_bilibili_transcripts: can't get CDP: {e}")
        return

    total_timeout = 30.0
    start = asyncio.get_event_loop().time()

    candidates = [r for r in results[:max_videos] if r.get('url')]
    if not candidates:
        return

    async def _fetch_one(r):
        try:
            transcript = await asyncio.wait_for(
                fetch_bilibili_transcript(r['url'], cdp=cdp),
                timeout=10.0,
            )
            return r, transcript
        except (asyncio.TimeoutError, Exception):
            return r, None

    tasks = [asyncio.create_task(_fetch_one(r)) for r in candidates]
    enriched_count = 0

    for coro in asyncio.as_completed(tasks):
        elapsed = asyncio.get_event_loop().time() - start
        if elapsed > total_timeout:
            logger.debug(f"Bilibili transcript enrichment: timeout after {elapsed:.0f}s")
            break
        r, transcript = await coro
        if transcript:
            existing = r.get('content', '')
            prefix = ' | Transcript: ' if existing else 'Transcript: '
            r['content'] = existing + prefix + transcript[:400]
            enriched_count += 1
            logger.info(f"Bilibili transcript enriched: {transcript[:80]}...")

    if enriched_count:
        logger.info(f"Bilibili transcript enriched {enriched_count}/{min(max_videos, len(results))} results")
