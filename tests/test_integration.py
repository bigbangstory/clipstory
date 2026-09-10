"""End-to-end tests through the real HTTP API against a real Postgres.

Covers the whole path: sign in, upload in chunks, probe, transcribe (fake),
suggest (fake), auto-render, then review: delete one clip, nudge one, add one,
apply, confirm only the changed ones re-rendered, finalise, download the zip.
The final assertion re-checks frame accuracy on a clip that came out of the
full pipeline.

Skipped unless TEST_DATABASE_URL points at a usable Postgres.
"""
from __future__ import annotations

import os
import zipfile

import pytest

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")

from app import auth, jobs  # noqa: E402
from app.config import settings  # noqa: E402
from tests.conftest import extract_frame, psnr_between, requires_ffmpeg  # noqa: E402

ADMIN = "admin@example.com"
MEMBER = "member@example.com"
OUTSIDER = "stranger@example.com"


@pytest.fixture(scope="module")
def client(database):
    from fastapi.testclient import TestClient

    from app.web import app

    with TestClient(app) as test_client:
        yield test_client


def sign_in(client, email: str):
    """Log in by consuming a real magic link, exercising the true auth path."""
    url = auth.request_login_link(email)
    token = url.split("token=")[1]
    client.cookies.clear()
    response = client.get(f"/auth/verify?token={token}", follow_redirects=False)
    assert response.status_code == 303, response.text
    return response


