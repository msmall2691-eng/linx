"""Cleaner profiles, document uploads, and the admin vetting queue.

The thing under test throughout is the gate: `can_take_jobs` is computed by
Postgres from two statuses that only an admin (or a vendor) can move, and every
surface that mentions it reads the same answer.
"""

from __future__ import annotations

import io
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 256


def _upload(client: TestClient, auth: dict, doc_type: str, *, content=PNG, mime="image/png"):
    return client.post(
        "/api/cleaner/documents",
        params={"document_type": doc_type},
        files={"upload": ("id-front.png", io.BytesIO(content), mime)},
        headers=auth,
    )


class TestProfile:
    def test_a_cleaner_creates_their_own_profile(self, client: TestClient, make_user) -> None:
        cleaner = make_user(role="cleaner")
        resp = client.put(
            "/api/cleaner/profile",
            json={
                "bio": "Ten years of turnovers.",
                "service_lat": "43.6591",
                "service_lng": "-70.2568",
                "service_radius_miles": 30,
            },
            headers=cleaner["auth"],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["service_radius_miles"] == 30
        assert body["can_take_jobs"] is False
        assert body["vetting"]["can_take_jobs"] is False

    def test_putting_twice_updates_rather_than_duplicating(
        self, client: TestClient, make_cleaner
    ) -> None:
        cleaner = make_cleaner()
        first = client.get("/api/cleaner/profile", headers=cleaner["auth"]).json()

        resp = client.put(
            "/api/cleaner/profile",
            json={"service_radius_miles": 5, "service_lat": "43.6", "service_lng": "-70.2"},
            headers=cleaner["auth"],
        )
        assert resp.status_code == 200
        assert resp.json()["id"] == first["id"]
        assert resp.json()["service_radius_miles"] == 5

    def test_a_cleaner_cannot_vouch_for_their_own_vetting(
        self, client: TestClient, make_cleaner
    ) -> None:
        """The statuses and the gate are not client-settable, at any cost."""
        cleaner = make_cleaner()
        resp = client.put(
            "/api/cleaner/profile",
            json={
                "service_radius_miles": 25,
                "id_verification_status": "approved",
                "background_check_status": "approved",
                "can_take_jobs": True,
                "has_insurance_on_file": True,
            },
            headers=cleaner["auth"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id_verification_status"] == "not_started"
        assert body["background_check_status"] == "not_started"
        assert body["can_take_jobs"] is False
        assert body["has_insurance_on_file"] is False

    def test_an_owner_cannot_reach_the_cleaner_surface(
        self, client: TestClient, make_user
    ) -> None:
        owner = make_user(role="owner")
        assert client.get("/api/cleaner/profile", headers=owner["auth"]).status_code == 403

    def test_the_profile_explains_why_bidding_is_blocked(
        self, client: TestClient, make_cleaner
    ) -> None:
        cleaner = make_cleaner()
        vetting = client.get("/api/cleaner/profile", headers=cleaner["auth"]).json()["vetting"]

        assert vetting["can_take_jobs"] is False
        assert "ID verification not started" in vetting["blockers"]
        assert "background check not started" in vetting["blockers"]
        # Insurance is a flag, so it is a warning and never a blocker.
        assert any("insurance" in warning for warning in vetting["warnings"])
        assert not any("insurance" in blocker for blocker in vetting["blockers"])

    def test_a_missing_service_area_is_flagged_not_blocked(
        self, client: TestClient, make_cleaner
    ) -> None:
        """Nothing will appear on the board, which should say so rather than look broken."""
        cleaner = make_cleaner(lat=None, lng=None)
        vetting = client.get("/api/cleaner/profile", headers=cleaner["auth"]).json()["vetting"]
        assert any("service area" in warning for warning in vetting["warnings"])


class TestDocumentUpload:
    def test_uploading_an_id_puts_the_cleaner_in_the_queue(
        self, client: TestClient, make_cleaner
    ) -> None:
        cleaner = make_cleaner()
        resp = _upload(client, cleaner["auth"], "id")
        assert resp.status_code == 201, resp.text

        body = resp.json()
        assert len(body["documents"]) == 1
        assert body["documents"][0]["status"] == "pending"
        # Uploading moves them into review — and never approves anything.
        assert body["id_verification_status"] == "pending"
        assert body["can_take_jobs"] is False

    def test_insurance_does_not_touch_id_verification(
        self, client: TestClient, make_cleaner
    ) -> None:
        """A COI is a flag at v1; it must not drag identity review along with it."""
        cleaner = make_cleaner()
        resp = _upload(client, cleaner["auth"], "insurance")
        assert resp.status_code == 201
        assert resp.json()["id_verification_status"] == "not_started"

    def test_the_storage_key_is_never_returned(
        self, client: TestClient, make_cleaner
    ) -> None:
        cleaner = make_cleaner()
        body = _upload(client, cleaner["auth"], "id").json()
        assert "storage_key" not in body["documents"][0]

    def test_an_unsupported_file_type_is_refused(
        self, client: TestClient, make_cleaner
    ) -> None:
        cleaner = make_cleaner()
        resp = _upload(
            client, cleaner["auth"], "id", content=b"<script>alert(1)</script>", mime="text/html"
        )
        assert resp.status_code == 415

    def test_an_empty_file_is_refused(self, client: TestClient, make_cleaner) -> None:
        cleaner = make_cleaner()
        assert _upload(client, cleaner["auth"], "id", content=b"").status_code == 415

    def test_an_oversized_file_is_refused(
        self, client: TestClient, make_cleaner, monkeypatch
    ) -> None:
        """Streamed and aborted on overrun, rather than trusting Content-Length."""
        from app.config import settings

        monkeypatch.setattr(settings, "max_document_bytes", 1024)
        cleaner = make_cleaner()
        resp = _upload(client, cleaner["auth"], "id", content=b"\x89PNG\r\n\x1a\n" + b"x" * 4096)
        assert resp.status_code == 413

    def test_a_cleaner_can_read_back_their_own_document(
        self, client: TestClient, make_cleaner
    ) -> None:
        cleaner = make_cleaner()
        document_id = _upload(client, cleaner["auth"], "id").json()["documents"][0]["id"]

        resp = client.get(
            f"/api/cleaner/documents/{document_id}/file", headers=cleaner["auth"]
        )
        assert resp.status_code == 200
        assert resp.content == PNG

    def test_one_cleaner_cannot_read_anothers_id(
        self, client: TestClient, make_cleaner
    ) -> None:
        """These are government IDs. Changing the id in the URL must get nothing."""
        first = make_cleaner()
        second = make_cleaner()
        document_id = _upload(client, first["auth"], "id").json()["documents"][0]["id"]

        resp = client.get(
            f"/api/cleaner/documents/{document_id}/file", headers=second["auth"]
        )
        assert resp.status_code == 404

    def test_a_pending_document_can_be_withdrawn(
        self, client: TestClient, make_cleaner
    ) -> None:
        cleaner = make_cleaner()
        document_id = _upload(client, cleaner["auth"], "id").json()["documents"][0]["id"]

        resp = client.delete(
            f"/api/cleaner/documents/{document_id}", headers=cleaner["auth"]
        )
        assert resp.status_code == 200
        assert resp.json()["documents"] == []

    def test_a_reviewed_document_cannot_be_withdrawn(
        self, client: TestClient, make_cleaner, admin_user
    ) -> None:
        """The evidence behind a decision is not removable by its subject."""
        cleaner = make_cleaner()
        document_id = _upload(client, cleaner["auth"], "id").json()["documents"][0]["id"]

        client.post(
            f"/api/admin/documents/{document_id}/review",
            json={"status": "approved", "notes": "Matches the reference."},
            headers=admin_user["auth"],
        )

        resp = client.delete(
            f"/api/cleaner/documents/{document_id}", headers=cleaner["auth"]
        )
        assert resp.status_code == 409


class TestStorageKeysCannotEscape:
    def test_a_crafted_key_is_refused(self, tmp_path) -> None:
        """Keys are generated, but this is where one would become a path."""
        from app.services.storage import LocalDiskStorage

        storage = LocalDiskStorage(tmp_path / "documents")
        with pytest.raises(ValueError, match="escapes the storage root"):
            storage.open("../../../../etc/passwd")

    def test_generated_keys_are_unguessable_and_scoped(self, tmp_path) -> None:
        from app.services.storage import LocalDiskStorage

        storage = LocalDiskStorage(tmp_path / "documents")
        cleaner_id = uuid.uuid4()
        key = storage.save(
            stream=io.BytesIO(PNG), content_type="image/png", cleaner_id=cleaner_id
        )
        # Scoped to the cleaner, and carrying no client-supplied filename.
        assert key.startswith(f"{cleaner_id}/")
        assert "id-front" not in key
        assert len(key.split("/")[1]) > 20


class TestAdminVettingQueue:
    def test_the_queue_lists_cleaners_awaiting_review(
        self, client: TestClient, make_cleaner, admin_user
    ) -> None:
        cleaner = make_cleaner()
        _upload(client, cleaner["auth"], "id")

        queue = client.get("/api/admin/vetting-queue", headers=admin_user["auth"]).json()
        assert [entry["profile_id"] for entry in queue] == [cleaner["profile_id"]]
        assert queue[0]["email"] == cleaner["user"]["email"]
        assert len(queue[0]["documents"]) == 1

    def test_cleared_cleaners_drop_out_of_the_queue(
        self, client: TestClient, make_cleaner, admin_user
    ) -> None:
        make_cleaner(cleared=True)
        waiting = make_cleaner()

        queue = client.get("/api/admin/vetting-queue", headers=admin_user["auth"]).json()
        assert [entry["profile_id"] for entry in queue] == [waiting["profile_id"]]

        everyone = client.get(
            "/api/admin/vetting-queue",
            params={"include_cleared": True},
            headers=admin_user["auth"],
        ).json()
        assert len(everyone) == 2

    def test_both_approvals_clear_the_cleaner(
        self, client: TestClient, make_cleaner, admin_user
    ) -> None:
        """The database adds the two statuses up; nothing else may."""
        cleaner = make_cleaner()
        profile_id = cleaner["profile_id"]

        after_id = client.post(
            f"/api/admin/cleaners/{profile_id}/id-verification",
            json={"status": "approved"},
            headers=admin_user["auth"],
        ).json()
        assert after_id["can_take_jobs"] is False  # background check still outstanding

        after_both = client.post(
            f"/api/admin/cleaners/{profile_id}/background-check",
            json={"status": "approved"},
            headers=admin_user["auth"],
        ).json()
        assert after_both["can_take_jobs"] is True

    def test_revoking_one_approval_revokes_clearance(
        self, client: TestClient, make_cleaner, admin_user
    ) -> None:
        cleaner = make_cleaner(cleared=True)
        resp = client.post(
            f"/api/admin/cleaners/{cleaner['profile_id']}/background-check",
            json={"status": "rejected", "notes": "Report came back with a conviction."},
            headers=admin_user["auth"],
        )
        assert resp.json()["can_take_jobs"] is False

    def test_approving_insurance_flips_the_flag_without_clearing(
        self, client: TestClient, make_cleaner, admin_user
    ) -> None:
        cleaner = make_cleaner()
        document_id = _upload(client, cleaner["auth"], "insurance").json()["documents"][0]["id"]

        body = client.post(
            f"/api/admin/documents/{document_id}/review",
            json={"status": "approved"},
            headers=admin_user["auth"],
        ).json()
        assert body["has_insurance_on_file"] is True
        assert body["can_take_jobs"] is False

    def test_approving_an_id_document_alone_does_not_clear_anyone(
        self, client: TestClient, make_cleaner, admin_user
    ) -> None:
        """A photo ID and a reference are weighed together by a human.

        Ticking off one file must not be the same as deciding the identity.
        """
        cleaner = make_cleaner()
        document_id = _upload(client, cleaner["auth"], "id").json()["documents"][0]["id"]

        body = client.post(
            f"/api/admin/documents/{document_id}/review",
            json={"status": "approved"},
            headers=admin_user["auth"],
        ).json()
        assert body["id_verification_status"] == "pending"
        assert body["can_take_jobs"] is False

    def test_an_admin_can_view_an_uploaded_document(
        self, client: TestClient, make_cleaner, admin_user
    ) -> None:
        cleaner = make_cleaner()
        document_id = _upload(client, cleaner["auth"], "id").json()["documents"][0]["id"]

        resp = client.get(
            f"/api/admin/documents/{document_id}/file", headers=admin_user["auth"]
        )
        assert resp.status_code == 200
        assert resp.content == PNG

    @pytest.mark.parametrize("role", ["owner", "cleaner"])
    def test_non_admins_cannot_reach_the_queue(
        self, client: TestClient, make_user, role: str
    ) -> None:
        user = make_user(role=role)
        assert (
            client.get("/api/admin/vetting-queue", headers=user["auth"]).status_code == 403
        )

    def test_a_cleaner_cannot_review_themselves(
        self, client: TestClient, make_cleaner
    ) -> None:
        cleaner = make_cleaner()
        resp = client.post(
            f"/api/admin/cleaners/{cleaner['profile_id']}/id-verification",
            json={"status": "approved"},
            headers=cleaner["auth"],
        )
        assert resp.status_code == 403


class TestCanTakeJobsCannotBeWritten:
    def test_not_even_by_the_application(self, client: TestClient, make_cleaner, db) -> None:
        """The last line of defence: Postgres refuses the write outright.

        No admin endpoint exists to override the gate, and if one were ever
        added by mistake, this is what stops it.
        """
        from app.models import CleanerProfile

        cleaner = make_cleaner()
        with pytest.raises(DBAPIError):
            db.execute(
                CleanerProfile.__table__.update()
                .where(CleanerProfile.id == uuid.UUID(cleaner["profile_id"]))
                .values(can_take_jobs=True)
            )
        db.rollback()
