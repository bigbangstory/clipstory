# Clipstory - Requirement Specification (v2, simplified)

Status: **agreed. All open questions closed. Phase 1 ready to build.**
Last updated: 2026-09-10 (decisions locked)
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

## 7. Access: invite-only

Magic-link login, no passwords to manage.

**Invite-only.** An admin adds an email address to the allowlist; only addresses
on that list can log in. Anyone else who reaches the URL sees a login page and
gets nowhere. This covers teammates, freelancers and clients on any domain
without opening the door to strangers.

Rejected: open access for anyone with the link. It would let strangers upload
gigabytes and burn CPU on your bill, and make you the host of whatever they
uploaded.

Every user sees only their own jobs. An admin flag can see all of them, and
manages the invite list from a simple settings page.

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

## 9. Sizing, against the agreed worst case

Agreed worst case: **1 hour at 1080p**, roughly 2 to 4 GB per source.

**Supabase Pro is required.** The free tier caps uploads at 50 MB per file,
which a 2 GB source misses by a factor of forty. Pro raises the ceiling to
500 GB. This is a hard gate, not a preference.
(https://supabase.com/docs/guides/storage/uploads/file-limits)

**Railway Pro is recommended, Hobby is too tight.** Volume caps are Trial
500 MB, Hobby 5 GB, Pro up to 1 TB. A 4 GB source on a 5 GB Hobby volume leaves
under 1 GB for the rendered clips, and 10 minutes of 1080p output can fill that.
Pro removes the constraint entirely. (https://docs.railway.com/volumes/reference)

**Render is ruled out.** It caps `/tmp` at 2 GB on every instance type, paid
included, and evicts the service when exceeded.
(https://community.render.com/t/increase-2gb-tmp-limit/22587)

**Storage bill stays small** because of the retention rule in section 9a: the
source, which is the expensive object, is deleted as soon as its clips render.
Only the clips persist, and a job of 5 to 10 short clips is a few hundred MB.

Vendor limits above are verified. Confirm current prices and included storage on
each vendor's own pricing page before you subscribe; I have not verified those.

## 9a. Retention

- **Source video**: deleted immediately after its clips render successfully. If
  the job fails, the source is kept so it can be retried without re-uploading.
- **Clips and manifest**: kept 30 days, then auto-deleted.
- A daily cleanup task enforces both. Users see the deletion date on the job.

## 10. Data model

```
users          -- from Supabase Auth, plus an is_admin flag

jobs           id, user_id, source_filename, source_path, status, error,
               duration_seconds, width, height, fps, created_at

clips          id, job_id, sequence, label, start_seconds, end_seconds,
               output_filename, output_path, status, rendered_duration, error
```

Status: `uploading -> probing -> awaiting_cuts -> rendering -> complete | failed`

## 11. Decisions locked

| Question | Decision |
|---|---|
| Worst realistic source | 1 hour, 1080p (roughly 2-4 GB) |
| Retention | Source deleted after render; clips kept 30 days |
| Who can log in | Invite-only list of email addresses |
| Clips per job | Typically 5 to 10 |

**Consequence of 5 to 10 clips per job: rendering runs sequentially.** At roughly
15 to 40 seconds per 1080p clip, a typical job finishes in 2 to 7 minutes. That
is well inside anyone's patience, so no parallel rendering is built. A
concurrency setting is left in the config in case real timings prove otherwise.

Nothing is blocking. Phase 1 can start.

The only input still needed from you is operational, not architectural: the list
of email addresses to seed the invite allowlist. That can be added after the
first deploy.

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
- Open questions cut from five to three, then all three answered and closed.
- Worst case fixed at 1 hour 1080p; sequential rendering confirmed as sufficient.
- Access settled as invite-only; open-link access explicitly rejected.
- Retention settled: source deleted after render, clips kept 30 days.
