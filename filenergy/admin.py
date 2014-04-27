from filenergy import app, db, models

from flask import g
from flask.ext.admin import Admin
from flask.ext.admin.contrib.sqla import ModelView


class AuthModelView(ModelView):

    def is_accessible(self):
        return g.user.is_authenticated() and g.user.is_superuser


admin = Admin(app)
admin.add_view(AuthModelView(models.File, db.session))
admin.add_view(AuthModelView(models.User, db.session))