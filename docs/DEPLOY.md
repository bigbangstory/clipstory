# Deploying Clipstory on a free Oracle Cloud VM

Target: a working, private, team-accessible install at zero cost.
Time: about an hour, most of it waiting on Oracle.

---

## What you are building

One VM running three containers: the web app, the ffmpeg worker, and Postgres.
A Cloudflare Tunnel gives it a public HTTPS address without opening a single
inbound port. Video files live on the VM's own disk.

## Step 1: Create the Oracle VM

1. Sign up at [cloud.oracle.com](https://cloud.oracle.com) (a card is required
   for identity checks; Always Free resources are not charged).
2. Compute → Instances → **Create instance**.
3. Change the shape to **Ampere / VM.Standard.A1.Flex**, and set **2 OCPUs and
   12 GB memory**. That is the current Always Free ceiling.
4. Image: **Canonical Ubuntu 24.04**.
5. Boot volume: raise it to **190 GB**. The Always Free allowance is 200 GB
   total across volumes, so leaving a little headroom avoids surprises.
6. Add your SSH public key. Create.

**If you get "Out of host capacity"** you have hit the well-known A1 shortage.
It is not something you have done wrong. Options, in order of effort: retry
every few hours, pick a different Availability Domain in the same region, or
create the instance in a less busy home region. Persistence usually wins within
a day.

## Step 2: Prepare the machine

```bash
ssh ubuntu@<your-instance-ip>

sudo apt-get update && sudo apt-get upgrade -y
sudo apt-get install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER
newgrp docker
```

Oracle images ship with a restrictive iptables policy. We are not opening any
inbound port, so leave it alone. The tunnel connects outbound.

## Step 3: Get the code and configure it

```bash
git clone <your-repo-url> clipstory && cd clipstory
cp .env.example .env

# Generate the two secrets
echo "SECRET_KEY=$(openssl rand -hex 32)" >> .env
echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)" >> .env

nano .env    # set ADMIN_EMAILS and BASE_URL
```

`ADMIN_EMAILS` is who can sign in first and invite everyone else.
`BASE_URL` must be the address the browser will actually use, including
`https://`, because sign-in links are built from it.

## Step 4: Start it

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f web
```

Wait for `clipstory web ready`. Then check it locally:

```bash
curl localhost:8000/healthz     # {"status":"ok"}
```

## Step 5: Put it on the internet with Cloudflare Tunnel

You need a domain on Cloudflare (a free plan is fine).

1. Cloudflare dashboard → **Zero Trust** → Networks → **Tunnels** → Create a
   tunnel → **Cloudflared**.
2. Name it `clipstory`, then copy the install command it shows for Debian and
   run it on the VM.
3. Under **Public Hostname**, add:
   - Subdomain: `clips`, Domain: `yourcompany.com`
   - Service: `HTTP` → `localhost:8000`
4. Save.

`https://clips.yourcompany.com` is now live. No inbound port was opened; the
tunnel dials out to Cloudflare.

Make sure `BASE_URL` in `.env` matches that address exactly, then
`docker compose up -d` to pick it up.

## Step 6: Sign in

No email provider is configured yet, so the sign-in link is written to the log.
That is intended for exactly this moment.

1. Go to `https://clips.yourcompany.com`, enter your admin address.
2. On the VM:

```bash
docker compose logs web | grep "SIGN-IN LINK"
```

3. Open the link. You are in, as an admin.

## Step 7: Turn on email, then invite your team

Until you do this, only someone who can read the server log can sign in.

1. Create a free [Resend](https://resend.com) account and an API key.
2. Add to `.env`:

```
RESEND_API_KEY=re_xxxxxxxx
MAIL_FROM=Clipstory <clips@yourcompany.com>
```

Verify your sending domain in Resend, or keep the default
`onboarding@resend.dev` for testing.

3. `docker compose up -d`
4. Go to **Team** and invite people by email address.

Only invited addresses can sign in. Anyone else who has the URL sees a login
page and gets no further.

---

## Running it

**Watch a render**
```bash
docker compose logs -f worker
```

**Update to a new version**
```bash
git pull && docker compose up -d --build
```
Jobs survive: state is in Postgres and files are on a volume. A job interrupted
mid-render is returned to the queue and picked up again.

**Back up the database**
```bash
docker compose exec db pg_dump -U clipstory clipstory | gzip > backup-$(date +%F).sql.gz
```
Worth doing before an upgrade. The clips themselves are reproducible from the
source, so the database matters more than the files.

**Check disk**
```bash
df -h /
docker compose exec worker du -sh /data/sources /data/clips
```

The app refuses an upload when free space is under 1.5x the file size, so you
get a clear message rather than a render that dies halfway.

---

## Sizing against your agreed worst case

A 1 hour 1080p source is roughly 2 to 4 GB. With a 190 GB volume you can hold
dozens of jobs at once. Retention keeps it that way on its own: the source is
deleted as soon as its clips render, and clips are purged after 30 days.

**Expect renders to be slower than on x86.** Ampere A1 is ARM, and ffmpeg's
x264 encoder is well optimised there but not identical. The 15 to 40 seconds
per 1080p clip quoted in the requirements was an x86 estimate. Measure it on
your first real job:

```bash
docker compose logs worker | grep "cut .* drift"
```

Every line reports the actual render and the measured drift from the requested
duration. If renders are slower than you can live with, the options in order of
value are: raise `RENDER_CONCURRENCY` to 2, change `VIDEO_PRESET` in
`app/media.py` from `veryfast` to `superfast`, or move to a paid x86 instance.
Do not switch to stream copy to gain speed; it breaks the exactness this tool
exists for, and a test will fail if you try.

## If something goes wrong

**Sign-in links do not arrive.** Without `RESEND_API_KEY` they never will, by
design. Read them from the log. With a key set, check
`docker compose logs web | grep -i email` for a provider rejection, usually an
unverified sending domain.

**"not enough disk space" when uploading.** `df -h /` and delete old jobs, or
lower `CLIP_RETENTION_DAYS`.

**A job is stuck in `rendering`.** The worker probably died. It is picked up
again automatically after three hours; to force it now, restart the worker with
`docker compose restart worker`.

**A clip failed with a drift message.** The rendered clip did not match the
requested duration within one frame, so it was rejected rather than delivered.
Usually the range runs past the end of the video, or the source has an unusual
variable frame rate. The job page names the clip and the reason.
