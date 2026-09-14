"""The schema itself — shape, constraints, and the invariants that back the guardrails.

Phase 1 ships no business logic, so these tests exercise the database directly.
They exist because the constraints below are what keeps a later phase's bug from
becoming a corrupt row: a second award on one turnover, a negative price, a
cleaner marked job-ready without being vetted.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.db import engine
from app.models import (
    Award,
    Base,
    Bid,
    CleanerProfile,
    Property,
    Review,
    Turnover,
    TurnoverStatus,
    User,
    UserRole,
    VerificationStatus,
)

EXPECTED_TABLES = {
    "users",
    "cleaner_profiles",
    "properties",
    "turnovers",
    "bids",
    "awards",
    "documents",
    "reviews",
    "payments_in",
    "payouts",
    # Phase 5. The one table phase 1 did not declare, because a notification is
    # a record of something happening rather than part of the domain shape.
    "notifications",
    # A booking feed an owner connects. Phase 1 could not have declared it: the
    # domain shape it belongs to is somebody *else's* system, and this table is
    # only the pointer at it plus what happened last time we read it. Adding it
    # failed this assertion first, which is what the assertion is for — a new
    # table should be a decision somebody makes out loud rather than a file that
    # appears.
    "property_calendars",
}


def _owner(db: Session) -> User:
    user = User(
        email=f"owner-{uuid.uuid4().hex}@example.com",
        hashed_password="x",
        full_name="Owner",
        role=UserRole.OWNER,
    )
    db.add(user)
    db.flush()
    return user


def _cleaner(db: Session) -> User:
    user = User(
        email=f"cleaner-{uuid.uuid4().hex}@example.com",
        hashed_password="x",
        full_name="Cleaner",
        role=UserRole.CLEANER,
    )
    db.add(user)
    db.flush()
    return user


def _turnover(db: Session, owner: User) -> Turnover:
    prop = Property(
        owner_id=owner.id,
        nickname="Seaside Cottage",
        address_line1="1 Harbor Way",
        city="Portland",
        state="ME",
        postal_code="04101",
        bedrooms=2,
        bathrooms=1.5,
    )
    db.add(prop)
    db.flush()

    checkout = datetime.now(timezone.utc) + timedelta(days=1)
    turnover = Turnover(
        property_id=prop.id,
        checkout_at=checkout,
        checkin_at=checkout + timedelta(hours=6),
        status=TurnoverStatus.OPEN,
    )
    db.add(turnover)
    db.flush()
    return turnover


class TestTablesExist:
    def test_every_v1_table_was_created_by_the_migration(self) -> None:
        actual = set(inspect(engine).get_table_names())
        assert EXPECTED_TABLES <= actual

    def test_the_models_and_the_migration_agree(self) -> None:
        """The suite runs migrations, not create_all, so this is not circular."""
        assert EXPECTED_TABLES == set(Base.metadata.tables)


class TestRunningOnPostgres:
    """Guardrail 1 needs row locks. SQLite has none, so the suite must not use it."""

    def test_the_test_database_is_postgres(self) -> None:
        assert engine.dialect.name == "postgresql"

    def test_select_for_update_is_available(self, db: Session) -> None:
        """The mechanism phase 4 will build the accept-a-bid path on.

        This is an environment check, not the double-award test — that one fires
        two real concurrent accepts and arrives with the endpoint in phase 4.
        """
        owner = _owner(db)
        turnover = _turnover(db, owner)
        db.commit()

        locked = db.execute(
            select(Turnover).where(Turnover.id == turnover.id).with_for_update()
        ).scalar_one()
        assert locked.id == turnover.id
        db.rollback()


class TestCanTakeJobs:
    """`can_take_jobs` is computed by Postgres, so a gate and a badge cannot disagree."""

    def test_a_new_cleaner_cannot_take_jobs(self, db: Session) -> None:
        user = _cleaner(db)
        profile = CleanerProfile(user_id=user.id)
        db.add(profile)
        db.commit()
        db.refresh(profile)

        assert profile.id_verification_status is VerificationStatus.NOT_STARTED
        assert profile.background_check_status is VerificationStatus.NOT_STARTED
        assert profile.can_take_jobs is False

    def test_id_alone_is_not_enough(self, db: Session) -> None:
        """A photo ID confirms identity, not history. Both checks or neither."""
        user = _cleaner(db)
        profile = CleanerProfile(
            user_id=user.id, id_verification_status=VerificationStatus.APPROVED
        )
        db.add(profile)
        db.commit()
        db.refresh(profile)
        assert profile.can_take_jobs is False

    def test_background_check_alone_is_not_enough(self, db: Session) -> None:
        user = _cleaner(db)
        profile = CleanerProfile(
            user_id=user.id, background_check_status=VerificationStatus.APPROVED
        )
        db.add(profile)
        db.commit()
        db.refresh(profile)
        assert profile.can_take_jobs is False

    def test_both_approvals_clear_the_cleaner(self, db: Session) -> None:
        user = _cleaner(db)
        profile = CleanerProfile(
            user_id=user.id,
            id_verification_status=VerificationStatus.APPROVED,
            background_check_status=VerificationStatus.APPROVED,
        )
        db.add(profile)
        db.commit()
        db.refresh(profile)
        assert profile.can_take_jobs is True

    def test_revoking_a_check_revokes_the_clearance(self, db: Session) -> None:
        user = _cleaner(db)
        profile = CleanerProfile(
            user_id=user.id,
            id_verification_status=VerificationStatus.APPROVED,
            background_check_status=VerificationStatus.APPROVED,
        )
        db.add(profile)
        db.commit()

        profile.background_check_status = VerificationStatus.REJECTED
        db.commit()
        db.refresh(profile)
        assert profile.can_take_jobs is False

    def test_it_cannot_be_set_by_hand(self, db: Session) -> None:
        """The point of the generated column: no code path can lie about it."""
        user = _cleaner(db)
        profile = CleanerProfile(user_id=user.id)
        db.add(profile)
        db.commit()

        with pytest.raises(DBAPIError):
            db.execute(
                CleanerProfile.__table__.update()
                .where(CleanerProfile.id == profile.id)
                .values(can_take_jobs=True)
            )
        db.rollback()

    def test_missing_insurance_is_a_flag_not_a_gate(self, db: Session) -> None:
        """v1 shows a missing COI prominently rather than blocking on it."""
        user = _cleaner(db)
        profile = CleanerProfile(
            user_id=user.id,
            id_verification_status=VerificationStatus.APPROVED,
            background_check_status=VerificationStatus.APPROVED,
            has_insurance_on_file=False,
        )
        db.add(profile)
        db.commit()
        db.refresh(profile)
        assert profile.can_take_jobs is True
        assert profile.has_insurance_on_file is False

    def test_one_profile_per_cleaner(self, db: Session) -> None:
        user = _cleaner(db)
        db.add(CleanerProfile(user_id=user.id))
        db.commit()
        db.add(CleanerProfile(user_id=user.id))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


class TestAwardIsSetOnce:
    """The database backstop behind guardrail 1.

    The row lock is the mechanism; this index is what turns a missed lock into a
    loud error rather than a second award. A turnover promised to two cleaners
    at once is the failure this schema refuses to represent.

    "At once" is the whole of it since phase 4. A booking can come undone — the
    cleaner backs out, or never turns up — and the job goes back on the bench to
    be awarded again, so the index is partial: unique on `turnover_id` *where
    the award is live*. Cancelled awards stay, because who backed out is the
    history a dispute is argued from.
    """

    def test_one_live_award_per_turnover(self, db: Session) -> None:
        owner = _owner(db)
        turnover = _turnover(db, owner)
        first, second = _cleaner(db), _cleaner(db)
        db.commit()

        db.add(
            Award(turnover_id=turnover.id, cleaner_id=first.id, agreed_price_cents=12_500)
        )
        db.commit()

        db.add(
            Award(turnover_id=turnover.id, cleaner_id=second.id, agreed_price_cents=11_000)
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        awards = db.execute(
            select(Award).where(Award.turnover_id == turnover.id)
        ).scalars().all()
        assert len(awards) == 1
        assert awards[0].cleaner_id == first.id

    def test_a_cancelled_award_frees_the_turnover_to_be_awarded_again(
        self, db: Session
    ) -> None:
        """The re-post path, at the schema level.

        Without this the only way to re-staff a job would be to delete the award
        of the cleaner who backed out — erasing the record of exactly the thing
        an owner would later want to point at.
        """
        from datetime import datetime, timezone

        owner = _owner(db)
        turnover = _turnover(db, owner)
        first, second = _cleaner(db), _cleaner(db)
        db.commit()

        original = Award(
            turnover_id=turnover.id, cleaner_id=first.id, agreed_price_cents=12_500
        )
        db.add(original)
        db.commit()

        original.cancelled_at = datetime.now(timezone.utc)
        original.cancellation_reason = "Van broke down."
        db.commit()

        db.add(
            Award(turnover_id=turnover.id, cleaner_id=second.id, agreed_price_cents=13_000)
        )
        db.commit()

        awards = (
            db.execute(select(Award).where(Award.turnover_id == turnover.id))
            .scalars()
            .all()
        )
        assert len(awards) == 2, "the cancelled award should still be on the record"
        live = [award for award in awards if award.cancelled_at is None]
        assert len(live) == 1
        assert live[0].cleaner_id == second.id

    def test_an_award_price_must_be_positive(self, db: Session) -> None:
        owner = _owner(db)
        turnover = _turnover(db, owner)
        cleaner = _cleaner(db)
        db.commit()

        db.add(Award(turnover_id=turnover.id, cleaner_id=cleaner.id, agreed_price_cents=0))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


class TestBids:
    def test_one_bid_per_cleaner_per_turnover(self, db: Session) -> None:
        """An owner never sees the same cleaner twice on one job."""
        owner = _owner(db)
        turnover = _turnover(db, owner)
        cleaner = _cleaner(db)
        db.commit()

        db.add(Bid(turnover_id=turnover.id, cleaner_id=cleaner.id, price_cents=12_000))
        db.commit()

        db.add(Bid(turnover_id=turnover.id, cleaner_id=cleaner.id, price_cents=11_000))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_a_bid_price_must_be_positive(self, db: Session) -> None:
        owner = _owner(db)
        turnover = _turnover(db, owner)
        cleaner = _cleaner(db)
        db.commit()

        db.add(Bid(turnover_id=turnover.id, cleaner_id=cleaner.id, price_cents=-100))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


class TestTurnoverShape:
    def test_checkin_may_be_null_for_a_standing_vacancy(self, db: Session) -> None:
        owner = _owner(db)
        prop = Property(
            owner_id=owner.id,
            nickname="Vacant Unit",
            address_line1="2 Harbor Way",
            city="Portland",
            state="ME",
            postal_code="04101",
        )
        db.add(prop)
        db.flush()

        turnover = Turnover(
            property_id=prop.id,
            checkout_at=datetime.now(timezone.utc),
            checkin_at=None,
        )
        db.add(turnover)
        db.commit()
        db.refresh(turnover)
        assert turnover.checkin_at is None

    def test_checkin_cannot_precede_checkout(self, db: Session) -> None:
        owner = _owner(db)
        prop = Property(
            owner_id=owner.id,
            nickname="Backwards",
            address_line1="3 Harbor Way",
            city="Portland",
            state="ME",
            postal_code="04101",
        )
        db.add(prop)
        db.flush()

        now = datetime.now(timezone.utc)
        db.add(
            Turnover(
                property_id=prop.id,
                checkout_at=now,
                checkin_at=now - timedelta(hours=1),
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_a_new_turnover_starts_as_a_standard_draft(self, db: Session) -> None:
        owner = _owner(db)
        prop = Property(
            owner_id=owner.id,
            nickname="Defaults",
            address_line1="4 Harbor Way",
            city="Portland",
            state="ME",
            postal_code="04101",
        )
        db.add(prop)
        db.flush()

        turnover = Turnover(
            property_id=prop.id, checkout_at=datetime.now(timezone.utc)
        )
        db.add(turnover)
        db.commit()
        db.refresh(turnover)
        # The urgency ladder is derived in phase 2; the safe floor is the default.
        assert turnover.status is TurnoverStatus.DRAFT
        assert turnover.urgency.value == "standard"
        assert turnover.is_same_day is False


class TestReviewsStayHiddenByDefault:
    def test_a_new_review_has_no_visible_at(self, db: Session) -> None:
        """Null `visible_at` means invisible. No second flag can contradict it."""
        owner = _owner(db)
        turnover = _turnover(db, owner)
        db.commit()

        review = Review(
            turnover_id=turnover.id,
            author_id=owner.id,
            author_role=UserRole.OWNER,
            rating=5,
            text="Spotless.",
        )
        db.add(review)
        db.commit()
        db.refresh(review)
        assert review.visible_at is None

    def test_one_review_per_side_per_turnover(self, db: Session) -> None:
        owner = _owner(db)
        turnover = _turnover(db, owner)
        db.commit()

        db.add(
            Review(
                turnover_id=turnover.id,
                author_id=owner.id,
                author_role=UserRole.OWNER,
                rating=5,
            )
        )
        db.commit()
        db.add(
            Review(
                turnover_id=turnover.id,
                author_id=owner.id,
                author_role=UserRole.OWNER,
                rating=1,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    @pytest.mark.parametrize("rating", [0, 6])
    def test_rating_stays_within_one_to_five(self, db: Session, rating: int) -> None:
        owner = _owner(db)
        turnover = _turnover(db, owner)
        db.commit()

        db.add(
            Review(
                turnover_id=turnover.id,
                author_id=owner.id,
                author_role=UserRole.OWNER,
                rating=rating,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
