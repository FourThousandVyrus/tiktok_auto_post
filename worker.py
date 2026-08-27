#!/usr/bin/env python3
"""
TikTok Cloud Poster - runs on GitHub Actions.

Each run:
  1. Reads queue records from the R2 bucket (upload_manifest.json + upload state)
  2. Picks the next unposted video (oldest first)
  3. Downloads it from R2 to a temp file
  4. Direct-Posts it to TikTok via FILE_UPLOAD (chunked)
  5. Marks it posted in bucket state so the next run continues the queue

Required env (GitHub Secrets):
  R2_ACCOUNT_ID, R2_ACCESS_KEY, R2_SECRET_KEY,
  TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET, TIKTOK_REFRESH_TOKEN

Optional env (repo variables):
  BUCKET (default tt-videos), HASHTAG_SUFFIX, PRIVACY_LEVEL (default SELF_ONLY)
"""

import json
import math
import os
import sys
import tempfile
import time
from datetime import datetime, timezone

import boto3
import requests

sys.stdout.reconfigure(encoding='utf-8')

R2_ENDPOINT_TMPL = 'https://{account_id}.r2.cloudflarestorage.com'
API = 'https://open.tiktokapis.com/v2'
CHUNK_SIZE = 32 * 1024 * 1024          # 32 MB chunks (TikTok max is 64 MB)
MAX_TITLE_LEN = 2000
POLL_ATTEMPTS = 30
POLL_INTERVAL = 10

UNIVERSAL = ['#fyp', '#foryou', '#viral']
CATEGORY_TAGS = [
    (r'kdrama|korean drama|k-drama',      ['#kdrama', '#koreanDrama']),
    (r'romance|love|romantic',             ['#romance', '#lovestory']),
    (r'action|fight|battle|war|revenge',   ['#action', '#thriller']),
    (r'baby|child|kid|mother|father',      ['#family', '#heartwarming']),
    (r'funny|comedy|laugh|hilarious',      ['#funny', '#comedy']),
    (r'drama|story|film|movie|scene',      ['#drama', '#shortfilm']),
    (r'mystery|secret|suspense',           ['#mystery', '#mustwatch']),
]
FALLBACK = ['#movierecommendation', '#viralvideo', '#trending']


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------- storage ----------------

def r2_client():
    return boto3.client(
        's3',
        endpoint_url=R2_ENDPOINT_TMPL.format(account_id=os.environ['R2_ACCOUNT_ID']),
        aws_access_key_id=os.environ['R2_ACCESS_KEY'],
        aws_secret_access_key=os.environ['R2_SECRET_KEY'],
        region_name='auto',
    )


def get_json(s3, bucket, key):
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        return json.loads(obj['Body'].read().decode('utf-8'))
    except s3.exceptions.NoSuchKey:
        return None


def put_json(s3, bucket, key, data):
    s3.put_object(Bucket=bucket, Key=key,
                  Body=json.dumps(data, indent=2, ensure_ascii=False).encode('utf-8'),
                  ContentType='application/json')


# ---------------- tiktok auth ----------------

def refresh_access_token(state):
    cur = state.get('refresh_token') or os.environ['TIKTOK_REFRESH_TOKEN']
    resp = requests.post(f'{API}/oauth/token/', data={
        'grant_type': 'refresh_token',
        'client_key': os.environ['TIKTOK_CLIENT_KEY'],
        'client_secret': os.environ['TIKTOK_CLIENT_SECRET'],
        'refresh_token': cur,
    }, timeout=30)
    data = resp.json()
    token = data.get('access_token')
    if not token:
        raise RuntimeError(f"Token refresh failed: {data}")
    state['token_cache'] = {
        'access_token': token,
        'expires_at': time.time() + int(data.get('expires_in', 7200)) - 600,
    }
    new_rt = data.get('refresh_token')
    if new_rt and new_rt != cur:
        state['refresh_token'] = new_rt
        log("Refresh token rotated (new value saved to bucket state)")
    log("Access token refreshed")
    return token


def get_token(state):
    cached = state.get('token_cache')
    if cached and cached.get('expires_at', 0) > time.time():
        return cached['access_token']
    return refresh_access_token(state)


# ---------------- posting ----------------

def get_hashtags(caption):
    import re
    result = list(UNIVERSAL)
    for pattern, tags in CATEGORY_TAGS:
        if re.search(pattern, caption, re.I):
            for t in tags:
                if t not in result and len(result) < 5:
                    result.append(t)
    for t in FALLBACK:
        if t not in result and len(result) < 5:
            result.append(t)
    return ' '.join(result[:5])


def build_title(caption, order):
    tags = get_hashtags(caption or '')
    title = f"{caption} {tags}".strip() if caption else tags
    return title[:MAX_TITLE_LEN]


def init_direct_post(token, size, title, privacy_level):
    body = {
        'post_info': {
            'title': title,
            'privacy_level': privacy_level,
            'disable_duet': False,
            'disable_comment': False,
            'disable_stitch': False,
            'video_cover_timestamp_ms': 0,
        },
        'source_info': {
            'source': 'FILE_UPLOAD',
            'video_size': size,
            'chunk_size': CHUNK_SIZE,
            'total_chunk_count': max(1, math.ceil(size / CHUNK_SIZE)),
        },
    }
    r = requests.post(f'{API}/post/publish/video/init/',
                      headers={'Authorization': f'Bearer {token}',
                               'Content-Type': 'application/json; charset=UTF-8'},
                      json=body, timeout=60)
    data = r.json()
    err = data.get('error', {})
    if err.get('code') != 'ok':
        raise RuntimeError(f"init failed HTTP {r.status_code}: {json.dumps(data)[:300]}")
    d = data['data']
    return d['publish_id'], d.get('upload_url')


