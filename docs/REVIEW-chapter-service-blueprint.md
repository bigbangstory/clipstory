# Review: "Video Timestamp & Chapter Generation Service" blueprint

Reviewed: 2026-09-10
Reviewer conclusion: **do not adopt as the basis for Clipstory.** Harvest three
small pieces, discard the architecture.

---

## 1. It does not do the job

The blueprint never cuts a video. There is no ffmpeg trim, no clip output, no
sequential naming, no zip. It ends at `formatted_output`, a string of chapter
titles for pasting into a YouTube description.

Clipstory's requirement is N playable MP4 files cut at exact points. This
blueprint produces zero video files. Steps 4, 5 and 6 of our agreed flow are
absent. Roughly the first half of the pipeline (upload, ffmpeg audio extract,
faster-whisper transcribe) overlaps with our Phase 2; the half that is the
actual product does not exist here.

## 2. It contradicts the decisions you made yesterday

You said: no LLM decides the cuts, you supply the timestamps.

This blueprint's entire purpose is an LLM deciding the divisions. `app/llm.py`
asks a model to emit `start_seconds`, and the returned integer is trusted
without a single check. Specifically, nothing verifies that:

- `start_seconds` corresponds to any real transcript segment boundary
- chapters are in ascending order
- the first chapter is actually 0, despite the prompt demanding it
- `start_seconds` is less than `duration_seconds`

The model is free to return 847 when nothing happens at 847. That is the
"cutting blindly" failure you explicitly rejected, with an extra layer of
plausibility on top because the number arrives inside valid JSON. `response_format`
guarantees the JSON parses. It guarantees nothing about whether the numbers
are true.

Also note `word_timestamps=False` in `transcriber.py`. Word-level timing is
precisely what lets a cut land on a word boundary. It is switched off.

## 3. Verified blocker: Render's 2 GB /tmp limit

The blueprint writes the source video to `/tmp/video_processing/<uuid>/`.

