# Clipstory - Requirements (v4)

Status: agreed. Supersedes v1 to v3; change log at the bottom.
Last updated: 2026-09-14

## 1. The brief

> "when i upload the main long video, it would transcribe the video and then
> crop the video into multiple parts. then once the clipping is done it would
> name it as per sequence."

Three verbs: transcribe, crop, name. Everything below serves those.

## 2. The flow

1. **Upload** a long video from any browser. Goes up in chunks; resumes.
2. **Transcribe** on the server (Whisper). Text with word-level timings.
3. **AI picks clips** on the server (local model via Ollama). It answers with
   transcript segment numbers; the timestamps come from Whisper.
4. **Cut every pick automatically**, frame-accurate, verified after render.
5. **Review** a page of finished, playable clips. Tweak only if needed:
   delete, nudge by a second or a word, drag edges on a timeline, add from
   the player or transcript, or edit as text.
6. **Apply** re-renders only the clips that changed.
7. **Finalise** renumbers `clip_01..N`, deletes the source, and locks the job.
8. **Download** clips individually or as a zip with `manifest.json`.

"Precut" is the default. A human is never required, only allowed.

## 2a. The second mode: editing by text

The same upload and the same transcript, used differently. The transcript is a
document; striking words out cuts them from the video.

1. **Strike words out** by dragging and pressing Delete, or **one-click
   cleanup**: filler words removed, long pauses shortened.
2. **Preview instantly.** The player seeks past deleted ranges. Nothing is
   encoded while editing, which is what makes this affordable on 2 ARM cores.
3. **Export** re-encodes the whole video in the background.

Deletions are stored; keep-ranges are derived from them and the source
duration, so the browser preview and the render cannot disagree.

Two rules make the output correct:

- **Cuts are snapped into silence.** Every deletion is widened to the middle of
  the gap between the surrounding words, so a join lands where nobody is
  speaking rather than clipping a word's attack.
- **Ranges are frame-aligned before rendering,** and boundaries are placed
  half a frame off so the selection comparison is unambiguous. Measured: the
  output frame count is then exactly the sum of the aligned ranges, from 2 up
  to 281 segments.
- **`select`, never `trim`+`concat`.** trim+concat holds the whole span from
  the first kept range to the last in memory, so its cost is set by the length
  of the source rather than the number of cuts. Measured on a 60-second 1080p
  source with 20 segments: **4.40 GB** peak for trim+concat against **0.38 GB**
  for select, and select ran 2.6x faster. An hour of 1080p through trim+concat
  would need hundreds of gigabytes and be killed on a 12 GB box.
- **`gte(t,S)*lt(t,E)`, never `between()`.** `between` is inclusive at both
  ends and keeps one extra frame per segment. Measured: 200 segments produced
  2600 frames for a 2400-frame request, and 6.4 seconds of silent A/V desync.
- **Audio is repacketised to one video frame per chunk before selection.**
  Audio is selected in whole packets (~21ms) and video in whole frames, so
  without this the two drift apart as a random walk. Measured over 281 cuts:
  54ms out of sync without it, 0ms with it.
- **Verification asserts the exact frame count,** not an approximate duration,
  and separately checks audio against video. A tolerance that grows with the
  number of cuts would hide exactly the bugs above.

Filler defaults are `um, uh, erm, uhm, mm, hmm, ah, er, eh`. Conversational
words (`so, like, right, you know, I mean`) are opt-in only: each is usually
load-bearing, and removing them by default would change what someone said.
Pauses over 0.8s are shortened to 0.4s, never removed outright.

## 3. Constraints

- Nothing installed on any laptop. Fully hosted.
- Invite-only access. Anyone not on the list gets a login page and no further.
- Zero per-video cost during testing: transcription and clip picking run on
  the server, no external API by default.
- Cuts are exact. Never a keyframe snap.

## 4. Decisions locked

| Question | Decision |
|---|---|
| Hosting for testing | Oracle Cloud Always Free ARM VM (2 OCPU, 12 GB, 200 GB). Start with 10-15 minute videos to measure Whisper and LLM speed before feeding hour-long files. |
| Transcription | faster-whisper, `base`, int8, on the VM. Swappable. |
| Clip picking | Ollama + `qwen2.5:7b-instruct` on the VM. Swappable; hosted Claude available as a paid option. |
| Default UX | Picks are rendered before anyone looks. Review page leads with playable clips. |
| Manual layer | Accept/reject, nudge ±1s and ±word, timeline drag with word snap, add via player/transcript/text. |
| Source retention | Kept until Finalise (tweaking needs it), or the retention window. |
| Clips per job | Typically 5 to 10; rendered sequentially. |
| Worst source | 1 hour at 1080p, roughly 2-4 GB. |

## 5. Cut accuracy

Every clip is re-encoded so it starts on the exact frame requested:

```
ffmpeg -ss <start> -i <source> -t <duration> \
       -vf setpts=PTS-STARTPTS -af asetpts=PTS-STARTPTS \
       -c:v libx264 -crf 18 -preset veryfast -pix_fmt yuv420p \
       -c:a aac -b:a 192k -movflags +faststart <output>
```

After rendering, the clip is probed and rejected if its duration drifts more
than one frame from the request. `-avoid_negative_ts make_zero` is deliberately
absent: measurement showed it reinstates the timeline offset the `setpts`
filters remove (0.080s start, 5.022s for a 5.000s request). Verified on a
real file: cut at 17.400s on a source with 10s keyframes yields frame 435 at
17.400s; stream copy yields frame 250 at 10.000s and 12.48s of video.

## 6. AI safety rule

The model returns `start_segment` and `end_segment`, never seconds. The schema
has no field that could carry a timestamp. `resolve()` looks each number up in
the stored transcript and drops anything that does not exist, runs backwards,
or overlaps. Long transcripts are windowed on segment boundaries so a local
model's context window is never silently exceeded.

## 7. Naming

`{source_slug}_clip_{NN}[_{label-slug}].mp4`, zero-padded, widening past 99.
Numbering is settled at Finalise so deletions during review leave no gaps.

## 8. Access

Magic-link login restricted to an invite list managed by admins. Revoking an
invite ends existing sessions on the next request.

## 9. Retention

Source kept until Finalise. Clips and the job purged `CLIP_RETENTION_DAYS`
(30) after rendering; the job row stays as history, marked expired.

## 10. Not in scope

Paid hosting, vertical reframing, burned-in captions, multi-worker
parallelism, editing beyond trimming. Revisit only with measured numbers.

## Change log

- v4: added text-based editing as a second mode on the same upload, with
  one-click cleanup. Frame alignment and snap-to-silence established by
  measurement. Voice cloning, audio enhancement, captions and multitrack
  remain out of scope.
- v3: transcription and AI clip picking moved from "later" to the core, per
  the original brief. Local LLM replaces the hosted API. Precut default.
  Review-and-tweak layer added. Source retained until Finalise.
- v2: four services became two; team access added; Railway/Supabase sizing.
- v1: initial spec.
