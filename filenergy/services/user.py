from base import BaseService
from filenergy.models import User
from filenergy import db, settings


class UserService(BaseService):

    entity = User
