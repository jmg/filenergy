import hashlib
import json
import logging
import os

from filenergy import db, settings
from filenergy.models import Chunk, File
from filenergy.services import embeddings, extraction
from filenergy.services.base import BaseService

log = logging.getLogger(__name__)


class FileService(BaseService):

    entity = File

    def save_file(self, request, user):
        os.makedirs(settings.UPLOAD_DIR, exist_ok=True)

        file_obj = request.files.get("files[]")
        if file_obj is None:
            return json.dumps([])

        safe_name = os.path.basename(file_obj.filename or "upload")
        file_path = os.path.join(settings.UPLOAD_DIR, safe_name)
        file_obj.save(file_path)

        is_public = request.form.get("is_public", "").lower() == "true"

        db_file = self._persist_upload(
            path=file_path, name=safe_name, user=user, is_public=is_public
        )
        self._index_file(db_file)
        return self._upload_response(db_file)

    def _upload_response(self, db_file):
        return json.dumps([{
            "name": db_file.name,
            "size": self.get_size(db_file),
            "url": db_file.url,
            "indexed": db_file.indexed_at is not None,
        }])

    def _persist_upload(self, **params):
        db_file = self.new(**params)
        db.session.add(db_file)
        db.session.commit()

        # The public URL is a hash of the row id — unguessable without leaking ids.
        db_file.url = hashlib.sha256(str(db_file.id).encode("utf-8")).hexdigest()
        db.session.add(db_file)
        db.session.commit()
        return db_file

    def _index_file(self, db_file):
        """Extract text, chunk it, embed each chunk, and persist."""
        if not extraction.is_indexable(db_file.name):
            return
        if not embeddings.is_configured():
            log.info("Skipping index for %s — VOYAGE_API_KEY not configured", db_file.name)
            return

        try:
            text = extraction.extract_text(db_file.path)
            if not text:
                return

            db_file.text_content = text
            chunks = extraction.chunk_text(
                text, settings.CHUNK_SIZE, settings.CHUNK_OVERLAP
            )
            if not chunks:
                return

            vectors = embeddings.embed_documents(chunks)
            for position, (content, vector) in enumerate(zip(chunks, vectors)):
                db.session.add(Chunk(
                    file_id=db_file.id,
                    position=position,
                    content=content,
                    embedding=json.dumps(vector),
                ))

            db_file.indexed_at = db.func.now()
            db_file.index_error = None
            db.session.commit()
        except Exception as exc:
            log.exception("Indexing failed for %s", db_file.name)
            db.session.rollback()
            db_file.index_error = str(exc)[:500]
            db.session.add(db_file)
            db.session.commit()

    def delete(self, db_file):
        if not db_file:
            return False

        try:
            db.session.delete(db_file)
            db.session.commit()
        except Exception:
            db.session.rollback()
            return False

        try:
            if db_file.path and os.path.exists(db_file.path):
                os.remove(db_file.path)
        except OSError:
            pass

        return True

    def get_size(self, db_file):
        try:
            return os.path.getsize(db_file.path) / 1000.0
        except OSError:
            return 0

    def get_content(self, db_file):
        with open(db_file.path, "rb") as fd:
            return fd.read()

    def search(self, user, file_name):
        like = File.name.like(f"%{file_name}%")
        is_public = File.is_public.is_(True)

        if user.is_authenticated:
            query = (is_public | (File.user == user)) & like
        else:
            query = is_public & like

        return File.query.filter(query).all()
