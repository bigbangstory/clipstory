# Free hosting options for the testing phase

Researched 2026-09-10. All limits below verified against vendor docs; prices and
included quotas should be re-checked on each vendor's own page before you rely
on them.

Goal: run Clipstory end to end, remotely, for zero cost, long enough to prove the
cuts land exactly where asked.

---

## What got ruled out, and why

**Supabase free** caps uploads at **50 MB per file**. Your agreed worst case is a
1 hour 1080p source at 2 to 4 GB. Off by a factor of forty to eighty. Storage is
unusable free; auth and Postgres on the same free plan are fine.
(https://supabase.com/docs/guides/storage/uploads/file-limits)

**Render free** caps `/tmp` at **2 GB on every instance type** and evicts the
service when exceeded. It also spins down when idle. Cannot hold your source
file. (https://community.render.com/t/increase-2gb-tmp-limit/22587)

**Railway** has no ongoing free tier, only trial credit. Hobby volumes cap at
5 GB. (https://docs.railway.com/volumes/reference)

**Fly.io has no free tier any more.** It is a free *trial* of 2 hours of machine
runtime or 7 days, whichever comes first, then pay-as-you-go with no free
allowances. (https://fly.io/docs/about/free-trial/)

**Hugging Face Spaces free CPU Basic** gives 2 cores, 16 GB RAM and 50 GB of
non-persistent disk, which would be ample. But **creating a Docker Space
requires a paid plan**. Free covers static Spaces only, so we cannot ship a
container with ffmpeg. (https://huggingface.co/pricing)

That leaves one strong option and one compromise.

---

## Option A: Oracle Cloud Always Free VM (recommended)

One free virtual machine that runs everything: the web app, the ffmpeg worker,
Postgres, and the video files themselves. No object storage vendor needed.

**What you get, free indefinitely, not a trial:**
- 2 OCPUs and 12 GB RAM of Ampere A1 ARM compute
- **200 GB of block storage**
- (https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)

200 GB of disk is the number that matters. It swallows your 2 to 4 GB sources
with room to spare, which is exactly what every managed free tier refuses to do.

**Public HTTPS with no open ports:** a free Cloudflare Tunnel gives the VM a
public HTTPS URL. Nothing is exposed directly to the internet.

**Total monthly cost: zero.** Nothing to cancel, no card charged, no trial clock.

### The honest caveats

1. **Ampere A1 capacity is a lottery.** "Out of host capacity" errors on free A1
   instances are common in popular regions and you may need to retry, or pick a
   less busy region. This is the main friction and it is real.
2. **The allocation was cut.** It was 4 OCPUs and 24 GB RAM; as of August 2026
   it is 2 OCPUs and 12 GB. Still plenty here, but do not trust older blog posts
   quoting the bigger numbers.
3. **It is ARM, not x86.** ffmpeg and x264 build and run fine on arm64, but
   encoding will be slower than the 15 to 40 seconds per clip I estimated for
   x86. I am not going to invent a number for it. We benchmark it on the first
   real clip and you will have a measured figure the same day.
4. **You administer a VM.** Not hard, but it is a machine you own rather than a
   dashboard. I would set it up with Docker Compose so there is one command to
   deploy and one file to read.
5. **Oracle can reclaim idle Always Free compute.** Keep it in use, or accept
   the risk on a testing box.

---

## Option B: Managed free tiers, small test files only

Supabase free for auth, Postgres and storage, plus any free container host.
Zero infrastructure work, deploy in an afternoon.

**The catch is the 50 MB file cap.** You cannot test a real 1 hour video. You
test with a 5 to 10 minute 720p clip instead.

This is less useless than it sounds. What the testing phase actually has to
prove is that a cut lands on the exact frame requested, that clips are named and
sequenced correctly, that the zip works, and that the parser accepts your
timestamp formats. **Every one of those is provable on a 5 minute video.** Frame
accuracy does not care how long the source is.

What it will not prove: behaviour on multi-GB uploads, resumable upload
reliability, render time on a full-length source, and disk pressure. Those are
exactly the things that break when you go to real content, so they get tested
properly the day you move off free.

---

## Option C: Cloudflare R2 for storage, if you want real file sizes without a VM

R2's free tier is **10 GB of storage, 1 million Class A operations, 10 million
Class B, and zero egress fees**, which is unusually generous and has no per-file
size problem for your sources.
(https://developers.cloudflare.com/r2/pricing/)

It solves storage cleanly. It does not solve compute, and compute is the harder
half: there is no free container host left that will give you ffmpeg plus enough
disk to stage a 4 GB file. So R2 is a good component, not a complete answer. It
pairs well with Option A if you later want files off the VM, and it is the
natural first paid upgrade if you skip Oracle.

---

## Recommendation

**Take Option A.** It is the only free path that runs your real workload at real
file sizes, and "free" means free indefinitely rather than a trial that expires
mid-test. Budget an hour of setup friction for the capacity lottery.

**Fall back to Option B** if the Oracle setup annoys you or A1 capacity refuses
to appear. Testing on 5 minute clips genuinely validates the part of this tool
that is hard to get right, and you lose nothing permanent by starting there. The
code is identical either way.

Either way, **build the storage layer behind an interface** from day one: a
`Storage` class with `put`, `get`, `signed_url`, `delete`. Local disk on the VM
for now, S3-compatible for R2 or Supabase later. Swapping is then one class, and
this is the single decision that keeps the free-tier choice from becoming a
rewrite when you start paying.

## Note on the local LLM

The clip picker now also runs on the VM (Ollama, `qwen2.5:7b-instruct`, about
4.7 GB on disk, 5 to 6 GB RAM while answering). It fits the 12 GB Always Free
allocation because the worker never runs it at the same time as Whisper. Speed
on two ARM cores is unmeasured; `docs/DEPLOY.md` step 7 says what to record.

## What changes in the spec

Nothing architectural. `docs/REQUIREMENTS.md` still describes one container
running the UI and worker, with Postgres as the queue. Only the host changes:
Oracle VM instead of Railway, local disk instead of Supabase Storage, both behind
the storage interface above. When you are ready to pay, moving to Railway plus
Supabase Pro is a config change and a deploy, not a rebuild.
