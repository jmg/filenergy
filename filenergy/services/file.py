from base import BaseService
from filenergy.models import File
from filenergy import db, settings

import os
import hashlib


class FileService(BaseService):

    entity = File

    def save_file(self, data, user):

        if not os.path.exists(settings.UPLOAD_DIR):
            os.mkdir(settings.UPLOAD_DIR)

        file_obj = data.get("file")
        file_path = os.path.join(settings.UPLOAD_DIR, file_obj.filename)
        file_obj.save(file_path)

        self.save_upload(path=file_path, name=file_obj.filename, user=user)

    def save_upload(self, **params):

        db_file = self.new(**params)
        db.session.add(db_file)
        db.session.commit()

        db_file.url = hashlib.sha512(str(db_file.id)).hexdigest()
        db.session.add(db_file)
        db.session.commit()

    def delete(self, db_file):

        if not db_file:
            return False

        try:
            db.session.delete(db_file)
            os.remove(db_file.path)
        except:
            return False

        db.session.commit()

        return True

    def get_size(self, db_file):

        return os.path.getsize(db_file.path) / 1000.0

    def get_content(self, db_file):

        with open(db_file.path, "r") as fd:

            file_content = fd.read()
            fd.close()

        return file_content

    def search(self, user, file_name):

        return self.filter(((File.is_public==True) | (File.user==user)) & (File.name.like("%{0}%".format(file_name)))).all()