# Clipstory - Requirement Specification (v2, simplified)

Status: draft for sign-off.
Last updated: 2026-09-10
Supersedes v1. Change log at the bottom.

---

## 1. What it is

A hosted tool. Your team logs in from any browser, uploads a long video, pastes
a list of timestamp ranges, and gets back multiple clips cut at exactly those
points, named in sequence, downloadable as a zip.

Nothing installed on anyone's machine.

## 2. The flow

1. Log in with a magic link sent to your work email.
2. Upload a long video. It goes straight from the browser to storage.
3. Paste timestamp ranges into a text box.
4. Confirm the parsed table.
5. Render. Watch progress.
6. Download the clips, individually or as one zip.

## 3. Not in scope

No AI picking cuts. No vertical reframing. No burned-in subtitles. No editing
beyond trimming. Transcription comes later, in Phase 2.

## 4. Cut accuracy

Every clip is re-encoded so it starts on the exact frame requested.

Stream copy (`-c copy`) is rejected. It can only start a file on a keyframe, so
it silently moves your cut by up to the keyframe interval, commonly 2 to 10
seconds. That is the failure this tool exists to prevent.

```
ffmpeg -ss <start> -i <source> -t <duration> \
       -c:v libx264 -crf 18 -preset veryfast -pix_fmt yuv420p \
       -c:a aac -b:a 192k \
       -avoid_negative_ts make_zero -movflags +faststart \
       <output>
```

`-ss` before `-i` seeks fast; with re-encode the output still begins on the
exact frame. CRF 18 is visually transparent. `+faststart` makes clips stream
instantly.

After each render the worker probes the output and asserts the duration is
within one frame of what was asked. A clip that fails is marked failed, never
delivered silently.

Cost: roughly 15 to 40 seconds per 1080p clip instead of about 1 second. Correct
trade for a tool whose entire point is exactness.

## 5. Timestamp input

One clip per line:

```
00:04:17 - 00:05:22
00:04:17.500 - 00:05:22.250
04:17-05:22
257 - 322
00:12:03 - 00:13:40 | Founder origin story
```

Accepts `-`, `->`, `to` or a comma as separator. Accepts `HH:MM:SS`, `MM:SS`,
`HH:MM:SS.mmm` and bare seconds. Optional label after `|`. Ignores blank lines
and lines starting with `#`.

Blocks the job with the offending line quoted if: start is not before end, end
exceeds the video duration, a clip is under 1 second, or a line will not parse.
Overlapping ranges are allowed but warned about.

The parsed list is shown as a table. Nothing renders until it is confirmed.

## 6. Naming and output

```
{source_slug}_clip_{NN}.mp4
```

Zero-padded, numbered in the order pasted, starting at 01. Padding widens past
99 clips. With a label: `podcast-ep12_clip_03_founder-origin-story.mp4`

Each job delivers the MP4s, a `manifest.json` (clip number, filename, start,
end, duration, label, probed duration), and `all_clips.zip` streamed on demand
rather than stored twice.

## 7. Access

Magic-link login, no passwords to manage.

**Access is restricted to an allowlist, not open to anyone with the URL.** An
open tool means anyone can upload gigabytes and burn hours of CPU on your bill,
and you become the host of whatever they upload. The allowlist is either your
email domain or a list of invited addresses. Adding a teammate takes seconds.

Every user sees only their own jobs. An admin flag can see all of them.

## 8. Architecture: two services

Down from four in v1. This is the simplification.

| What | Where | Why |
|---|---|---|
| Web UI + ffmpeg worker | **One container on Railway**, with a persistent volume | Same repo, same deploy. The worker polls the jobs table in a background process. No separate host, no queue service. |
| Auth + database + file storage | **Supabase** | Three things from one vendor, no code to write for any of them. |

Vercel is dropped. Splitting the UI onto a second host bought nothing at your
volume and doubled the deploy surface.

**Why the worker cannot be serverless.** Long video needs sustained CPU minutes
and a real filesystem. This is the one constraint that is not negotiable.

**Job state lives in Postgres**, so a container restart or redeploy resumes
instead of losing the work. The worker claims jobs with `SELECT ... FOR UPDATE
SKIP LOCKED`. That is the whole queue.

## 9. Verified plan requirements

Two hard gates, both checked against vendor docs on 2026-09-10:

**Supabase free tier caps uploads at 50 MB per file.** Fatal for long video. The
Pro plan raises this to 500 GB. **Supabase Pro is required**, not optional.
(https://supabase.com/docs/guides/storage/uploads/file-limits)

**Railway volumes are capped by plan**: Trial 500 MB, Hobby 5 GB, Pro up to 1 TB.
The worker needs room for one source plus its clips at once. A 90-minute 1080p
source is roughly 3.4 to 5.4 GB, so **Hobby's 5 GB is too tight and Pro is
required**. (https://docs.railway.com/volumes/reference)

For contrast, Render caps `/tmp` at 2 GB on **every** instance type and evicts
the service when exceeded, which rules Render out for this workload unless a
persistent disk is attached.
(https://community.render.com/t/increase-2gb-tmp-limit/22587)

Confirm current pricing on each vendor's own pricing page before committing.
I have verified the limits above, not the prices.

## 10. Data model

```
users          -- from Supabase Auth, plus an is_admin flag

jobs           id, user_id, source_filename, source_path, status, error,
               duration_seconds, width, height, fps, created_at

clips          id, job_id, sequence, label, start_seconds, end_seconds,
               output_filename, output_path, status, rendered_duration, error
```

Status: `uploading -> probing -> awaiting_cuts -> rendering -> complete | failed`

## 11. Still open

1. **Your worst realistic source.** 1 hour at 1080p, or 3 hours at 4K? This sets
   the volume size and the monthly bill. Everything else is decided.
2. **Retention.** How long do sources and clips survive before auto-delete?
   Storage on multi-GB files is the main recurring cost.
3. **Team size and allowlist.** Which email domain, or which addresses?

## 12. Phases

**Phase 1** - login, upload, paste timestamps, exact cutting, sequential naming,
zip download, manifest. Fully usable with no transcription and no API spend.

**Phase 2** - transcription, transcript shown beside the video, click a sentence
to fill in a timestamp. Provider sits behind an interface so it can be swapped.

**Phase 3** - only if real use demands it: smart cut for faster renders, 9:16
versions, burned-in captions.

Phase 1 deliberately has no transcription so the exactness of the cutting, which
is the actual product, is proven before any recurring API cost.

---

## Change log from v1

- Four services became two. Vercel dropped; UI and worker share one container.
- Multi-user access moved from "out of scope" into Phase 1.
- Render replaced by Railway, on a verified 2 GB `/tmp` cap.
- Supabase Pro confirmed as a hard requirement, not a preference.
- Manifest CSV dropped; JSON only.
- Open questions cut from five to three.
