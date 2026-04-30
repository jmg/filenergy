"""HTTP tests for round-6: URL ingestion, onboarding wizard, dashboard."""
from filenergy.models import File, User, WorkspaceMember


def _stub_urlopen(monkeypatch, body=b"<p>hi</p>", ctype="text/html"):
    class _R:
        def __init__(self):
            self.headers = {"Content-Type": ctype}
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self, n=None):
            return body

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _R())


# ---- URL ingestion endpoint ----


def test_from_url_requires_url(auth_client):
    r = auth_client.post("/file/from_url/", data={"url": ""})
    assert r.status_code == 400


def test_from_url_indexes_page(auth_client, monkeypatch, workspace):
    _stub_urlopen(monkeypatch, b"<title>X</title><p>Apples are red.</p>")
    r = auth_client.post(
        "/file/from_url/", data={"url": "https://example.com/x"}
    )
    assert r.status_code == 200
    payload = r.get_json()
    assert payload["ok"] is True
    f = File.query.filter_by(workspace_id=workspace.id).one()
    assert f.indexed_at is not None
    assert "Apples" in (f.text_content or "")


def test_from_url_returns_400_on_ingestion_error(auth_client, monkeypatch):
    def boom(req, timeout=None):
        raise OSError("dns nope")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    r = auth_client.post(
        "/file/from_url/", data={"url": "https://example.com/x"}
    )
    assert r.status_code == 400


def test_from_url_quota_exceeded(auth_client, monkeypatch):
    from filenergy import settings as cfg
    monkeypatch.setitem(cfg.PLAN_LIMITS["free"], "files_max", 0)
    r = auth_client.post(
        "/file/from_url/", data={"url": "https://example.com/x"}
    )
    assert r.status_code == 402


def test_from_url_requires_login(client):
    r = client.post(
        "/file/from_url/", data={"url": "https://example.com/x"},
        follow_redirects=False,
    )
    assert r.status_code == 302


# ---- Onboarding ----


def test_onboarding_loads_for_authenticated(auth_client, workspace):
    r = auth_client.get("/onboarding/")
    assert r.status_code == 200
    # The page should at least mention "workspace" somewhere.
    assert b"workspace" in r.data.lower() or b"Workspace" in r.data


def test_onboarding_requires_login(client):
    r = client.get("/onboarding/", follow_redirects=False)
    assert r.status_code == 302


def test_onboarding_skip_redirects_home(auth_client):
    r = auth_client.post("/onboarding/skip", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["Location"] in ("/", "/index.index")


def test_onboarding_rename_workspace(auth_client, workspace):
    r = auth_client.post(
        "/onboarding/rename", data={"name": "Acme HQ"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    from filenergy import db
    db.session.refresh(workspace)
    assert workspace.name == "Acme HQ"


def test_onboarding_rename_blank_is_noop(auth_client, workspace):
    original = workspace.name
    r = auth_client.post(
        "/onboarding/rename", data={"name": "  "}, follow_redirects=False,
    )
    assert r.status_code == 302
    from filenergy import db
    db.session.refresh(workspace)
    assert workspace.name == original


def test_onboarding_seed_drops_sample_files(auth_client, workspace):
    r = auth_client.post("/onboarding/seed", follow_redirects=False)
    assert r.status_code == 302
    files = File.query.filter_by(workspace_id=workspace.id).all()
    assert len(files) == 3
    assert {f.name for f in files} == {"welcome.md", "filenergy-pricing.md", "tips.md"}
    # Should be indexed.
    assert all(f.indexed_at is not None for f in files)


def test_register_redirects_new_user_to_onboarding(client):
    r = client.post(
        "/user/register/",
        data={"email": "neww@x.co", "password": "pw", "password_again": "pw"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "/onboarding/" in r.headers["Location"]


# ---- Dashboard ----


def test_dashboard_requires_login(client):
    r = client.get("/dashboard/", follow_redirects=False)
    assert r.status_code == 302


def test_dashboard_requires_admin(client, db, user, workspace):
    other = User(email="m@m", username="m")
    other.set_password("pw")
    db.session.add(other)
    db.session.commit()
    db.session.add(WorkspaceMember(
        workspace_id=workspace.id, user_id=other.id, role="member"
    ))
    db.session.commit()
    client.post("/user/login/", data={"email": "m@m", "password": "pw"})
    client.post(f"/w/switch/{workspace.id}")
    r = client.get("/dashboard/")
    assert r.status_code == 403


def test_dashboard_renders_for_owner(auth_client, uploaded_file):
    auth_client.post("/ask/", json={"question": "?"})
    r = auth_client.get("/dashboard/")
    assert r.status_code == 200
    # Stat numbers and chart bars are rendered.
    assert b"Files" in r.data
    assert b"Conversations" in r.data
    assert b"chart" in r.data


# ---- OCR fallback wiring ----


def test_index_falls_back_to_ocr(auth_client, db, user, workspace, monkeypatch):
    """Upload an image; pypdf can't extract — OCR via stub fills in the text."""
    import io as _io

    _stub_external_services_obj = None
    # The fixture stub doesn't expose itself here, but we can override the
    # OCR's internal call pathway directly.
    from filenergy.services import ocr as ocr_module
    monkeypatch.setattr(ocr_module, "ocr_file", lambda path: "the text from OCR")

    auth_client.post(
        "/file/upload/",
        data={"files[]": (_io.BytesIO(b"\x89PNG\r\n\x1a\nbinary"), "scan.png")},
        content_type="multipart/form-data",
    )
    f = File.query.filter_by(workspace_id=workspace.id).one()
    assert f.text_content == "the text from OCR"
    assert f.indexed_at is not None


def test_index_skips_ocr_when_unconfigured(
    auth_client, db, user, workspace, monkeypatch
):
    import io as _io

    from filenergy.services import ocr as ocr_module
    monkeypatch.setattr(ocr_module, "is_configured", lambda: False)

    auth_client.post(
        "/file/upload/",
        data={"files[]": (_io.BytesIO(b"\x89PNG\r\n\x1a\nbinary"), "scan.png")},
        content_type="multipart/form-data",
    )
    f = File.query.filter_by(workspace_id=workspace.id).one()
    assert f.indexed_at is None
    assert f.index_error == "no text extracted"