Render caps temporary storage at **2 GB, on every instance type**, and kills the
service with "Evicted. Size of temporary storage volume /tmp exceeded the limit
of 2GB" when exceeded. This is not a free-tier restriction; paid instances have
the same cap and the feature request to raise it is still open.
(https://community.render.com/t/increase-2gb-tmp-limit/22587,
https://render.com/docs/disks)

A 90-minute 1080p source at a typical 5-8 Mbps is roughly 3.4 to 5.4 GB. It
does not fit. The extracted audio is not the problem (16 kHz mono 16-bit PCM is
32 KB/s, about 115 MB per hour), the source video is.

Consequence: on Render this design works for short demo clips and dies on
exactly the long-form content it was written for. Fixing it means never landing
the whole video on the container disk, which means object storage plus ranged
or streamed reads, which is a different architecture, not a patch.

## 4. Correction to an assumption worth stating

I expected to report that the synchronous request would hit a platform timeout.
It would not: Render supports a request timeout of up to 100 minutes
(https://render.com/blog/sharp-opinions-clean-infrastructure-how-cynical-sally-runs-on-render).
So Render's load balancer is not what breaks this.

What breaks it is the next item.

## 5. The event loop is blocked, and it will get the job killed

`process_video` is declared `async def`, but every expensive call inside it is
synchronous and blocking:

- `shutil.copyfileobj(...)` writing the upload
- `subprocess.run(...)` inside `extract_audio`
- `self.model.transcribe(...)` inside `Transcriber`

FastAPI runs an `async def` handler directly on the event loop. It does not
offload it to a threadpool the way it does for a plain `def` handler. So for the
entire duration of a transcription, which on CPU int8 is minutes to tens of
minutes, the process serves nothing else.

Combined with `--workers 1`, the failure chain is concrete:

1. A long transcription starts and freezes the loop.
2. Render's health check hits `/health`.
3. `/health` cannot respond, because the loop is blocked.
4. Render marks the service unhealthy and restarts it.
5. The job is destroyed mid-flight, with no persisted state to resume from.

The minimum fix is changing `async def` to `def` so Starlette offloads to a
worker thread. The correct fix is a job queue and a separate worker, which is
what our own spec already calls for.

There is also no job record anywhere. A restart, redeploy, dropped connection
or closed laptop loses the work with no way to recover or even to see that it
happened.

## 6. Security defects

**SSRF, high severity.** `/api/v1/generate-timestamps` accepts an arbitrary
`video_url` and fetches it server-side with `follow_redirects=True`. There is
no scheme check, no host allowlist, and no private-address block. An attacker
can reach the cloud metadata endpoint, internal services, or anything else the
container can route to, and redirects mean an allowlist on the initial URL alone
would not be sufficient either.

**No authentication or rate limiting.** The endpoint is public. It spends your
LLM credits and pins your CPU. Anyone who finds the URL can run it in a loop.

**No upload size cap.** Unbounded write to a 2 GB volume. Trivially fills the
disk and evicts the service.

**Error detail disclosure.** `detail=str(exc)` returns raw exception text,
including full ffmpeg stderr, to the caller.

## 7. Functional bugs

**The 400 becomes a 500.** Inside the `try`, a failed URL fetch raises
`HTTPException(400)`. The bare `except Exception` catches it and re-raises it as
a 500. A user's bad URL is reported as an internal server error. Fix by
re-raising `HTTPException` before the generic handler.

**`resp.content` buffers the entire video in RAM.** `await client.get(...)`
reads the whole body into memory before writing it. A 3 GB source needs 3 GB of
RAM on an instance the blueprint recommends provisioning with 2 GB. Use
`client.stream()` and write in chunks.

**Model loads at import time.** `transcriber_instance = Transcriber()` at module
scope downloads and loads the weights during import. The model is not baked into
the image, so every cold start and every redeploy re-downloads from HuggingFace
onto an ephemeral filesystem, and startup blocks until it finishes. Pre-download
into a Docker layer.

**Transcript length is unbounded.** The full transcript is packed into one
prompt with no token counting, chunking or truncation. A 3-hour video is
plausibly 1,000 to 2,000 Whisper segments. The variable is named
`transcript_sample`, which suggests sampling was intended and never written. It
will fail on long input with no graceful degradation, which is the input this
service exists to handle.

**No VAD filter.** `faster-whisper` accepts `vad_filter=True`, which materially
reduces hallucinated text during silence. Not enabled. Whisper hallucinating a
sentence in a silent passage is a well-known failure mode and it directly
corrupts chapter boundaries.

**Extension hardcoded.** Every upload is written as `input_video.mp4` regardless
of actual container. ffmpeg sniffs content so this usually survives, but it is
misleading and will confuse debugging of a .mov or .mkv.

**Config is not real settings management.** `Settings(BaseModel)` evaluates
`os.getenv` at class definition time. It works, but `pydantic-settings`
`BaseSettings` is the intended tool and gives validation and a required-field
error instead of a silent empty `LLM_API_KEY`.

## 8. What is actually worth keeping

Three things, and they are genuinely fine:

1. **The ffmpeg audio extraction command** in `app/audio.py`. `-vn -acodec
   pcm_s16le -ar 16000 -ac 1` is exactly right for Whisper. Reuse verbatim.
2. **The `faster-whisper` usage pattern**, once `word_timestamps=True` and
   `vad_filter=True` are switched on. This becomes one implementation behind our
   `TranscriptionProvider` interface.
3. **The Dockerfile shape.** Correct base image, ffmpeg installed properly,
   `PORT` respected. Drop `build-essential` and `curl` from the final image or
   use a multi-stage build to cut size and attack surface.

## 9. Recommendation

Keep the Clipstory spec in `docs/REQUIREMENTS.md` as the plan of record. Lift the
three items above into Phase 2. Do not adopt the synchronous single-container
request/response model, the `/tmp` staging, or the LLM chapter selection.

The one design idea worth stealing outright is accepting a **video URL** as an
alternative to browser upload. For multi-GB sources, pointing the worker at a
Drive or S3 link is often more reliable than a browser upload. It must be built
with an allowlist and private-address blocking, not the open fetch shown here.
