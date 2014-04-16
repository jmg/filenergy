from base import BaseService
from filenergy.models import User
from filenergy import db, settings
from sqlalchemy.sql import exists

from flask.ext.login import login_user, logout_user


class UserService(BaseService):

    entity = User

    def register(self, email, password, password_again, username=None):

        if username is None:
            username = email

        if password != password_again:
            return "Passwords don't match."

        if db.session.query(exists().where(self.entity.email==email)).scalar():
            return "An user with that email already exists."

        user = self.new(username=username, email=email)
        user.set_password(password)
        self.save(user)

        login_user(user)

    def login(self, email, password):

        user = self.get_one(email=email)
        if user is None or not user.check_password(password):
            return "Email or password incorrect."

        login_user(user)

    def logout(self):

        logout_user()