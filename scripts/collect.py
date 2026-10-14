#!/usr/bin/env python3
"""Daily collector for KH95314/ivy-reels-watch.

Usage:
    collect.py --workdir ~/workspace/ivy-reels-watch [--date YYYY-MM-DD]

1. Reads seeds/watchlist.csv (status active/candidate).
2. Pulls recent reels via batched `instagram-cli posts` (stops at first 429).
3. Merges into reels/latest.csv (dedupe by shortcode); new reels go to
   reels/daily/<date>.csv.
4. Flags new outliers (likes >= 3x account median) into briefing/<date>.md.
5. On Mondays (or if profiles/latest.csv is >7 days old): refreshes
   profiles/latest.csv + profiles/weekly/<date>.csv (same 429-stop rule).
6. Prints a JSON summary to stdout.

Read-only: never follows/likes/comments. No paid tools.
"""
import argparse
import csv
import json
import os
import re
import statistics
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

LA = ZoneInfo('America/Los_Angeles')
PREFERRED_USER = 'tryaifirst'
BATCH = 8

notes = []
hit_429 = False


def run(cmd):
    """Run a shell command; return (ok, stdout). Sets hit_429 on rate limit."""
    global hit_429
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        notes.append(f'timeout: {cmd[:80]}')
        return False, ''
    out = (p.stdout or '') + (p.stderr or '')
    # Rate-limit signal: non-zero exit + an actual rate-limit indication.
    # (A bare '429' substring is NOT enough — fbids/captions can contain it.)
    if p.returncode != 0 and re.search(
            r'\b429\b|too many requests|rate.?limit', out, re.IGNORECASE):
        hit_429 = True
        notes.append('instagram-cli returned 429 — stopped further CLI calls')
        return False, ''
    if p.returncode != 0:
        notes.append(f'cmd failed ({p.returncode}): {cmd[:80]}')
        return False, ''
    return True, p.stdout


def get_account_id():
    ok, out = run('instagram-cli accounts')
    if ok:
        try:
            data = json.loads(out)
            for a in data.get('accounts', []):
                if a.get('username') == PREFERRED_USER:
                    return a['user_fbid']
            accs = data.get('accounts', [])
            if accs:
                return accs[0]['user_fbid']
        except Exception as e:
            notes.append(f'accounts parse failed: {e}')
    m = re.search(r'"user_fbid"\s*:\s*"(\d+)"', out or '')
    if m:
        return m.group(1)
    notes.append('using fallback account id from memory')
    return '17841427825684051'  # @tryaifirst (user's own account)


def shortcode_of(url):
    m = re.search(r'instagram\.com/(?:reel|p)/([A-Za-z0-9_-]+)', url or '')
    return m.group(1) if m else ''


def read_csv(path):
    if not os.path.isfile(path):
        return [], []
    with open(path, encoding='utf-8') as f:
        r = csv.DictReader(f)
        rows = list(r)
        return r.fieldnames, rows


