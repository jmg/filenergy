from filenergy import app
from filenergy.views.ask import ask_bp
from filenergy.views.file import file_bp
from filenergy.views.index import index_bp
from filenergy.views.user import user_bp

app.register_blueprint(index_bp)
app.register_blueprint(user_bp, url_prefix="/user")
app.register_blueprint(file_bp, url_prefix="/file")
app.register_blueprint(ask_bp, url_prefix="/ask")
