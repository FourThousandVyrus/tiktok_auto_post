#!/usr/bin/env python3
"""
One-time TikTok authorization helper (run on YOUR PC).

Opens the consent page in your browser, you approve, then paste back the
redirect URL. Exchanges the code for a refresh token that powers the cloud
worker for the next year.

Usage:
  python tiktok_auth_helper.py
"""

import base64
import hashlib
import json
import os
import secrets
import sys
import webbrowser
from urllib.parse import urlencode, urlparse, parse_qs

import requests

sys.stdout.reconfigure(encoding='utf-8')

API = 'https://open.tiktokapis.com/v2'
REDIRECT_URI = 'http://localhost'
SCOPES = 'user.info.basic,video.upload,video.publish'
TOKENS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tiktok_tokens.json')


def pkce_pair():
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    return verifier, challenge


def main():
    creds_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tiktok_app.json')
    if os.path.exists(creds_path):
        with open(creds_path, 'r', encoding='utf-8') as f:
            app = json.load(f)
        client_key = app['client_key']
        client_secret = app['client_secret']
        print(f"Loaded TikTok app credentials from tiktok_app.json")
    else:
        client_key = input('TikTok Client Key: ').strip()
        client_secret = input('TikTok Client Secret: ').strip()

    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)

    auth_url = ('https://www.tiktok.com/v2/auth/authorize/?' + urlencode({
        'client_key': client_key,
        'scope': SCOPES,
        'response_type': 'code',
        'redirect_uri': REDIRECT_URI,
        'state': state,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
    }))

    print("\nOpening browser for TikTok login/consent...")
    print("(If it doesn't open, paste this URL manually):\n")
    print(auth_url + "\n")
    webbrowser.open(auth_url)

    print("After approving, the page will fail to load (example.com).")
    print("Copy the FULL URL from your browser address bar and paste it here:")
    redirected = input('\n> ').strip()

    qs = parse_qs(urlparse(redirected).query)
    if qs.get('state', [None])[0] != state:
        sys.exit('ERROR: state mismatch - possible CSRF, aborting.')
    code = qs.get('code', [None])[0]
    if not code:
        sys.exit(f"ERROR: no code in URL: {redirected}")

    resp = requests.post(f'{API}/oauth/token/', data={
        'grant_type': 'authorization_code',
        'client_key': client_key,
        'client_secret': client_secret,
        'code': code,
        'redirect_uri': REDIRECT_URI,
        'code_verifier': verifier,
    }, timeout=30)
    data = resp.json()

    if not data.get('access_token'):
        sys.exit(f"Token exchange failed: {json.dumps(data, indent=2)}")

    refresh_token = data.get('refresh_token')
    open_id = data.get('open_id', '')
    rt_expires_days = int(data.get('refresh_token_expires_in', 31536000) / 86400)

    with open(TOKENS_FILE, 'w') as f:
        json.dump({'refresh_token': refresh_token, 'open_id': open_id,
                   'obtained_at': __import__('datetime').datetime.now().isoformat()},
                  f, indent=2)

    print("\n" + "=" * 60)
    print("SUCCESS! Tokens saved locally to", os.path.basename(TOKENS_FILE))
    print(f"Refresh token valid ~{rt_expires_days} days.")
    print("\nNow add it as a GitHub secret (run this in your terminal):\n")
    print(f"  gh secret set TIKTOK_REFRESH_TOKEN --body \"{refresh_token}\"")
    print("=" * 60)


if __name__ == '__main__':
    main()
