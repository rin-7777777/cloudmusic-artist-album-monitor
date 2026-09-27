# NetEase Cloud Music – Artist Album Monitor · cloudmusic-artist-album-monitor

> This crawler was written for my own personal use. The number of artists I care about keeps growing, and opening each artist page one by one every day just to check whether a new song or a cover had been uploaded was way too much hassle. I tried a .bat file before, but it opened a pile of pages in an instant — everything got laggy, there was a chance of getting banned, and it was bad for the CPU. So I wrote this thing (why didn't I think of using a crawler sooner? I really am a fool).

**Language**: [中文](../README.md) ｜ [日本語](README.ja.md) ｜ [English](README.en.md)

---

## What it does

It reads the list in `歌手配置.txt`, uses the public NetEase Cloud Music API to fetch
**every album** of each artist, compares the result against the last successful snapshot by
**album ID**, and appends the **new** and **removed** albums to a log file.

One run = one up-to-date snapshot + one log containing only the changes.
No more opening artist pages one by one.

## Features

- Single-file script, only `requests`, synchronous sequential requests — no async, no threads, no Selenium
- Automatic pagination, so it fetches *all* albums of an artist (not just the few shown on the web page, and no login needed)
- New albums are detected by **album ID**, so renames, remasters and new cover art are never mistaken for new releases
- On a failed request or an invalid artist ID it **never overwrites the old snapshot** — only the status and failure reason are updated
- Network errors are retried automatically; when a previously failed artist succeeds again, the missing new albums are recorded and a `[恢复]` (recovered) line is written
- Random delays between requests, to avoid hammering the API
- Atomic snapshot writes (temp file + replace), so a Ctrl+C in the middle cannot corrupt the JSON
- No configuration, no database — the four files simply live next to the script

## Requirements

- Python 3.x (developed and tested on Windows 10 + Python 3.14.4)
- `requests`

```bash
pip install requests
```

## Files

| File | Purpose | Access |
| --- | --- | --- |
| `专辑监控.py` | The main script (single file) | — |
| `歌手配置.txt` | File C: artist list + `LIMIT` (items per page) | read-only |
| `专辑快照.json` | File A: full album snapshot per artist | read + write |
| `监控日志.log` | File B: new / removed / failure / recovery log | append |

For daily use you only need these four files in the root; everything else lives in `其他文件/` ("other files"). `README.md` (Chinese) stays in the root because that is the one GitHub renders on the repo page:

```text
翻唱检查/
├── 专辑监控.py          the main script
├── 歌手配置.txt         File C: artist list
├── 专辑快照.json        File A: full snapshot
├── 监控日志.log         File B: change log
├── README.md            Chinese readme (the one GitHub renders)
└── 其他文件/            junk drawer, unrelated to running it
    ├── README.ja.md       Japanese version
    ├── README.en.md       this file (English)
    ├── 旧/                the old .bat script and hand-saved web pages
    ├── 测试.py            a one-off script used to probe the API
    └── .vscode/           editor settings
```

## Quick start

1. Install the dependency: `pip install requests`
2. Edit `歌手配置.txt` and add one line per artist: `artist name | artist ID`
3. Run it:

```bash
python 专辑监控.py
```

The artist ID is the number in the artist page URL, e.g. `https://music.163.com/#/artist/album?id=53678173`.
On the first run, every album that is found is recorded as `[新增]` (new).

## Configuration format

```text
LIMIT = 50            # items per page; optional, defaults to 50

# artist name | artist ID
花譜 | 32062601
理芽 | 47614811
```

- Lines starting with `#` and empty lines are ignored
- Malformed lines are skipped with a warning on the console; the run is not aborted
- The "artist name" is only used for display and for the log. Renaming an artist does not affect new-album detection (only the album ID matters)
- Duplicate artist IDs are skipped with a notice

## Sample output

Console:

```text
[1/24] 巫てんり（ID=94660135）处理中 ...
      第 1 页 offset=0 取回 5 张（累计 5 张）
    [成功] 共 5 张 | 新增 5 张 | 下架 0 张
    等待 7.3 秒后处理下一位歌手 ...
```

`监控日志.log`:

```text
[2026-09-28 01:28:09] [新增] 巫てんり | 94660135 | Reveal | 2026-04-12 | 369215220
[2026-09-28 01:28:09] [下架] 某位歌手 | 12345678 | 某张专辑 | 2020-01-01 | 123456789
[2026-09-28 01:28:09] [失败] 某位歌手 | 12345678 | 原因=网络请求失败（共尝试 3 次）... | 旧专辑数=42 | 已保留旧快照
[2026-09-28 01:28:09] [恢复] 某位歌手 | 12345678 | 本次新增3张 | 状态已恢复ok
```

(Console messages and log lines are in Chinese.)

## Data source and rules

The web page HTML no longer contains the album data, so instead of parsing HTML with BeautifulSoup
the script calls the public API directly:

```text
GET https://music.163.com/api/artist/albums/{artist_id}?offset={offset}&limit={limit}
```

Requests use a normal browser User-Agent plus `Referer: https://music.163.com/`, with a 15 second timeout.
Random delays: 1.5–3.5 s between pages, 5–10 s between artists.

- `code=200` and `artist` is not null → success. Take `id` / `name` / `publishTime` from `hotAlbums` (millisecond timestamp converted to `YYYY-MM-DD`)
- `code!=200` or `artist` is `null` → the artist ID is invalid: status becomes `invalid`, **no retry**, the old snapshot is left untouched
- `code=200`, `artist` not null, but `hotAlbums` is empty → treated as "this artist has no albums": status `ok`, the old list is cleared, no failure log is written
- Pagination: `offset` starts at 0 and `+= limit` each time; the end is reached when a page returns fewer than `limit` items (only an exactly-full page continues)
- Network errors (timeout / connection failure / 5xx): retried twice (2–3 s apart); if it still fails the status becomes `failed` and the old snapshot is preserved

## Notes

- `专辑快照.json` and `监控日志.log` are the only history. **If you delete the snapshot, the next run will record every album as new again**, so don't delete them casually
- When a fetch fails the old list is kept as-is, so occasionally skipping a run does not lose data — the next successful run catches up automatically
- A failure reason longer than 200 characters is truncated in the log (the full text is kept in `专辑快照.json`)
- With many artists a full run takes several minutes (the delays are intentional — don't set them too low)
- It only reads public endpoints: no login, no audio downloads

## Disclaimer

For personal use and learning only. Please do not turn it into a high-frequency scraper, and do not use it commercially.
All API data and related content belong to NetEase Cloud Music and its rights holders.
