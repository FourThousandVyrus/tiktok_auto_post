# TikTok Auto-Poster — Setup Guide

Posts ~2 archived videos per day from the R2 queue (oldest first), on this weekly schedule:

| Day | Times |
|---|---|
| Mon | 10:23 AM · 6:53 PM |
| Tue | 4:07 AM · 12:29 PM |
| Wed | 11:03 AM · 7:00 PM |
| Thu | 4:30 AM · 7:00 PM |
| Fri | 12:29 PM · 6:50 PM |
| Sat | 10:23 AM · 7:00 PM |
| Sun | 11:00 AM · 12:29 PM |

14 posts/week ≈ 76 weeks of content from the current archive.

## What's in this folder
| File | Purpose |
|---|---|
| `worker.py` | The poster — runs on GitHub Actions |
| `tiktok_auth_helper.py` | One-time TikTok login → refresh token (run on YOUR PC) |
| `.github/workflows/post.yml` | Scheduler: 2× daily + manual test button |
| `requirements.txt` | Python deps for Actions |

## Setup steps (one-time, ~20 min)

### 1. Create the GitHub repo
- github.com → New repository → name it anything → **Private**
- Do NOT initialize with README

### 2. Push this folder as the repo
In a terminal inside this folder:
```
git init
git add .
git commit -m "TikTok cloud poster"
git branch -M main
git remote add origin https://github.com/YOURNAME/REPO.git
git push -u origin main
```

### 3. Add secrets (repo → Settings → Secrets and variables → Actions)
```
gh auth login
gh secret set R2_ACCOUNT_ID --body "<your Account ID>"
gh secret set R2_ACCESS_KEY --body "<Access Key ID>"
gh secret set R2_SECRET_KEY --body "<Secret Access Key>"
gh secret set TIKTOK_CLIENT_KEY --body "awt197e29ay51wmj"
gh secret set TIKTOK_CLIENT_SECRET --body "<client secret>"
```

### 4. Get the TikTok refresh token (on your PC, once)
```
pip install requests
python tiktok_auth_helper.py
```
- Browser opens → log into the TikTok account that will post → Approve
- The page errors out at example.com — that's expected; copy the full address-bar URL back into the helper
- Run the `gh secret set TIKTOK_REFRESH_TOKEN ...` line it prints

### 5. Test manually
Repo → Actions tab → **Post to TikTok** → Run workflow → watch logs.
First post goes live as **private** (SELF_ONLY) until app audit passes.
Check the TikTok app: the video appears in your profile's private list.

### 6. Done. It posts by itself twice a day.

## Ongoing maintenance
| When | What |
|---|---|
| ~every 7 months | Upload next batch from your PC (`python r2_upload.py --batch N`) |
| ~every 11 months | Re-run `tiktok_auth_helper.py` (refresh tokens expire yearly) |
| After app review passes | Change `PRIVACY_LEVEL` to `PUBLIC_TO_EVERYONE` in post.yml |

## Changing post times
Edit the cron lines in `.github/workflows/post.yml` (your zone is UTC+0, so local time = UTC; day numbers: 0=Sun … 6=Sat).
