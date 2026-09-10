"""End-to-end test through the real HTTP API against a real Postgres.

Covers the whole path an operator takes: sign in, upload a multi-chunk file,
have it probed, paste cut points, confirm, render, and download. The final
assertion re-checks frame accuracy on a clip that came out of the full
pipeline, not just out of a direct call to the cutter.

Skipped unless TEST_DATABASE_URL points at a usable Postgres.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set"
)

from app import auth, db, jobs  # noqa: E402
from app.config import settings  # noqa: E402
from tests.conftest import extract_frame, psnr_between, requires_ffmpeg  # noqa: E402

ADMIN = "admin@example.com"
MEMBER = "member@example.com"
OUTSIDER = "stranger@example.com"


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app.web import app

    db.wait_for_database()
    with db.connection() as conn:
        conn.execute("DROP TABLE IF EXISTS clips, jobs, login_tokens, invites, users CASCADE")
    db.apply_schema()

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


class TestAccessControl:
    def test_uninvited_address_cannot_get_a_link(self, client):
        with pytest.raises(auth.NotInvited):
            auth.request_login_link(OUTSIDER)

    def test_login_page_does_not_reveal_whether_an_address_is_invited(self, client):
        invited = client.post("/login", data={"email": ADMIN}, follow_redirects=False)
        uninvited = client.post("/login", data={"email": OUTSIDER}, follow_redirects=False)
        assert invited.status_code == uninvited.status_code == 303
        assert invited.headers["location"] == uninvited.headers["location"], (
            "responses must be identical or the invite list can be enumerated"
        )

    def test_anonymous_browser_is_sent_to_login(self, client):
        client.cookies.clear()
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_bootstrap_admin_can_sign_in_and_is_admin(self, client):
        sign_in(client, ADMIN)
        assert client.get("/admin").status_code == 200

    def test_magic_link_works_only_once(self, client):
        url = auth.request_login_link(ADMIN)
        token = url.split("token=")[1]
        assert client.get(f"/auth/verify?token={token}", follow_redirects=False).status_code == 303
        again = client.get(f"/auth/verify?token={token}", follow_redirects=False)
        assert "error" in again.headers["location"], "a used link must not work twice"

    def test_garbage_token_is_rejected(self, client):
        response = client.get("/auth/verify?token=not-a-real-token", follow_redirects=False)
        assert "error" in response.headers["location"]

    def test_admin_can_invite_and_revoke(self, client):
        sign_in(client, ADMIN)
        client.post("/admin/invite", data={"email": MEMBER}, follow_redirects=False)
        assert auth.is_invited(MEMBER)

        sign_in(client, MEMBER)
        assert client.get("/").status_code == 200
        assert client.get("/admin").status_code == 403, "members must not reach admin pages"

        sign_in(client, ADMIN)
        client.post("/admin/revoke", data={"email": MEMBER}, follow_redirects=False)
        assert not auth.is_invited(MEMBER)

    def test_revoking_kills_an_existing_session_immediately(self, client):
        sign_in(client, ADMIN)
        client.post("/admin/invite", data={"email": MEMBER}, follow_redirects=False)
        sign_in(client, MEMBER)
        member_cookies = dict(client.cookies)
        assert client.get("/").status_code == 200

        sign_in(client, ADMIN)
        client.post("/admin/revoke", data={"email": MEMBER}, follow_redirects=False)

        client.cookies.clear()
        for name, value in member_cookies.items():
            client.cookies.set(name, value)
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303, (
            "a revoked user must lose access at once, not when their cookie expires"
        )

    def test_admin_cannot_revoke_themselves(self, client):
        sign_in(client, ADMIN)
        assert client.post("/admin/revoke", data={"email": ADMIN}).status_code == 400


@requires_ffmpeg
class TestFullPipeline:
    def test_upload_cut_render_download(self, client, marked_source, tmp_path):
        sign_in(client, ADMIN)
        payload = marked_source.read_bytes()

        # --- open the upload -------------------------------------------------
        response = client.post(
            "/api/uploads",
            data={"filename": "Podcast Ep12.mp4", "total_bytes": len(payload)},
        )
        assert response.status_code == 200, response.text
        job_id = response.json()["job_id"]

        # --- send it in several chunks, as the browser does ------------------
        chunk = max(len(payload) // 4, 1)
        offset = 0
        while offset < len(payload):
            piece = payload[offset : offset + chunk]
            response = client.put(
                f"/api/uploads/{job_id}",
                content=piece,
                headers={"X-Chunk-Offset": str(offset)},
            )
            assert response.status_code == 200, response.text
            offset = response.json()["received_bytes"]
        assert offset == len(payload)

        # a chunk replayed at the wrong offset must be refused, not appended
        assert client.put(
            f"/api/uploads/{job_id}", content=b"junk", headers={"X-Chunk-Offset": "0"}
        ).status_code == 409

        assert client.post(f"/api/uploads/{job_id}/complete").status_code == 200

        # --- worker probes it ------------------------------------------------
        job = jobs.claim_next_job("test-worker")
        assert job is not None and str(job["id"]) == job_id
        jobs.probe_job(job)

        job = jobs.get_job(job_id)
        assert job["status"] == jobs.AWAITING_CUTS
        assert job["duration_seconds"] == pytest.approx(30.0, abs=0.2)
        assert (job["width"], job["height"]) == (640, 360)

        # --- a bad cut list is rejected before anything renders --------------
        response = client.post(
            f"/jobs/{job_id}/cuts", data={"cuts": "00:00:10 - 00:00:20\ngibberish"}
        )
        assert response.status_code == 400
        assert "Line 2" in response.text
        assert jobs.get_job(job_id)["status"] == jobs.AWAITING_CUTS

        # --- a range past the end of the video is rejected -------------------
        # The source is 30s. 00:45:00 parses perfectly well, so this exercises
        # the duration check rather than the format check.
        response = client.post(f"/jobs/{job_id}/cuts", data={"cuts": "00:00:10 - 00:45:00"})
        assert response.status_code == 400
        assert "past the end" in response.text

        # --- an out-of-range clock value is caught as a format error ---------
        response = client.post(f"/jobs/{job_id}/cuts", data={"cuts": "00:00:10 - 00:99:00"})
        assert response.status_code == 400
        assert "minutes out of range" in response.text

        # --- a good list previews without rendering --------------------------
        cut_list = (
            "# two clips\n"
            "00:00:17.400 - 00:00:22.400 | Founder origin story\n"
            "00:00:05 to 00:00:09\n"
        )
        response = client.post(f"/jobs/{job_id}/cuts", data={"cuts": cut_list})
        assert response.status_code == 200
        assert "Confirm these 2 clips" in response.text
        # The preview must promise the filenames the renderer actually writes.
        assert "podcast-ep12_clip_01_founder-origin-story.mp4" in response.text
        assert "podcast-ep12_clip_02.mp4" in response.text
        assert jobs.get_job(job_id)["status"] == jobs.AWAITING_CUTS, (
            "previewing must never queue the job"
        )

        # --- confirming queues it --------------------------------------------
        response = client.post(
            f"/jobs/{job_id}/cuts", data={"cuts": cut_list, "confirm": "yes"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert jobs.get_job(job_id)["status"] == jobs.QUEUED

        clips = jobs.get_clips(job_id)
        assert [c["sequence"] for c in clips] == [1, 2]
        assert clips[0]["output_filename"] == "podcast-ep12_clip_01_founder-origin-story.mp4"
        assert clips[1]["output_filename"] == "podcast-ep12_clip_02.mp4"

        # --- worker renders ---------------------------------------------------
        job = jobs.claim_next_job("test-worker")
        assert job is not None
        jobs.render_job(job)

        job = jobs.get_job(job_id)
        assert job["status"] == jobs.COMPLETE, job["error"]
        assert job["expires_at"] is not None

        clips = jobs.get_clips(job_id)
        assert all(c["status"] == "complete" for c in clips)
        assert clips[0]["rendered_duration"] == pytest.approx(5.0, abs=0.04)
        assert clips[1]["rendered_duration"] == pytest.approx(4.0, abs=0.04)

        # --- retention: the source is gone, the clips are not -----------------
        assert job["source_deleted_at"] is not None
        assert not jobs.storage().exists(job["source_path"])
        assert jobs.storage().exists(clips[0]["output_path"])

        # --- downloads ---------------------------------------------------------
        response = client.get(f"/jobs/{job_id}/clips/{clips[0]['id']}")
        assert response.status_code == 200
        assert response.headers["content-type"] == "video/mp4"
        assert len(response.content) > 1000

        manifest = client.get(f"/jobs/{job_id}/manifest.json").json()
        assert manifest["clip_count"] == 2
        assert manifest["clips"][0]["start"] == "00:00:17.400"
        assert manifest["clips"][0]["label"] == "Founder origin story"
        assert manifest["clips"][0]["requested_duration_seconds"] == 5.0

        response = client.get(f"/jobs/{job_id}/download.zip")
        assert response.status_code == 200
        archive = tmp_path / "clips.zip"
        archive.write_bytes(response.content)

        import zipfile

        with zipfile.ZipFile(archive) as bundle:
            names = set(bundle.namelist())
            assert names == {
                "podcast-ep12_clip_01_founder-origin-story.mp4",
                "podcast-ep12_clip_02.mp4",
                "manifest.json",
            }
            bundle.extract("podcast-ep12_clip_01_founder-origin-story.mp4", tmp_path)

        # --- the whole pipeline still produced a frame-accurate cut -----------
        produced = tmp_path / "podcast-ep12_clip_01_founder-origin-story.mp4"
        first = extract_frame(produced, tmp_path / "pipeline_first.png")
        expected = extract_frame(marked_source, tmp_path / "pipeline_expected.png", at=17.400)
        assert psnr_between(first, expected) > 30.0, (
            "a clip that went through the full pipeline must still start on the "
            "exact requested frame"
        )

    def test_another_user_cannot_read_someone_elses_job(self, client, marked_source):
        sign_in(client, ADMIN)
        response = client.post(
            "/api/uploads", data={"filename": "private.mp4", "total_bytes": 1024}
        )
        job_id = response.json()["job_id"]

        client.post("/admin/invite", data={"email": MEMBER}, follow_redirects=False)
        sign_in(client, MEMBER)
        assert client.get(f"/jobs/{job_id}").status_code == 404
        assert client.get(f"/api/uploads/{job_id}").status_code == 404
        assert client.get(f"/jobs/{job_id}/manifest.json").status_code == 404

    def test_oversized_upload_is_refused_up_front(self, client):
        sign_in(client, ADMIN)
        response = client.post(
            "/api/uploads",
            data={"filename": "huge.mp4", "total_bytes": settings.max_upload_bytes + 1},
        )
        assert response.status_code == 413

    def test_empty_upload_is_refused(self, client):
        sign_in(client, ADMIN)
        assert client.post(
            "/api/uploads", data={"filename": "empty.mp4", "total_bytes": 0}
        ).status_code == 400

    def test_health_endpoint_reports_ok(self, client):
        assert client.get("/healthz").json()["status"] == "ok"
