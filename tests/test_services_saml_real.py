"""Tests for the python3-saml-backed SAML service.

The python3-saml dep needs libxml2 + xmlsec C libs and isn't installed
in the test image. We mock it in `sys.modules` to exercise our wrapper
without requiring the real library.
"""
import sys
import types

import pytest

from filenergy.services import saml_sso


def test_is_configured_false_default(monkeypatch):
    monkeypatch.delenv("SAML_ENABLED", raising=False)
    assert saml_sso.is_configured() is False


def test_is_configured_true_with_env(monkeypatch):
    monkeypatch.setenv("SAML_ENABLED", "true")
    monkeypatch.setenv("SAML_IDP_METADATA_URL", "https://idp/x")
    assert saml_sso.is_configured() is True


def test_status_shape(monkeypatch):
    monkeypatch.delenv("SAML_ENABLED", raising=False)
    s = saml_sso.status()
    assert "enabled" in s
    assert "library_available" in s
    assert "idp_metadata_url" in s


def test_init_request_unconfigured_raises(monkeypatch):
    monkeypatch.delenv("SAML_ENABLED", raising=False)
    with pytest.raises(saml_sso.SAMLError):
        saml_sso.init_request(redirect_uri="x")


def test_process_response_unconfigured_raises(monkeypatch):
    monkeypatch.delenv("SAML_ENABLED", raising=False)
    with pytest.raises(saml_sso.SAMLError):
        saml_sso.process_response({})


def test_init_request_without_library(monkeypatch, app):
    """Configured but python3-saml not installed → SAMLError, not crash."""
    monkeypatch.setenv("SAML_ENABLED", "true")
    monkeypatch.setenv("SAML_IDP_METADATA_URL", "https://idp/x")
    # Make sure the import fails.
    monkeypatch.setitem(sys.modules, "onelogin", None)
    with app.test_request_context(), pytest.raises(saml_sso.SAMLError):
        saml_sso.init_request(redirect_uri="x")


def _install_fake_saml(monkeypatch, *, attrs=None, name_id="alice@x.co",
                       authenticated=True, errors=None):
    """Install a fake `onelogin.saml2.auth` so SAML calls don't need libxml2."""
    fake_root = types.ModuleType("onelogin")
    fake_saml2 = types.ModuleType("onelogin.saml2")
    fake_auth = types.ModuleType("onelogin.saml2.auth")

    captured = {}

    class FakeAuth:
        def __init__(self, req, settings):
            captured["req"] = req
            captured["settings"] = settings

        def login(self, return_to=None):
            captured["return_to"] = return_to
            return "https://idp/sso?RelayState=x"

        def process_response(self):
            captured["processed"] = True

        def get_errors(self):
            return errors or []

        def is_authenticated(self):
            return authenticated

        def get_nameid(self):
            return name_id

        def get_attributes(self):
            return attrs or {}

    fake_auth.OneLogin_Saml2_Auth = FakeAuth
    fake_saml2.auth = fake_auth
    fake_root.saml2 = fake_saml2
    monkeypatch.setitem(sys.modules, "onelogin", fake_root)
    monkeypatch.setitem(sys.modules, "onelogin.saml2", fake_saml2)
    monkeypatch.setitem(sys.modules, "onelogin.saml2.auth", fake_auth)
    return captured


def _stub_metadata(monkeypatch):
    metadata = b'''<?xml version="1.0"?>
<EntityDescriptor xmlns="urn:oasis:names:tc:SAML:2.0:metadata"
                  entityID="https://idp.example.com">
  <IDPSSODescriptor>
    <KeyDescriptor>
      <KeyInfo xmlns="http://www.w3.org/2000/09/xmldsig#">
        <X509Data><X509Certificate>FAKECERT==</X509Certificate></X509Data>
      </KeyInfo>
    </KeyDescriptor>
    <SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
                         Location="https://idp.example.com/sso"/>
  </IDPSSODescriptor>
</EntityDescriptor>'''

    class _R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=None): return metadata

    monkeypatch.setattr("urllib.request.urlopen",
                         lambda url, timeout=None: _R())


