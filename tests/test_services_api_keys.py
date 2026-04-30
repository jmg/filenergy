from filenergy.models import ApiKey
from filenergy.services import api_keys


def test_mint_returns_plaintext_only_once(db, user, workspace, app):
    with app.test_request_context():
        record, plaintext = api_keys.mint(workspace, user, "my key")
    assert plaintext.startswith("fk_")
    assert record.token_hash != plaintext
    assert record.prefix == plaintext[:12]
    assert record.is_active


def test_verify_active_token(db, user, workspace, app):
    with app.test_request_context():
        record, plaintext = api_keys.mint(workspace, user, "x")
        looked_up = api_keys.verify(plaintext)
    assert looked_up.id == record.id
    # last_used_at gets bumped.
    assert looked_up.last_used_at is not None


def test_verify_unknown_token_returns_none(app):
    with app.test_request_context():
        assert api_keys.verify("fk_not_real") is None
        assert api_keys.verify("") is None
        assert api_keys.verify(None) is None
        assert api_keys.verify("no-prefix") is None


def test_verify_revoked_token_returns_none(db, user, workspace, app):
    with app.test_request_context():
        record, plaintext = api_keys.mint(workspace, user, "x")
        api_keys.revoke(workspace, record.id)
    assert api_keys.verify(plaintext) is None


def test_revoke_idempotent(db, user, workspace, app):
    with app.test_request_context():
        record, _ = api_keys.mint(workspace, user, "x")
        assert api_keys.revoke(workspace, record.id) is True
        assert api_keys.revoke(workspace, record.id) is False


def test_revoke_unknown_id_returns_false(db, workspace, app):
    with app.test_request_context():
        assert api_keys.revoke(workspace, 9999) is False


def test_revoke_other_workspace_fails(db, user, workspace, app):
    from filenergy.models import User
    from filenergy.services import workspaces as ws_service

    other = User(email="o@o", username="o")
    other.set_password("p")
    db.session.add(other)
    db.session.commit()
    other_ws = ws_service.ensure_default_for(other)
    with app.test_request_context():
        record, _ = api_keys.mint(workspace, user, "x")
        # Other workspace can't revoke our key.
        assert api_keys.revoke(other_ws, record.id) is False


def test_list_for_workspace(db, user, workspace, app):
    with app.test_request_context():
        api_keys.mint(workspace, user, "a")
        api_keys.mint(workspace, user, "b")
    keys = api_keys.list_for_workspace(workspace)
    assert {k.name for k in keys} == {"a", "b"}
