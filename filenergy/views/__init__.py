from flask import render_template

from filenergy import app, csrf
from filenergy.views.api_v1 import api_v1_bp
from filenergy.views.ask import ask_bp
from filenergy.views.audit import audit_bp
from filenergy.views.billing import billing_bp
from filenergy.views.collections import collections_bp
from filenergy.views.connectors import connectors_bp
from filenergy.views.conversation_share import conversation_share_bp
from filenergy.views.dashboard import dashboard_bp
from filenergy.views.docs import docs_bp
from filenergy.views.file import file_bp
from filenergy.views.health import health_bp
from filenergy.views.index import index_bp
from filenergy.views.onboarding import onboarding_bp
from filenergy.views.saml import saml_bp
from filenergy.views.settings_views import settings_bp
from filenergy.views.share import share_bp
from filenergy.views.user import user_bp
from filenergy.views.workspace import workspace_bp

app.register_blueprint(index_bp)
app.register_blueprint(user_bp, url_prefix="/user")
app.register_blueprint(file_bp, url_prefix="/file")
app.register_blueprint(ask_bp, url_prefix="/ask")
app.register_blueprint(workspace_bp, url_prefix="/w")
app.register_blueprint(settings_bp, url_prefix="/settings")
app.register_blueprint(audit_bp, url_prefix="/audit")
app.register_blueprint(collections_bp, url_prefix="/collections")
app.register_blueprint(share_bp, url_prefix="/s")
app.register_blueprint(billing_bp, url_prefix="/webhooks")
app.register_blueprint(api_v1_bp, url_prefix="/api/v1")
app.register_blueprint(docs_bp, url_prefix="/api/v1")
app.register_blueprint(onboarding_bp, url_prefix="/onboarding")
app.register_blueprint(dashboard_bp, url_prefix="/dashboard")
app.register_blueprint(connectors_bp, url_prefix="/connectors")
app.register_blueprint(conversation_share_bp, url_prefix="/sc")
app.register_blueprint(saml_bp, url_prefix="/saml")
app.register_blueprint(health_bp)

# API keys + Stripe webhook authenticate themselves (Bearer / HMAC); they
# are not browser-driven and don't need CSRF tokens.
csrf.exempt(api_v1_bp)
csrf.exempt(billing_bp)
# SAML ACS receives a SAMLResponse from the IdP, not a browser form, so
# there's no CSRF token available.
csrf.exempt(saml_bp)


@app.errorhandler(404)
def _not_found(_err):
    return render_template("errors/404.html"), 404


@app.errorhandler(500)
def _internal_error(_err):
    return render_template("errors/500.html"), 500
