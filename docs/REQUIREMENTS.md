# Clipstory - Requirement Specification (v1)

Status: draft for sign-off. No code written until this is agreed.
Last updated: 2026-09-09

---

## 1. What this is

A hosted (cloud-only) tool that takes one long video, produces a transcript with
timestamps, lets the operator paste a list of cut points, and renders multiple
frame-accurate clips named in sequence, downloadable as a zip.

Nothing runs on the operator's machine. Accessible from any browser.

## 2. What this is NOT (v1 scope guard)

- No AI/LLM selection of highlights. Cut points are supplied by a human.
- No vertical 9:16 reframing or face tracking.
- No burned-in subtitles.
- No multi-user teams, roles or client accounts.
- No editing beyond trimming (no transitions, music, colour, overlays).

Each of these is a defensible v2 item. None is in v1.

## 3. Core user flow

1. **Upload** - operator selects a long video in the browser. The file goes
   directly to object storage via a resumable, presigned upload. It never
   passes through the web app server.
2. **Probe** - worker reads duration, container, video/audio codecs, resolution,
   fps, and whether the frame rate is constant or variable. Stored on the job.
3. **Transcribe** - worker extracts audio and produces a transcript with
   segment-level and word-level timestamps. Displayed alongside the video in the
   browser so the operator can find their moments.
4. **Define cuts** - operator pastes a plain-text list of timestamp ranges into
   a text box. Parsed, validated, previewed as a table before rendering.
5. **Render** - worker cuts each range as its own MP4, frame-accurate.
6. **Deliver** - clips listed with individual download links, plus a
   "Download all" zip of the batch. Plus a manifest.

## 4. Cut accuracy - the non-negotiable part

The stated requirement: cut at the exact point given, never approximately.

**Decision: frame-accurate re-encode.** Stream-copy (`-c copy`) cannot honour an
arbitrary timestamp because it can only start a file on a keyframe, so it silently
moves the cut by up to the keyframe interval (commonly 2-10 seconds on uploaded
footage). That is exactly the failure mode this tool exists to avoid, so
stream-copy is rejected for v1 even though it is faster.

Per-clip command shape:

```
ffmpeg -ss <start> -i <source> -t <duration> \
       -c:v libx264 -crf 18 -preset veryfast -pix_fmt yuv420p \
       -c:a aac -b:a 192k \
       -avoid_negative_ts make_zero -movflags +faststart \
       <output>
```

Notes on the choices:
- `-ss` before `-i` gives fast seek; with re-encode ffmpeg decodes from the
  preceding keyframe and discards, so the output still starts on the exact
  requested frame. Accuracy is preserved, seek time is not wasted.
- `-crf 18` is visually transparent for social/marketing use.
- `-preset veryfast` trades file size for render time. Tunable per job later.
- `+faststart` puts the moov atom first so clips stream instantly on the web.
- Audio is always re-encoded to AAC to guarantee a clean start with no priming
  gap at the cut.

**Verification requirement:** after each render the worker probes the output and
asserts the actual duration is within one frame of the requested duration. A clip
that fails this check is marked failed, not silently delivered.

**Cost of this decision:** roughly 15-40 seconds of render per 1080p clip on a
2 vCPU worker, versus about 1 second for stream copy. This is the correct trade
for a tool whose whole purpose is exact cuts. If render times become painful in
real use, "smart cut" (copy the interior, re-encode only the head and tail
fragments, concatenate) is the v2 optimisation. It is materially more code and
has real edge cases with variable frame rate sources, so it is not v1.

## 5. Timestamp input format

Single text box, one clip per line. The parser accepts:

```
00:04:17 - 00:05:22
00:04:17.500 - 00:05:22.250
04:17-05:22
257 - 322
00:12:03 - 00:13:40 | Founder origin story
```

Rules:
- Separator: `-`, `->`, `to`, or a comma. Whitespace ignored.
- `HH:MM:SS`, `MM:SS`, `HH:MM:SS.mmm` and bare seconds all accepted.
- Optional label after a `|`, used in the filename and manifest.
- Blank lines and lines starting with `#` ignored.

Validation, all shown before any render starts:
- start must be < end
- end must be <= source duration
- minimum clip length 1 second
- overlapping ranges are allowed but flagged as a warning, not an error
- any unparseable line blocks the job with the offending line number quoted

The parsed list renders as a table (number, start, end, duration, label) for the
operator to confirm. Nothing renders until they confirm.

## 6. Output naming

Default pattern:

```
{source_slug}_clip_{NN}.mp4
```

- `NN` is zero-padded, sequenced in the order the ranges were pasted, starting
  at 01. Padding widens automatically past 99 clips.
- `source_slug` is the original filename, lowercased, non-alphanumerics collapsed
  to hyphens, truncated to 60 chars.
