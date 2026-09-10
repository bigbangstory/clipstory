# Clipstory

Upload one long video, paste a list of timestamps, get back multiple clips cut
at exactly those points, named in sequence, downloadable as a zip.

Hosted and invite-only. Nothing to install on anyone's machine.

## Why it exists

Cutting a podcast or webinar into social clips means either paying per video
for a tool that guesses where the interesting bits are, or doing it by hand in
an editor. Clipstory does neither. You decide the moments; it does the cutting,
exactly, in bulk.

## The one guarantee

**Every clip starts on the frame you asked for.**

The fast way to cut video is `ffmpeg -c copy`, which does not re-encode. It can
only start a file on a keyframe, so it silently moves your cut by up to the
keyframe interval, commonly 2 to 10 seconds. It never warns you.

Clipstory re-encodes instead, and then checks its own work: every rendered clip
is probed and rejected if its duration drifts more than one frame from the
request. A clip you receive is a clip that was verified.

Measured on a source with keyframes every 10 seconds, asked to cut at 17.400s:

| | first frame | duration for a 5.000s request |
|---|---|---|
| Clipstory | frame 435, at 17.400s | 5.000s, 125 frames |
| `-c copy` | frame 250, at 10.000s | 12.480s, 312 frames |

See `tests/test_media.py`, which fails if this ever regresses.

## How it works

1. Sign in with a magic link. Invite-only: an address not on the list cannot
   get in, whoever sends them the URL.
2. Upload a long video. It goes up in chunks, so a dropped connection resumes
   instead of starting over.
3. Paste your cut points, one per line.
4. Check the table of what was understood. Nothing renders until you confirm.
5. Download the clips individually or as one zip, with a manifest.

### Timestamp formats

```
00:04:17 - 00:05:22
00:04:17.500 -> 00:05:22.250
04:17 to 05:22
257, 322
00:12:03 - 00:13:40 | Founder origin story
# lines starting with a hash are ignored
```

A line that will not parse blocks the whole job and is quoted back at you with
its line number. Partial rendering would be worse than no rendering.

### Output

```
podcast-ep12_clip_01_founder-origin-story.mp4
podcast-ep12_clip_02.mp4
podcast-ep12_clip_03_closing-line.mp4
manifest.json
```

Numbered in the order you pasted them, zero-padded so they sort correctly.

## Retention

The source video is deleted as soon as its clips render, because it is the
expensive object and is rarely needed twice. If a job fails, the source is kept
so you can retry without re-uploading. Clips are deleted after 30 days.
Both are configurable.

## Architecture

Two processes and a database, all on one machine.

| Piece | What it does |
|---|---|
| `app/web.py` | HTTP. Never blocks: the longest thing a request does is write one 8 MB chunk. |
| `app/worker.py` | Probing and rendering. Claims jobs with `FOR UPDATE SKIP LOCKED`. |
| Postgres | State and the queue. A restart resumes rather than losing work. |
| `app/storage.py` | Files behind an interface, so local disk can become S3 or R2 without a rewrite. |

Key modules: `timestamps.py` (parsing and validation), `media.py` (ffmpeg and
the accuracy check), `naming.py` (output filenames), `auth.py` (invite-only
magic links), `jobs.py` (lifecycle and retention).

## Deploying

See [docs/DEPLOY.md](docs/DEPLOY.md) for a free Oracle Cloud install with a
Cloudflare Tunnel. Short version:

```bash
cp .env.example .env      # set SECRET_KEY, POSTGRES_PASSWORD, ADMIN_EMAILS, BASE_URL
docker compose up -d --build
docker compose logs web | grep "SIGN-IN LINK"
```

## Tests

```bash
docker compose up -d db
DATABASE_URL=postgresql://clipstory:$PASSWORD@localhost:5432/clipstory ./run-tests.sh
```

124 tests. The media and integration tests need `ffmpeg` and `ffprobe` on the
path and skip cleanly without them; the integration tests need
`TEST_DATABASE_URL` and skip without it.

The suite generates real video, cuts it, and verifies the output frame by
frame, including once through the entire upload-to-download pipeline.

## Documents

- [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) - the agreed spec and why each
  decision was made
- [docs/FREE-TIER-OPTIONS.md](docs/FREE-TIER-OPTIONS.md) - which free hosts can
  actually run this, verified against vendor docs
- [docs/DEPLOY.md](docs/DEPLOY.md) - deployment
