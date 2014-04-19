import simplejson as json
from base import BaseService
from filenergy.models import File
from filenergy import db, settings

import os
import hashlib


class FileService(BaseService):

    entity = File

    def save_file(self, request, user):

        if not os.path.exists(settings.UPLOAD_DIR):
            os.mkdir(settings.UPLOAD_DIR)

        file_obj = request.files.get("files[]")
        file_path = os.path.join(settings.UPLOAD_DIR, file_obj.filename)
        file_obj.save(file_path)

        is_public = bool(request.form.get("is_public", False))

        db_file = self.save_upload(path=file_path, name=file_obj.filename, user=user, is_public=is_public)
        return self.return_response(db_file)

    def return_response(self, db_file):

        result = []
        result.append({
            "name": db_file.name,
            "size": self.get_size(db_file),
            "url": db_file.url,
        })
        response_data = json.dumps(result)
        return response_data

    def save_upload(self, **params):

        db_file = self.new(**params)
        db.session.add(db_file)
        db.session.commit()

        db_file.url = hashlib.sha512(str(db_file.id)).hexdigest()
        db.session.add(db_file)
        db.session.commit()

        return db_file

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

        like_query = (File.name.like("%{0}%".format(file_name)))
        is_public_query = (File.is_public==True)

        if user.is_authenticated():
            query = (is_public_query | (File.user==user)) & like_query
        else:
            query = is_public_query & like_query

        return self.filter(query).all()