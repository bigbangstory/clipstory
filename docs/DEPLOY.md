# Deploying Clipstory on a free Oracle Cloud VM

Target: a working, private, team-accessible install at zero cost, with
transcription and clip picking running on the box itself.
Time: about an hour, most of it waiting on Oracle and on the first model pull.

## What you are building

One VM running four containers: web app, worker, Postgres, and Ollama (the
local language model). A Cloudflare Tunnel gives it a public HTTPS address
without opening a single inbound port. Video files live on the VM's disk.

## Step 1: Create the Oracle VM

1. Sign up at [cloud.oracle.com](https://cloud.oracle.com). A card is required
   for identity checks; Always Free resources are not charged.
2. Compute, Instances, **Create instance**.
3. Shape: **Ampere / VM.Standard.A1.Flex**, **2 OCPUs, 12 GB memory**. That is
   the current Always Free ceiling.
4. Image: **Canonical Ubuntu 24.04**.
5. Boot volume: raise it to **190 GB**. The allowance is 200 GB total.
6. Add your SSH public key. Create.

**"Out of host capacity"** means you hit the well-known A1 shortage, not a
mistake. Retry every few hours, try another Availability Domain, or a less busy
home region. Persistence usually wins within a day.

## Step 2: Prepare the machine

```bash
ssh ubuntu@<your-instance-ip>
sudo apt-get update && sudo apt-get upgrade -y
sudo apt-get install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER
newgrp docker
```

Leave Oracle's restrictive firewall alone. Nothing inbound is needed.

## Step 3: Get the code and configure it

```bash
git clone <your-repo-url> clipstory && cd clipstory
cp .env.example .env
echo "SECRET_KEY=$(openssl rand -hex 32)" >> .env
echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)" >> .env
nano .env    # set ADMIN_EMAILS and BASE_URL
```

`ADMIN_EMAILS` is who signs in first and invites everyone else. `BASE_URL`
must be the address the browser will use, with `https://`, because sign-in
links are built from it. The transcription and suggestion defaults are fine.

## Step 4: Start it

```bash
docker compose up -d --build
docker compose ps
```

Two things happen on first start that do not happen again:

- The image build pre-downloads the Whisper `base` model (about 150 MB).
- `ollama-pull` fetches `qwen2.5:7b-instruct` (about 4.7 GB). Watch it with
  `docker compose logs -f ollama-pull`; it exits when done. Until then, jobs
  transcribe fine but report "could not reach Ollama" for suggestions. Just
  press **Ask the AI again** on the job once the pull has finished.

Then:

```bash
docker compose logs -f web      # wait for "clipstory web ready"
curl localhost:8000/healthz     # {"status":"ok"}
```

## Step 5: Put it on the internet with Cloudflare Tunnel

You need a domain on Cloudflare (free plan is fine).

1. Cloudflare dashboard, **Zero Trust**, Networks, **Tunnels**, Create a
   tunnel, **Cloudflared**.
2. Name it `clipstory`, copy the Debian install command it shows, run it on
   the VM.
3. Public Hostname: subdomain `clips`, your domain, service `HTTP` to
   `localhost:8000`. Save.

`https://clips.yourcompany.com` is live. Make sure `BASE_URL` in `.env`
matches it exactly, then `docker compose up -d` to pick it up.

## Step 6: Sign in

No email provider is configured yet, so the sign-in link goes to the log. That
is intended for exactly this moment.

1. Open the site, enter your admin address.
2. `docker compose logs web | grep "SIGN-IN LINK"`
3. Open the link. You are in, as an admin.

## Step 7: The first real video, and the numbers to write down

Upload a **10 to 15 minute** video first, not an hour-long one. Then:

```bash
docker compose logs -f worker
```

Three lines tell you what this machine can do:

```
transcribed 142 segments (en) in 812s with faster-whisper      <- Whisper speed
asking ollama for 8 clips in window 1/1 (142 segments, ~2900 tokens)
suggested 6 clips from 142 segments (7 proposed, 1 dropped as invalid)
cut ... at 17.400s for 42.000s, drift 0ms                        <- render speed per clip
```

Divide the transcription time by the video length: that ratio is your
Whisper speed on this box, and it tells you how long an hour will take. Do
the same for the gap between "asking ollama" and "suggested". Record both
here once you have them; they are unmeasured until you do, and every
estimate in these docs is exactly that.

If Whisper is slower than you can live with: `WHISPER_MODEL_SIZE=tiny` is
roughly twice as fast and noticeably less accurate. If the model is the slow
part: a smaller model such as `qwen2.5:3b-instruct` in `SUGGEST_MODEL`, then
`docker compose up -d` (the pull runs again for the new name).

## Step 7a: The first text export, and the number to write down

The clip flow re-encodes 60 seconds at a time. A **text export re-encodes the
whole video**, so it is the slowest thing this machine will ever do, and it is
the one figure in these docs that is genuinely unknown until you measure it.

Open a job, switch to **Edit**, run cleanup, press Export, then:

```bash
docker compose logs -f worker
```

```
rendered edit podcast-ep12_edited.mp4: 34 segments, 151.080s kept, drift 0ms, took 92.4s
```

Divide the time taken by the source length: that ratio tells you what an hour
will cost. Record it here once you have it.

If it is slower than you can live with, in order of what to try first:

1. `VIDEO_PRESET` in `app/media.py` from `veryfast` to `superfast` or
   `ultrafast`. Bigger files, much faster encode, no loss of accuracy.
2. Export shorter sections rather than whole hours.
3. A paid x86 box.

Do not reach for stream copy to gain speed. It cannot cut on a non-keyframe,
which breaks the guarantee the whole tool exists for, and the tests will fail.

## Step 8: Email, then invite the team

1. Free [Resend](https://resend.com) account, API key.
2. In `.env`: `RESEND_API_KEY=re_...` and `MAIL_FROM=Clipstory <clips@yourcompany.com>`
   (verify the sending domain in Resend, or keep `onboarding@resend.dev` for testing).
3. `docker compose up -d`
4. **Team** page, invite by email address.

Only invited addresses can sign in.

## Running it

| Task | Command |
|---|---|
| Watch a job | `docker compose logs -f worker` |
| Update | `git pull && docker compose up -d --build` (jobs survive; a job mid-flight is picked up again) |
| Back up the database | `docker compose exec db pg_dump -U clipstory clipstory \| gzip > backup-$(date +%F).sql.gz` |
| Disk | `df -h /` and `docker compose exec worker du -sh /data/sources /data/clips` |
| Watch a text export | `docker compose logs -f worker \| grep "rendered edit"` |
| Switch to hosted Claude for picking | `SUGGEST_PROVIDER=anthropic`, `ANTHROPIC_API_KEY=...` in `.env`, then `docker compose up -d`. Costs per video. |

The app refuses an upload when free space is under 1.5x the file size, so you
get a clear message rather than a render that dies halfway.

## Memory on the 12 GB box

A text export holds about **0.4 GB** regardless of how long the source is or
how many cuts the edit has, because the renderer streams rather than buffering
the timeline. This was not free: the obvious filtergraph shape (`trim` plus
`concat`) peaks at 4.40 GB for a single minute of 1080p and would be killed
outright on an hour. If you ever change `app/media.py`, the tests enforce the
streaming shape.


The worker runs one stage at a time, never Whisper and the LLM together.
Whisper `base` int8 needs about 1 GB; the 7B model about 5 to 6 GB while
answering; Postgres and the app under 1 GB. If you raise `OLLAMA_NUM_CTX`
much above 16384 or move to a larger model, watch `docker stats` during a
suggestion pass.

## If something goes wrong

**"could not reach Ollama".** The model pull has not finished, or the
`ollama` container is down. `docker compose ps`, then `docker compose logs
ollama-pull`. Press **Ask the AI again** on the job afterwards.

**Every job says "no speech was found".** Check the source has an audio track
(`ffprobe` it). If it does, try `WHISPER_MODEL_SIZE=small`.

**Sign-in links do not arrive.** Without `RESEND_API_KEY` they never will, by
design; read them from the log. With a key set, check
`docker compose logs web | grep -i email` for a provider rejection.

**A job is stuck in transcribing or rendering.** The worker died. It is
returned to the queue automatically after four hours; to force it now,
`docker compose restart worker`.

**A clip failed with a drift message.** It did not match the requested
duration within one frame, so it was rejected rather than delivered. Usually
the range runs past the end of the video. Fix the edge on the review page and
Apply.
