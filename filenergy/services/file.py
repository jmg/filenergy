import hashlib
import json
import logging
import os

from filenergy import db, settings
from filenergy.models import Chunk, File, utcnow
from filenergy.services import embeddings, events, extraction
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

        size_bytes = 0
        try:
            size_bytes = os.path.getsize(file_path)
        except OSError:
            pass

        db_file = self._persist_upload(
            path=file_path,
            name=safe_name,
            user=user,
            is_public=is_public,
            size_bytes=size_bytes,
        )
        events.log_event(
            events.FILE_UPLOADED,
            user=user,
            file_id=db_file.id,
            name=safe_name,
            size=size_bytes,
        )

        self.index_file(db_file)
        return self._upload_response(db_file)

    def _upload_response(self, db_file):
        return json.dumps([{
            "name": db_file.name,
            "size": db_file.size_bytes / 1000.0,
            "url": db_file.url,
            "indexed": db_file.indexed_at is not None,
        }])

    def _persist_upload(self, **params):
        db_file = self.new(**params)
        db.session.add(db_file)
        db.session.commit()

        # Hash the row id so the public URL doesn't leak the integer id.
        db_file.url = hashlib.sha256(str(db_file.id).encode("utf-8")).hexdigest()
        db.session.add(db_file)
        db.session.commit()
        return db_file

    def index_file(self, db_file):
        """Extract text, chunk it, embed each chunk, and persist."""
        if not extraction.is_indexable(db_file.name):
            return False
        if not embeddings.is_configured():
            log.info(
                "Skipping index for %s — VOYAGE_API_KEY not configured",
                db_file.name,
            )
            return False

        try:
            text = extraction.extract_text(db_file.path)
            if not text:
                db_file.index_error = "no text extracted"
                db.session.commit()
                events.log_event(
                    events.FILE_INDEX_FAILED,
                    user=db_file.user,
                    file_id=db_file.id,
                    reason="no_text",
                )
                return False

            db_file.text_content = text
            chunks = extraction.chunk_text(
                text, settings.CHUNK_SIZE, settings.CHUNK_OVERLAP
            )
            if not chunks:
                db_file.index_error = "empty after chunking"
                db.session.commit()
                return False

            vectors = embeddings.embed_documents(chunks)
            # Drop any existing chunks before re-adding (supports reindex).
            Chunk.query.filter_by(file_id=db_file.id).delete()
            for position, (content, vector) in enumerate(zip(chunks, vectors)):
                db.session.add(Chunk(
                    file_id=db_file.id,
                    position=position,
                    content=content,
                    embedding=json.dumps(vector),
                ))

            db_file.indexed_at = utcnow()
            db_file.index_error = None
            db.session.commit()
            events.log_event(
                events.FILE_INDEXED,
                user=db_file.user,
                file_id=db_file.id,
                chunks=len(chunks),
            )
            return True
        except Exception as exc:
            log.exception("Indexing failed for %s", db_file.name)
            db.session.rollback()
            db_file.index_error = str(exc)[:500]
            db.session.add(db_file)
            db.session.commit()
            events.log_event(
                events.FILE_INDEX_FAILED,
                user=db_file.user,
                file_id=db_file.id,
                reason=str(exc)[:200],
            )
            return False

    def delete(self, db_file):
        if not db_file:
            return False

        file_id = db_file.id
        user = db_file.user
        name = db_file.name

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

        events.log_event(
            events.FILE_DELETED, user=user, file_id=file_id, name=name
        )
        return True

    def get_size(self, db_file):
        if db_file.size_bytes:
            return db_file.size_bytes / 1000.0
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
