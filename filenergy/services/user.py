from flask_login import login_user, logout_user
from sqlalchemy.sql import exists

from filenergy import db
from filenergy.models import User
from filenergy.services.base import BaseService


class UserService(BaseService):

    entity = User

    def register(self, email, password, password_again, username=None):
        if username is None:
            username = email

        if password != password_again:
            return "Passwords don't match."

        if db.session.query(exists().where(self.entity.email == email)).scalar():
            return "A user with that email already exists."

        user = self.new(username=username, email=email)
        user.set_password(password)
        self.save(user)
        login_user(user)
        return None

    def login(self, email, password):
        user = self.get_one(email=email)
        if user is None or not user.check_password(password):
            return "Email or password incorrect."

        login_user(user)
        return None

    def logout(self):
        logout_user()