def write_csv(path, fieldnames, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workdir', required=True)
    ap.add_argument('--date', default=None)
    args = ap.parse_args()
    wd = args.workdir
    today = args.date or datetime.now(LA).strftime('%Y-%m-%d')

    # --- watchlist ---
    _, wl = read_csv(f'{wd}/seeds/watchlist.csv')
    targets = [(r['handle'], r['niche']) for r in wl
               if r.get('status') in ('active', 'candidate') and r.get('handle')]
    if not targets:
        print(json.dumps({'error': 'empty watchlist'}))
        return

    aid = get_account_id()

    # --- known reels ---
    _, known = read_csv(f'{wd}/reels/latest.csv')
    seen_sc = {r['shortcode'] for r in known}
    for r in known:
        r.setdefault('source', 'seed')  # migrate pre-source rows

    # --- pull recent reels, batched ---
    new_rows, daily_rows = [], []
    followers_seen = {}
    handles = [h for h, _ in targets]
    niche_of = dict(targets)
    for i in range(0, len(handles), BATCH):
        if hit_429:
            break
        batch = handles[i:i + BATCH]
        ok, out = run(
            f"instagram-cli posts --account-id {aid} "
            f"--username {','.join(batch)} --post-types REEL --limit 12")
        if not ok:
            continue
        try:
            posts = json.loads(out).get('posts', [])
        except Exception as e:
            notes.append(f'posts parse failed for batch {i}: {e}')
            continue
        for p in posts:
            url = p.get('url') or ''
            sc = shortcode_of(url)
            if not sc or sc in seen_sc:
                continue
            uname = p.get('username') or ''
            if uname not in niche_of:
                continue  # batched calls can return non-watchlist posts; skip
            pdate = (p.get('created_at') or '')[:10]
            likes = p.get('likes')
            likes_s = str(likes) if isinstance(likes, int) else ''
            cap = (p.get('post_caption') or '').replace('\n', ' ')[:150]
            if uname not in followers_seen and isinstance(p.get('follower_count'), int):
                followers_seen[uname] = p['follower_count']
            row = {'handle': uname, 'niche': niche_of.get(uname, ''),
                   'reel_url': url, 'shortcode': sc, 'post_date': pdate,
                   'likes': likes_s, 'caption': cap, 'first_seen': today,
                   'source': 'pull'}
            new_rows.append(row)
            daily_rows.append(row)
            seen_sc.add(sc)

    # --- merge reels/latest.csv ---
    rfields = ['handle', 'niche', 'reel_url', 'shortcode', 'post_date',
               'likes', 'caption', 'first_seen', 'source']
    all_reels = known + new_rows
    write_csv(f'{wd}/reels/latest.csv', rfields, all_reels)
    if daily_rows:
        write_csv(f'{wd}/reels/daily/{today}.csv', rfields, daily_rows)

    # --- outlier detection: baseline = this account's pulled reels only
    # (seeds are breakout-biased); flag reels posted within last 14 days ---
    from datetime import date as d, timedelta
    cutoff = (d.fromisoformat(today) - timedelta(days=14)).isoformat()
    pull_by_handle = {}
    for r in all_reels:
        if r.get('source') == 'pull':
            pull_by_handle.setdefault(r['handle'], []).append(r)
    outliers = []
    for h, rs in pull_by_handle.items():
        recent = sorted(rs, key=lambda r: r.get('post_date', ''), reverse=True)[:20]
        vals = [int(r['likes']) for r in recent
                if str(r.get('likes', '')).isdigit()]
        if len(vals) < 5:
            continue
        med = statistics.median(vals)
        if med <= 0:
            continue
        for r in recent:
            if r.get('first_seen') != today:
                continue
            if not r.get('post_date') or r['post_date'] < cutoff:
                continue
            lk = r.get('likes', '')
            if str(lk).isdigit() and int(lk) >= 3 * med:
                outliers.append({'handle': h, 'niche': r.get('niche', ''),
                                 'url': r['reel_url'], 'likes': lk,
                                 'median': med, 'mult': round(int(lk) / med, 1),
                                 'post_date': r['post_date'],
                                 'caption': r.get('caption', '')})

    # --- profiles (Mondays or stale >7d) ---
    pfields, profs = read_csv(f'{wd}/profiles/latest.csv')
    do_profiles = False
    if datetime.now(LA).weekday() == 0:
        do_profiles = True
    elif profs:
        try:
            latest_check = max(r.get('checked_at', '') for r in profs)
            do_profiles = latest_check < datetime.now(LA).strftime('%Y-%m-%d')
            # simpler: refresh if oldest check older than 7 days
            oldest = min(r.get('checked_at', '9999') for r in profs)
            from datetime import date as d
            do_profiles = (d.today() - d.fromisoformat(oldest)).days >= 7 \
                if oldest and oldest != '9999' else True
        except Exception:
            do_profiles = True
    profiles_refreshed = 0
    if do_profiles and not hit_429:
        pmap = {r['handle']: r for r in profs}
        for h, n in targets:
            if hit_429:
                break
            ok, out = run(f'instagram-cli profile --account-id {aid} --username {h}')
            if not ok:
                continue
            try:
                ps = json.loads(out).get('profiles', [])
            except Exception:
                continue
            if not ps:
                continue
            pr = ps[0]
            fl = pr.get('follower_count', '')
            bio = (pr.get('bio') or pr.get('biography') or '').replace('\n', ' ')[:200]
            pmap[h] = {'handle': h, 'niche': n,
                       'followers': str(fl) if fl != '' else pmap.get(h, {}).get('followers', ''),
                       'bio': bio,
                       'profile_url': f'https://www.instagram.com/{h}/',
                       'checked_at': today}
            profiles_refreshed += 1
        pfields = ['handle', 'niche', 'followers', 'bio', 'profile_url', 'checked_at']
        write_csv(f'{wd}/profiles/latest.csv', pfields, list(pmap.values()))
        write_csv(f'{wd}/profiles/weekly/{today}.csv', pfields, list(pmap.values()))

    # --- followers: free refresh from posts data (no extra API calls) ---
    if followers_seen:
        pmap = {r['handle']: r for r in profs}
        for h, fl in followers_seen.items():
            if h in pmap:
                pmap[h]['followers'] = str(fl)
                pmap[h]['checked_at'] = today
        pfields = ['handle', 'niche', 'followers', 'bio', 'profile_url', 'checked_at']
        for r in pmap.values():
            for k in pfields:
                r.setdefault(k, '')
        write_csv(f'{wd}/profiles/latest.csv', pfields, list(pmap.values()))

    # --- briefing ---
    blines = [f'# Ivy Reels Watch — {today}', '']
    if outliers:
        blines.append(f'## 新爆款（{len(outliers)} 条，点赞≥平时3倍）')
        for o in sorted(outliers, key=lambda x: x['mult'], reverse=True):
            blines.append(
                f"- @{o['handle']}（{o['niche']}）：{o['url']}｜点赞 {o['likes']} "
                f"（平时中位 {o['median']}，约 {o['mult']}x）｜发布 {o['post_date']}")
            if o['caption']:
                blines.append(f"  caption: {o['caption']}")
    else:
        blines.append('## 新爆款：无')
    if notes:
        blines.append('')
        blines.append('## 运行备注')
        blines.extend(f'- {n}' for n in notes)
    bpath = f'{wd}/briefing/{today}.md'
    os.makedirs(os.path.dirname(bpath), exist_ok=True)
    open(bpath, 'w', encoding='utf-8').write('\n'.join(blines) + '\n')

    summary = {'date': today, 'accounts_targeted': len(targets),
               'accounts_checked': len(handles) if not hit_429 else 'partial',
               'new_reels': len(new_rows), 'outliers': len(outliers),
               'hit_429': hit_429, 'profiles_refreshed': profiles_refreshed,
               'briefing': f'briefing/{today}.md', 'notes': notes}
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
