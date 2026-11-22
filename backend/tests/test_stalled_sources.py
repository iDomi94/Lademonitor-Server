"""Status-Mail bei anhaltenden Abruffehlern (notifications._run_stalled_sources, v0.56.3)."""

from datetime import datetime, timedelta

import pytest

from app import mailer, models, notifications

from conftest import create_vehicle, register


@pytest.fixture
def sent(monkeypatch):
    mails = []
    monkeypatch.setattr(mailer, "send", lambda db, **kw: mails.append(kw) or True)
    return mails


def _config(client, db_session, model=models.VwDataActConfig, status="error", **fields):
    register(client, email="a@example.org")
    vehicle = create_vehicle(client)
    user_id = db_session.query(models.User).one().id
    config = model(user_id=user_id, vehicle_id=vehicle["id"], enabled=True,
                   last_status=status, last_error="kein JSON", **fields)
    db_session.add(config)
    db_session.commit()
    return config


def _age(db_session, config, hours):
    config.stalled_since = datetime.utcnow() - timedelta(hours=hours)
    db_session.commit()


def test_first_sighting_only_starts_the_clock(client, db_session, sent):
    config = _config(client, db_session)
    notifications._run_stalled_sources(db_session)
    assert config.stalled_since is not None
    assert sent == []


def test_mail_after_a_day_and_only_once(client, db_session, sent):
    config = _config(client, db_session)
    notifications._run_stalled_sources(db_session)
    _age(db_session, config, 23)
    notifications._run_stalled_sources(db_session)
    assert sent == []

    _age(db_session, config, 25)
    notifications._run_stalled_sources(db_session)
    notifications._run_stalled_sources(db_session)
    assert len(sent) == 1
    assert sent[0]["kind"] == "source_stalled"
    assert "VW" in sent[0]["mail"].subject


def test_recovery_resets_and_next_outage_mails_again(client, db_session, sent):
    config = _config(client, db_session)
    notifications._run_stalled_sources(db_session)
    _age(db_session, config, 30)
    notifications._run_stalled_sources(db_session)
    assert len(sent) == 1

    config.last_status = "ok"
    db_session.commit()
    notifications._run_stalled_sources(db_session)
    assert config.stalled_since is None and config.stalled_notified_at is None

    config.last_status = "error"
    db_session.commit()
    notifications._run_stalled_sources(db_session)
    _age(db_session, config, 30)
    notifications._run_stalled_sources(db_session)
    assert len(sent) == 2


@pytest.mark.parametrize("status", ["auth_error", "no_request", "ok", None])
def test_statuses_with_their_own_mail_or_working_are_ignored(client, db_session, sent, status):
    config = _config(client, db_session, status=status)
    notifications._run_stalled_sources(db_session)
    assert config.stalled_since is None
    assert sent == []


def test_disabled_source_and_switched_off_user_get_no_mail(client, db_session, sent):
    config = _config(client, db_session, model=models.RenaultConfig, status="rate_limited")
    db_session.get(models.User, config.user_id).notify_myskoda_error = False
    db_session.commit()
    notifications._run_stalled_sources(db_session)
    _age(db_session, config, 30)
    notifications._run_stalled_sources(db_session)
    assert sent == []
    # Merker steht trotzdem - sonst liefe die Pruefung jede Viertelstunde erneut.
    assert config.stalled_notified_at is not None

    config.enabled = False
    config.stalled_since = None
    db_session.commit()
    notifications._run_stalled_sources(db_session)
    assert config.stalled_since is None


@pytest.mark.parametrize("model", [models.MySkodaConfig, models.BmwCarDataConfig])
def test_other_sources_are_covered(client, db_session, sent, model):
    config = _config(client, db_session, model=model)
    notifications._run_stalled_sources(db_session)
    _age(db_session, config, 30)
    notifications._run_stalled_sources(db_session)
    assert len(sent) == 1
