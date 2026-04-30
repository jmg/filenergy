from filenergy import app
from filenergy.views.api_v1 import api_v1_bp
from filenergy.views.ask import ask_bp
from filenergy.views.billing import billing_bp
from filenergy.views.file import file_bp
from filenergy.views.index import index_bp
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
app.register_blueprint(share_bp, url_prefix="/s")
app.register_blueprint(billing_bp, url_prefix="/webhooks")
app.register_blueprint(api_v1_bp, url_prefix="/api/v1")