- If a label was supplied it is appended: `podcast-ep12_clip_03_founder-origin-story.mp4`
- Pattern is configurable per job via a template string, with the sequence
  number always mandatory.

Example batch:
```
podcast-ep12_clip_01.mp4
podcast-ep12_clip_02.mp4
podcast-ep12_clip_03.mp4
```

## 7. Deliverables per job

- The clip MP4s.
- `manifest.json` and `manifest.csv`: clip number, filename, start, end,
  duration, label, transcript text falling inside the range, and the rendered
  file's probed duration.
- `all_clips.zip` containing the clips and both manifests, streamed on demand
  rather than pre-built, so a large batch does not sit in storage twice.

## 8. Architecture and hosting - recommendation

**Recommendation: split web and worker.**

| Layer | Choice | Why |
|---|---|---|
| Web UI | Next.js on Vercel | Already connected to this account. Handles auth, upload UI, transcript view, cut input, job status. Never touches ffmpeg. |
| Storage | Supabase Storage | Source videos, rendered clips, transcripts. Resumable uploads for large files, presigned URLs so bytes never cross the app server. |
| Database | Supabase Postgres | Jobs, clips, transcript segments, status. |
| Auth | Supabase Auth | Single operator account in v1, email login. |
| Worker | Long-running container on Railway, Render or Fly.io | ffmpeg needs a real filesystem, real CPU minutes and no execution timeout. |
| Queue | Postgres-backed job table with row locking (`FOR UPDATE SKIP LOCKED`) | One less service. Adequate for single-operator volume. Swap to a dedicated queue only if concurrency becomes real. |

**Why the worker cannot be a serverless function.** Vercel and Supabase Edge
Functions both cap execution time and give ephemeral, size-limited disk. A
90-minute 1080p source is commonly 2-8 GB, and a batch of 10 frame-accurate
clips is minutes of sustained CPU. That work needs a persistent container.
This is the single most important infrastructure constraint in the project.

**The honest alternative:** run everything in one container on Railway or
Render, Next.js and worker together. Fewer moving parts, one deploy, one bill,
and genuinely simpler to debug. It scales worse and couples UI uptime to render
load. Given single-operator volume, this is a legitimate choice and I would not
argue hard against it. My recommendation stays with the split because storage
and database on Supabase gives durable files and job history for free, and
because the worker can then be restarted or resized without touching the UI.

**Transcription: provider interface, one default.** The worker calls a
`TranscriptionProvider` interface returning a normalised
`{segments: [{start, end, text, words: [{start, end, word}]}]}` shape.
Candidate implementations: a hosted Whisper API, AssemblyAI, Deepgram. Swapping
provider is one class, no pipeline changes. Since the operator supplies cut
points manually, the transcript is a reading aid, not a decision engine, so the
cheapest adequate provider wins.

## 9. Data model (first pass)

```
jobs
  id, user_id, source_filename, source_path, status, error,
  duration_seconds, width, height, fps, vfr, created_at, updated_at

transcript_segments
  id, job_id, index, start_seconds, end_seconds, text

clips
  id, job_id, sequence, label, start_seconds, end_seconds,
  output_filename, output_path, status, rendered_duration, error
```

Job status: `uploading -> probing -> transcribing -> awaiting_cuts -> rendering -> complete | failed`

## 10. Open items needing your confirmation

1. **Storage file size limit.** Supabase Storage plans have per-file upload
   ceilings that differ by tier. I have not verified the current numbers and
   will not quote them from memory. Before we build, I need to confirm your
   Supabase plan and its actual per-file limit against Supabase's own pricing
   and storage docs. If your typical source exceeds it, storage moves to S3 or
   Cloudflare R2 and everything else stays the same.
2. **Typical source size and duration.** What is a realistic worst case for you,
   1 hour at 1080p, or 3 hours at 4K? This sets worker size and cost directly.
3. **Clips per video.** Typically how many ranges will you paste, 5 or 50? This
   sets whether rendering needs to run in parallel.
4. **Retention.** How long should source videos and clips stay in storage before
   auto-deletion? Storage cost on multi-GB sources adds up and is the main
   recurring bill in this design.
5. **Who logs in.** Just you, or others on the team? v1 assumes one account.

## 11. Build phases

- **Phase 1** - upload, probe, paste timestamps, frame-accurate render, sequential
  naming, per-clip download, zip download, manifest. Fully usable without any
  transcription.
- **Phase 2** - transcription provider, transcript display next to the video,
  click a sentence to fill a timestamp into the cut box.
- **Phase 3** - optimisations and extras as needed: smart cut, 9:16 versions,
  burned-in captions, multi-user.

Phase 1 is deliberately transcription-free so that the exactness of the cutting,
which is the actual point of the tool, can be proven before any API spend.
