# Clipstory

Upload one long video. It gets transcribed, an AI picks the moments worth
cutting, the clips are cut on the exact frame and named in order, and you
download a zip. If a pick is slightly off, nudge it and only that clip is
re-rendered.

Hosted and invite-only. Nothing to install on anyone's machine. Everything,
including the transcription and the AI, runs on your own server at no
per-video cost.

## Two things it does

One upload, one transcription, two ways to use it.

**Clips.** The AI picks the moments worth cutting and they are already
rendered when you open the page.

```
UPLOAD -> TRANSCRIBE -> AI PICKS CLIPS -> CUT ALL PICKS -> REVIEW & TWEAK -> RE-CUT CHANGED -> DOWNLOAD
          Whisper       local LLM         ffmpeg, exact    play, nudge,       only the ones     clip_01..N
          on the VM     on the VM         frame            drag, add, delete  you touched       + zip
```

**Edit.** The transcript is a document. Strike words out and they are cut from
the video.

```
UPLOAD -> TRANSCRIBE -> STRIKE WORDS OUT -> PREVIEW INSTANTLY -> EXPORT -> DOWNLOAD
          Whisper       or one-click        the player skips     re-encodes   the whole
          on the VM     cleanup             the deleted parts    in the bg    video, edited
```

Nothing waits on a human in either mode.

## The two guarantees

**1. Every clip starts on the frame you asked for.** The fast way to cut video
(`ffmpeg -c copy`) can only start on a keyframe, so it silently moves a cut by
up to the keyframe interval, commonly 2 to 10 seconds. Clipstory re-encodes
and then checks its own work: every rendered clip is probed and rejected if it
drifts more than one frame from the request. Measured on a source with
keyframes every 10 seconds, asked to cut at 17.400s:

| | first frame | duration for a 5.000s request |
|---|---|---|
| Clipstory | frame 435, at 17.400s | 5.000s, 125 frames |
| `-c copy` | frame 250, at 10.000s | 12.480s, 312 frames |

**2. The AI can never invent a timestamp.** It reads the transcript as
numbered segments and answers with segment numbers. The seconds come from
Whisper's measured word timings. A number that does not exist fails a lookup
and is dropped. A model's worst case is a dull pick, never a wrong cut.

Both are enforced by tests that fail if anyone regresses them.

## Editing by text

The transcript renders as a document beside the player. Drag across words,
press Delete, and they are cut. Click a struck word to bring it back.

**One-click cleanup** removes filler words (um, uh, erm, hmm, ah) and shortens
long pauses. Conversational words (so, like, right, you know) are off by
default, because each is usually doing real work in a sentence and removing
them silently changes what someone said. Pauses are *shortened*, not removed:
speech with its pauses stripped out sounds rushed.

**Two things make this work on hardware that could never render it live:**

Editing costs nothing. The preview player seeks past the deleted ranges, so
the edit plays back immediately without encoding a thing. Only Export
re-encodes, and that runs in the background like every other job.

Cuts are snapped into silence. Every deletion is widened to the middle of the
gap between the surrounding words, so the join lands where nobody is speaking
instead of clipping the attack of a word.

## Reviewing the picks

One page per video: the clips on the left, the source player with a timeline
and the searchable transcript on the right.

- **Play** any rendered clip, or audition a proposed range in the source.
- **Nudge** a clip's start or end by one second, or to the previous or next
  word boundary from the transcript.
- **Drag** a clip's edges on the timeline; they snap to the nearest word.
- **Add** a clip from the player (set start, set end), by shift-clicking a
  transcript line and clicking another, or as a line of text.
- **Delete** any clip.
- **Apply changes** re-renders only what changed.
- **Finalise** renumbers the clips 01 to N, deletes the source, and locks the
  job.

The text box accepts `HH:MM:SS.mmm - HH:MM:SS.mmm | label`, `MM:SS`, or plain
seconds, with `-`, `->`, `to` or a comma between start and end. A line that
matches an existing clip leaves it untouched.

## Output

```
podcast-ep12_clip_01_your-first-ten-clients.mp4
podcast-ep12_clip_02_the-fiverr-objection.mp4
podcast-ep12_clip_03_one-piece-of-advice.mp4
manifest.json
```

## Stack, all on one machine

| Layer | Tool |
|---|---|
| Web | FastAPI, plain HTML, one small script for the review page |
| State and queue | Postgres, claimed with `FOR UPDATE SKIP LOCKED` |
| Worker | One Python process: probe, transcribe, suggest, render |
| Reading and cutting | ffprobe, ffmpeg (libx264, CRF 18) |
| Transcription | faster-whisper, Whisper `base`, word timestamps, VAD |
| Clip picking | Ollama running `qwen2.5:7b-instruct`, JSON-schema constrained |
| Text editing | ffmpeg `trim`/`concat` via `-filter_complex_script`, frame-aligned |
| Public URL | Cloudflare Tunnel, no inbound ports |

Both model providers sit behind an interface: `TRANSCRIPTION_PROVIDER` and
`SUGGEST_PROVIDER` in `.env`. Hosted Claude (`anthropic`) is available as a
paid swap for the clip picker.

## Deploying

See [docs/DEPLOY.md](docs/DEPLOY.md) for a zero-cost install on an Oracle
Cloud Always Free VM. Short version:

```bash
cp .env.example .env      # SECRET_KEY, POSTGRES_PASSWORD, ADMIN_EMAILS, BASE_URL
docker compose up -d --build
docker compose logs web | grep "SIGN-IN LINK"
```

## Tests

```bash
docker compose up -d db
DATABASE_URL=postgresql://clipstory:$PASSWORD@localhost:5432/clipstory ./run-tests.sh
```

285 tests. The runner disables both model providers so nothing downloads
weights or calls a model; tests that need a transcript or suggestions inject
fakes. Media and integration tests need `ffmpeg` and skip cleanly without it;
integration tests need `TEST_DATABASE_URL`.

The suite generates real video, cuts it, and checks the output frame by frame,
including once through the entire upload, review, tweak, finalise, download
path.

## Documents

- [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md): what was agreed and why
- [docs/DEPLOY.md](docs/DEPLOY.md): deployment and first-run measurement
- [docs/FREE-TIER-OPTIONS.md](docs/FREE-TIER-OPTIONS.md): which free hosts can run this