def upload(client, source_path, filename="Podcast Ep12.mp4") -> str:
    payload = source_path.read_bytes()
    response = client.post("/api/uploads", data={"filename": filename, "total_bytes": len(payload)})
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    chunk = max(len(payload) // 4, 1)
    offset = 0
    while offset < len(payload):
        response = client.put(f"/api/uploads/{job_id}", content=payload[offset:offset + chunk],
                              headers={"X-Chunk-Offset": str(offset)})
        assert response.status_code == 200, response.text
        offset = response.json()["received_bytes"]
    assert client.post(f"/api/uploads/{job_id}/complete").status_code == 200
    return job_id


def run_worker_once() -> dict | None:
    job = jobs.claim_next_job("test-worker")
    if job is None:
        return None
    from app import worker

    worker.process(job)
    return jobs.get_job(str(job["id"]))


def drain_worker():
    while run_worker_once() is not None:
        pass


class TestAccessControl:
    def test_uninvited_address_cannot_get_a_link(self, client):
        with pytest.raises(auth.NotInvited):
            auth.request_login_link(OUTSIDER)

    def test_login_page_does_not_reveal_whether_an_address_is_invited(self, client):
        invited = client.post("/login", data={"email": ADMIN}, follow_redirects=False)
        uninvited = client.post("/login", data={"email": OUTSIDER}, follow_redirects=False)
        assert invited.status_code == uninvited.status_code == 303
        assert invited.headers["location"] == uninvited.headers["location"]

    def test_anonymous_browser_is_sent_to_login(self, client):
        client.cookies.clear()
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/login"

    def test_bootstrap_admin_can_sign_in_and_is_admin(self, client):
        sign_in(client, ADMIN)
        assert client.get("/admin").status_code == 200

    def test_magic_link_works_only_once(self, client):
        token = auth.request_login_link(ADMIN).split("token=")[1]
        assert client.get(f"/auth/verify?token={token}", follow_redirects=False).status_code == 303
        again = client.get(f"/auth/verify?token={token}", follow_redirects=False)
        assert "error" in again.headers["location"]

    def test_admin_can_invite_and_revoke(self, client):
        sign_in(client, ADMIN)
        client.post("/admin/invite", data={"email": MEMBER}, follow_redirects=False)
        assert auth.is_invited(MEMBER)
        sign_in(client, MEMBER)
        assert client.get("/").status_code == 200
        assert client.get("/admin").status_code == 403
        sign_in(client, ADMIN)
        client.post("/admin/revoke", data={"email": MEMBER}, follow_redirects=False)
        assert not auth.is_invited(MEMBER)

    def test_revoking_kills_an_existing_session_immediately(self, client):
        sign_in(client, ADMIN)
        client.post("/admin/invite", data={"email": MEMBER}, follow_redirects=False)
        sign_in(client, MEMBER)
        member_cookies = dict(client.cookies)
        sign_in(client, ADMIN)
        client.post("/admin/revoke", data={"email": MEMBER}, follow_redirects=False)
        client.cookies.clear()
        for name, value in member_cookies.items():
            client.cookies.set(name, value)
        assert client.get("/", follow_redirects=False).status_code == 303

    def test_admin_cannot_revoke_themselves(self, client):
        sign_in(client, ADMIN)
        assert client.post("/admin/revoke", data={"email": ADMIN}).status_code == 400


@requires_ffmpeg
class TestPrecutPipeline:
    def test_upload_transcribe_suggest_autorender_review_finalise(
        self, client, marked_source, fake_providers, tmp_path
    ):
        transcriber, suggester = fake_providers
        sign_in(client, ADMIN)
        job_id = upload(client, marked_source)

        # --- worker: probe + transcribe + suggest, then auto-queue ----------
        job = run_worker_once()
        assert job["status"] == jobs.QUEUED, job
        assert transcriber.calls == 1 and suggester.calls == 1
        assert job["transcript_language"] == "en"
        assert job["duration_seconds"] == pytest.approx(30.0, abs=0.2)
        assert len(jobs.get_transcript(job_id)) == 6
        assert jobs.get_word_boundaries(job_id), "word boundaries stored for snapping"

        clips = jobs.get_clips(job_id)
        assert [c["label"] for c in clips] == ["Founder origin story", "Closing line"]
        # timestamps come from the (fake) transcript, never from the suggester
        assert (clips[0]["start_seconds"], clips[0]["end_seconds"]) == (5.0, 15.0)
        assert (clips[1]["start_seconds"], clips[1]["end_seconds"]) == (20.0, 25.0)
        assert all(c["status"] == jobs.CLIP_PENDING for c in clips)

        # --- worker: render, no human involved --------------------------------
        job = run_worker_once()
        assert job["status"] == jobs.COMPLETE, job["error"]
        clips = jobs.get_clips(job_id)
        assert all(c["status"] == jobs.CLIP_COMPLETE for c in clips)
        assert clips[0]["rendered_duration"] == pytest.approx(10.0, abs=0.04)
        assert job["source_deleted_at"] is None, "source must survive until finalise"

        page = client.get(f"/jobs/{job_id}")
        assert page.status_code == 200
        assert "Apply changes" in page.text and "Finalise" in page.text
        assert "podcast-ep12_clip_01_founder-origin-story.mp4" in page.text

        # --- review: delete clip 2, nudge clip 1, add a third -----------------
        first_id = clips[0]["id"]
        first_path = clips[0]["output_path"]
        response = client.post(f"/jobs/{job_id}/apply", json={"rows": [
            {"id": first_id, "start": 5.0, "end": 15.0, "label": "Founder origin story"},   # unchanged
            {"id": None, "start": 17.4, "end": 22.4, "label": "Added by hand"},             # new
        ]})
        assert response.status_code == 200, response.text
        assert response.json()["counts"] == {"unchanged": 1, "changed": 0, "added": 1, "deleted": 1}
        assert jobs.get_job(job_id)["status"] == jobs.QUEUED

        job = run_worker_once()
        assert job["status"] == jobs.COMPLETE, job["error"]
        clips = jobs.get_clips(job_id)
        assert [c["label"] for c in clips] == ["Founder origin story", "Added by hand"]
        assert clips[0]["output_path"] == first_path, "an unchanged clip is never re-rendered"
        assert clips[1]["status"] == jobs.CLIP_COMPLETE

        # --- review: nudge the first clip's start by one second ----------------
        response = client.post(f"/jobs/{job_id}/apply", json={"rows": [
            {"id": first_id, "start": 6.0, "end": 15.0, "label": "Founder origin story"},
            {"id": clips[1]["id"], "start": 17.4, "end": 22.4, "label": "Added by hand"},
        ]})
        assert response.json()["counts"]["changed"] == 1
        assert response.json()["counts"]["unchanged"] == 1
        assert not jobs.storage().exists(first_path), "a changed clip's old file is removed"
        job = run_worker_once()
        assert job["status"] == jobs.COMPLETE, job["error"]
        assert jobs.get_clips(job_id)[0]["rendered_duration"] == pytest.approx(9.0, abs=0.04)

        # --- text box path: identical lines leave everything untouched ---------
        text = "00:00:06.000 - 00:00:15.000 | Founder origin story\n00:00:17.400 - 00:00:22.400 | Added by hand\n"
        response = client.post(f"/jobs/{job_id}/cuts", data={"cuts": text}, follow_redirects=False)
        assert response.status_code == 303
        assert jobs.get_job(job_id)["status"] == jobs.COMPLETE, "nothing changed, nothing re-queued"

        # --- validation: bad rows never reach the database ---------------------
        assert client.post(f"/jobs/{job_id}/apply", json={"rows": [{"start": 10, "end": 5}]}).status_code == 400
        assert client.post(f"/jobs/{job_id}/apply", json={"rows": [{"start": 10, "end": 99}]}).status_code == 400
        bad = client.post(f"/jobs/{job_id}/cuts", data={"cuts": "00:00:10 - 00:00:20\ngibberish"})
        assert bad.status_code == 400 and "Line 2" in bad.text

        # --- finalise: contiguous numbering, source gone -----------------------
        response = client.post(f"/jobs/{job_id}/finalise", follow_redirects=False)
        assert response.status_code == 303
        job = jobs.get_job(job_id)
        assert job["status"] == jobs.FINALISED
        assert job["source_deleted_at"] is not None
        assert not jobs.storage().exists(job["source_path"])
        clips = jobs.get_clips(job_id)
        assert [c["sequence"] for c in clips] == [1, 2]
        assert [c["output_filename"] for c in clips] == [
            "podcast-ep12_clip_01_founder-origin-story.mp4",
            "podcast-ep12_clip_02_added-by-hand.mp4",
        ]
        assert all(jobs.storage().exists(c["output_path"]) for c in clips)
        assert client.post(f"/jobs/{job_id}/apply", json={"rows": []}).status_code == 409, "locked"

        # --- downloads ---------------------------------------------------------
        manifest = client.get(f"/jobs/{job_id}/manifest.json").json()
        assert manifest["clip_count"] == 2 and manifest["finalised_at"]
        response = client.get(f"/jobs/{job_id}/download.zip")
        assert response.status_code == 200
        archive = tmp_path / "clips.zip"
        archive.write_bytes(response.content)
        with zipfile.ZipFile(archive) as bundle:
            assert set(bundle.namelist()) == {
                "podcast-ep12_clip_01_founder-origin-story.mp4",
                "podcast-ep12_clip_02_added-by-hand.mp4",
                "manifest.json",
            }
            bundle.extract("podcast-ep12_clip_02_added-by-hand.mp4", tmp_path)

        # --- a clip added and tweaked through the review page is still exact ---
        produced = tmp_path / "podcast-ep12_clip_02_added-by-hand.mp4"
        first = extract_frame(produced, tmp_path / "first.png")
        expected = extract_frame(marked_source, tmp_path / "expected.png", at=17.400)
        assert psnr_between(first, expected) > 30.0

    def test_no_suggestions_lands_on_an_editable_empty_review_page(self, client, marked_source, fake_providers):
        _, suggester = fake_providers
        suggester.fail = "Ollama is down"
        sign_in(client, ADMIN)
        job_id = upload(client, marked_source, "quiet.mp4")
        job = run_worker_once()
        assert job["status"] == jobs.AWAITING_CUTS
        assert "Ollama is down" in job["suggestion_error"]
        assert jobs.get_clips(job_id) == []
        page = client.get(f"/jobs/{job_id}")
        assert "No AI suggestions" in page.text and "Apply changes" in page.text

    def test_disabled_transcription_skips_audio_extraction_entirely(self, client, marked_source, monkeypatch):
        from app import transcription

        transcription.set_provider(None)  # run-tests.sh: disabled
        calls = []
        monkeypatch.setattr(jobs, "extract_audio", lambda *a, **k: calls.append(1))
        sign_in(client, ADMIN)
        job_id = upload(client, marked_source, "nospeech.mp4")
        job = run_worker_once()
        assert job["status"] == jobs.AWAITING_CUTS
        assert calls == [], "no audio decode when transcription is off"
        assert "switched off" in job["transcript_error"]

    def test_suggest_again_is_a_worker_stage_not_a_request(self, client, marked_source, fake_providers):
        _, suggester = fake_providers
        sign_in(client, ADMIN)
        job_id = upload(client, marked_source, "again.mp4")
        drain_worker()
        assert jobs.get_job(job_id)["status"] == jobs.COMPLETE
        before = suggester.calls

        response = client.post(f"/jobs/{job_id}/suggest", follow_redirects=False)
        assert response.status_code == 303
        assert jobs.get_job(job_id)["status"] == jobs.SUGGEST_REQUESTED
        assert suggester.calls == before, "the request must not call the model"

        job = run_worker_once()
        assert suggester.calls == before + 1
        assert job["status"] == jobs.COMPLETE, "returns to where it was"
        assert len(jobs.get_clips(job_id)) == 2, "existing clips untouched"

    def test_stale_transcribing_job_is_recovered(self, client, marked_source, fake_providers):
        sign_in(client, ADMIN)
        job_id = upload(client, marked_source, "stuck.mp4")
        # Simulate a worker that died five hours into transcription.
        jobs.db.execute(
            "UPDATE jobs SET status = %s, claimed_by = 'dead', claimed_at = now() - interval '5 hours' WHERE id = %s",
            (jobs.TRANSCRIBING, job_id),
        )
        assert jobs.claim_next_job("w") is None or jobs.get_job(job_id)["status"] != jobs.PROBING
        assert jobs.release_stale_claims() >= 1
        assert jobs.get_job(job_id)["status"] == jobs.UPLOADED
        drain_worker()
        assert jobs.get_job(job_id)["status"] == jobs.COMPLETE

    def test_unreadable_upload_gets_an_expiry_so_it_is_purged(self, client, tmp_path):
        sign_in(client, ADMIN)
        junk = tmp_path / "junk.mp4"
        junk.write_bytes(b"this is not a video" * 1000)
        job_id = upload(client, junk, "junk.mp4")
        job = run_worker_once()
        assert job["status"] == jobs.FAILED
        assert job["expires_at"] is not None
        assert job["claimed_by"] is None

    def test_another_user_cannot_read_someone_elses_job(self, client, marked_source):
        sign_in(client, ADMIN)
        job_id = client.post("/api/uploads", data={"filename": "private.mp4", "total_bytes": 1024}).json()["job_id"]
        client.post("/admin/invite", data={"email": MEMBER}, follow_redirects=False)
        sign_in(client, MEMBER)
        for path in (f"/jobs/{job_id}", f"/api/uploads/{job_id}", f"/jobs/{job_id}/manifest.json", f"/jobs/{job_id}/source"):
            assert client.get(path).status_code == 404, path
        assert client.post(f"/jobs/{job_id}/apply", json={"rows": []}).status_code == 404

    def test_oversized_and_empty_uploads_are_refused_up_front(self, client):
        sign_in(client, ADMIN)
        assert client.post("/api/uploads", data={"filename": "huge.mp4", "total_bytes": settings.max_upload_bytes + 1}).status_code == 413
        assert client.post("/api/uploads", data={"filename": "empty.mp4", "total_bytes": 0}).status_code == 400

    def test_health_endpoint_reports_ok(self, client):
        assert client.get("/healthz").json()["status"] == "ok"