def upload_chunks(upload_url, path, size):
    with open(path, 'rb') as f:
        sent = 0
        part = 1
        while sent < size:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            end = sent + len(chunk) - 1
            headers = {
                'Content-Range': f'bytes {sent}-{end}/{size}',
                'Content-Type': 'video/mp4',
            }
            r = requests.put(upload_url, data=chunk, headers=headers, timeout=300)
            if r.status_code not in (200, 201, 206):
                raise RuntimeError(f"chunk {part} failed: HTTP {r.status_code} {r.text[:200]}")
            log(f"  uploaded chunk {part} ({len(chunk)/1024**2:.1f} MB)")
            sent += len(chunk)
            part += 1


def poll_status(token, publish_id):
    for attempt in range(POLL_ATTEMPTS):
        r = requests.post(f'{API}/post/publish/status/fetch/',
                          headers={'Authorization': f'Bearer {token}',
                                   'Content-Type': 'application/json'},
                          json={'publish_id': publish_id}, timeout=30)
        data = r.json()
        status = data.get('data', {}).get('status', '')
        if status == 'PUBLISH_STATUS_FAILED':
            raise RuntimeError(f"TikTok rejected post: {json.dumps(data)[:300]}")
        if status in ('PUBLISH_STATUS_PUBLISHED', 'PUBLISH_STATUS_SENT_TO_INBOX',
                      'SELF_ONLY_PUBLISHED'):
            log(f"Post confirmed: {status}")
            return True
        time.sleep(POLL_INTERVAL)
    raise RuntimeError(f"Timed out waiting for publish confirmation (publish_id={publish_id})")


# ---------------- main ----------------

def main():
    required = ['R2_ACCOUNT_ID', 'R2_ACCESS_KEY', 'R2_SECRET_KEY',
                'TIKTOK_CLIENT_KEY', 'TIKTOK_CLIENT_SECRET', 'TIKTOK_REFRESH_TOKEN']
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"Missing secrets: {missing}")
        sys.exit(1)

    bucket = os.environ.get('BUCKET', 'tt-videos')
    privacy = os.environ.get('PRIVACY_LEVEL', 'SELF_ONLY')

    s3 = r2_client()
    manifest = get_json(s3, bucket, 'state/upload_manifest.json')
    upload_state = get_json(s3, bucket, 'state/r2_upload_state.json') or {'uploaded': {}}
    post_state = get_json(s3, bucket, 'state/post_state.json') or {'posted': {}, 'failed': {}}

    uploaded_keys = set(upload_state.get('uploaded', {}))
    posted_keys = set(post_state.get('posted', {}).keys())

    # self-healing cleanup: videos already posted should not occupy bucket space
    cleaned = 0
    for key in sorted(posted_keys & uploaded_keys,
                      key=lambda k: upload_state['uploaded'][k].get('order', 0)):
        try:
            s3.delete_object(Bucket=bucket, Key=key)
            del upload_state['uploaded'][key]
            uploaded_keys.discard(key)
            cleaned += 1
        except Exception as e:
            log(f"cleanup skipped {key}: {e}")
    if cleaned:
        put_json(s3, bucket, 'state/r2_upload_state.json', upload_state)
        log(f"Cleaned {cleaned} previously-posted video(s) from the bucket")

    next_video = None
    for v in manifest['videos']:
        if v['r2_key'] in posted_keys:
            continue
        if v['r2_key'] not in uploaded_keys:
            continue
        next_video = v
        break

    if not next_video:
        log("Queue empty - every uploaded video has been posted.")
        sys.exit(0)

    log(f"Next up [{next_video['order']}/{len(manifest['videos'])}]: "
        f"{next_video['r2_key']} - {(next_video['caption'] or '(no caption)')[:60]}")

    tmp = tempfile.NamedTemporaryFile(suffix='.mp4', delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        s3.download_file(bucket, next_video['r2_key'], tmp_path)
        size = os.path.getsize(tmp_path)
        log(f"Downloaded {size/1024**2:.1f} MB")

        token = get_token(post_state)
        title = build_title(next_video.get('caption', ''), next_video['order'])
        log(f"Title: {title[:80]}")

        publish_id, upload_url = init_direct_post(token, size, title, privacy)
        log(f"publish_id={publish_id}")

        if not upload_url:
            raise RuntimeError("No upload_url returned by TikTok init")

        upload_chunks(upload_url, tmp_path, size)
        poll_status(get_token(post_state), publish_id)

        post_state['posted'][next_video['r2_key']] = {
            'order': next_video['order'],
            'tweet_id': next_video.get('tweet_id'),
            'caption': next_video.get('caption', ''),
            'publish_id': publish_id,
            'privacy_level': privacy,
            'posted_at': datetime.now(timezone.utc).isoformat(),
        }
        post_state['failed'].pop(next_video['r2_key'], None)
    except Exception as e:
        post_state.setdefault('failed', {})[next_video['r2_key']] = str(e)[:300]
        log(f"FAILED: {e}")
        raise
    finally:
        put_json(s3, bucket, 'state/post_state.json', post_state)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    # posted successfully -> free its bucket space (PC archive keeps the original)
    try:
        s3.delete_object(bucket=bucket, Key=next_video['r2_key'])
        upload_state.get('uploaded', {}).pop(next_video['r2_key'], None)
        put_json(s3, bucket, 'state/r2_upload_state.json', upload_state)
        log(f"Freed {next_video['size_bytes']/1024**2:.1f} MB - "
            f"{next_video['r2_key']} deleted from bucket")
    except Exception as e:
        log(f"Note: auto-delete failed ({e}); next run's cleanup will retry")

    done = len(post_state['posted'])
    log(f"Done - {done} videos posted all-time.")


if __name__ == '__main__':
    main()