def test_init_request_returns_idp_url(monkeypatch, app):
    monkeypatch.setenv("SAML_ENABLED", "true")
    monkeypatch.setenv("SAML_IDP_METADATA_URL", "https://idp.example.com/metadata")
    monkeypatch.setenv("SAML_SP_ENTITY_ID", "https://app.example.com/saml")
    monkeypatch.setenv("SAML_SP_ACS_URL", "https://app.example.com/saml/acs")
    _stub_metadata(monkeypatch)
    captured = _install_fake_saml(monkeypatch)
    with app.test_request_context():
        url = saml_sso.init_request(redirect_uri="https://app.example.com/")
    assert url.startswith("https://idp")
    assert captured["return_to"] == "https://app.example.com/"


def test_process_response_provisions_user(monkeypatch, app, db):
    monkeypatch.setenv("SAML_ENABLED", "true")
    monkeypatch.setenv("SAML_IDP_METADATA_URL", "https://idp.example.com/metadata")
    _stub_metadata(monkeypatch)
    _install_fake_saml(monkeypatch, name_id="newby@samlmail.com")
    with app.test_request_context(method="POST", data={"SAMLResponse": "x"}):
        user = saml_sso.process_response({"SAMLResponse": "x"})
    assert user.email == "newby@samlmail.com"
    # Workspace was provisioned.
    assert any(m.role == "owner" for m in user.memberships.all())


def test_process_response_links_existing_user(monkeypatch, app, db, user):
    monkeypatch.setenv("SAML_ENABLED", "true")
    monkeypatch.setenv("SAML_IDP_METADATA_URL", "https://idp.example.com/metadata")
    _stub_metadata(monkeypatch)
    _install_fake_saml(monkeypatch, name_id=user.email)
    with app.test_request_context(method="POST", data={"SAMLResponse": "x"}):
        returned = saml_sso.process_response({})
    assert returned.id == user.id


def test_process_response_attribute_email_fallback(monkeypatch, app, db):
    monkeypatch.setenv("SAML_ENABLED", "true")
    monkeypatch.setenv("SAML_IDP_METADATA_URL", "https://idp.example.com/metadata")
    _stub_metadata(monkeypatch)
    _install_fake_saml(
        monkeypatch, name_id="", attrs={"email": ["fallback@x.co"]}
    )
    with app.test_request_context(method="POST", data={"SAMLResponse": "x"}):
        user = saml_sso.process_response({})
    assert user.email == "fallback@x.co"


def test_process_response_with_errors_raises(monkeypatch, app):
    monkeypatch.setenv("SAML_ENABLED", "true")
    monkeypatch.setenv("SAML_IDP_METADATA_URL", "https://idp.example.com/metadata")
    _stub_metadata(monkeypatch)
    _install_fake_saml(
        monkeypatch, errors=["invalid_response_signature"], authenticated=False
    )
    with app.test_request_context(method="POST", data={"SAMLResponse": "x"}):
        with pytest.raises(saml_sso.SAMLError):
            saml_sso.process_response({})


def test_process_response_unauthenticated_raises(monkeypatch, app):
    monkeypatch.setenv("SAML_ENABLED", "true")
    monkeypatch.setenv("SAML_IDP_METADATA_URL", "https://idp.example.com/metadata")
    _stub_metadata(monkeypatch)
    _install_fake_saml(monkeypatch, authenticated=False)
    with app.test_request_context(method="POST", data={"SAMLResponse": "x"}):
        with pytest.raises(saml_sso.SAMLError):
            saml_sso.process_response({})


def test_process_response_missing_email_raises(monkeypatch, app):
    monkeypatch.setenv("SAML_ENABLED", "true")
    monkeypatch.setenv("SAML_IDP_METADATA_URL", "https://idp.example.com/metadata")
    _stub_metadata(monkeypatch)
    _install_fake_saml(monkeypatch, name_id="", attrs={})
    with app.test_request_context(method="POST", data={"SAMLResponse": "x"}):
        with pytest.raises(saml_sso.SAMLError):
            saml_sso.process_response({})


def test_idp_metadata_fetch_failure(monkeypatch):
    def boom(url, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(saml_sso.SAMLError):
        saml_sso._fetch_idp_metadata("https://idp/x")


def test_idp_metadata_invalid_xml(monkeypatch):
    class _R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=None): return b"not xml at all"

    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=None: _R())
    with pytest.raises(saml_sso.SAMLError):
        saml_sso._fetch_idp_metadata("https://idp/x")


def test_has_lib_returns_false_without_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "onelogin", None)
    assert saml_sso._has_lib() is False
