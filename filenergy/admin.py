from flask import g
from flask_admin import Admin
from flask_admin.contrib.sqla import ModelView

from filenergy import app, db, models


class AuthModelView(ModelView):

    def is_accessible(self):
        return g.user.is_authenticated and g.user.is_superuser


admin = Admin(app, name="Filenergy Admin", url="/admin")
admin.add_view(AuthModelView(models.File, db.session, endpoint="admin_file"))
admin.add_view(AuthModelView(models.User, db.session, endpoint="admin_user"))
admin.add_view(AuthModelView(models.Chunk, db.session, endpoint="admin_chunk"))
