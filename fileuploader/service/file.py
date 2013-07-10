from base import BaseService
from fileuploader.models import File

from filenergy.settings import UPLOAD_DIR
import os
import hashlib


class FileService(BaseService):

    entity = File

    def save_file(self, data, user):
            
        if not os.path.exists(UPLOAD_DIR):
            os.mkdir(UPLOAD_DIR)

        file_obj = data.get("file")
        file_content = file_obj.read()
        file_name = file_obj.name

        file_path = os.path.join(UPLOAD_DIR, file_name)

        with open(file_path, "w") as fd:

            fd.write(file_content)
            fd.close()

        self.save(path=file_path, name=file_name, user=user)

    def save(self, **params):

        db_file = self.new(**params)
        db_file.save()

        db_file.url = hashlib.sha512(str(db_file.id)).hexdigest()
        db_file.save()

    def delete(self, db_file):

        if not db_file:
            return False

        try:            
            db_file.delete()
            os.remove(db_file.path)
        except:
            return False

        return True

    def get_size(self, db_file):

        return os.path.getsize(db_file.path) / 1000.0

    def get_content(self, db_file):

        with open(db_file.path, "r") as fd:
            
            file_content = fd.read()
            fd.close()

        return file_content