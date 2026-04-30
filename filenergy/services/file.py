import hashlib
import json
import logging
import os

from filenergy import app, db, settings
from filenergy.models import Chunk, File, utcnow
from filenergy.services import embeddings, enrichment, events, extraction, ocr
from filenergy.services.base import BaseService

log = logging.getLogger(__name__)


class FileService(BaseService):

    entity = File

    def save_file(self, request, user, workspace, *, sync_index=None):
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
            workspace_id=workspace.id,
            is_public=is_public,
            size_bytes=size_bytes,
        )
        events.log_event(
            events.FILE_UPLOADED,
            user=user,
            workspace_id=workspace.id,
            file_id=db_file.id,
            name=safe_name,
            size=size_bytes,
        )

        if sync_index is None:
            sync_index = settings.SYNC_INDEXING or app.config.get("TESTING", False)

        if sync_index:
            self.index_file(db_file)
        else:
            self._index_async(db_file.id)

        return self._upload_response(db_file)

    def _upload_response(self, db_file):
        return json.dumps([{
            "name": db_file.name,
            "size": db_file.size_bytes / 1000.0,
            "url": db_file.url,
            "indexed": db_file.indexed_at is not None,
            "id": db_file.id,
        }])

    def _persist_upload(self, **params):
        db_file = self.new(**params)
        db.session.add(db_file)
        db.session.commit()

        db_file.url = hashlib.sha256(str(db_file.id).encode("utf-8")).hexdigest()
        db.session.add(db_file)
        db.session.commit()
        return db_file

    def _index_async(self, file_id: int) -> None:
        """Run indexing via the configured job backend (thread or RQ)."""
        from filenergy.services import jobs
        jobs.enqueue("filenergy.services.file.index_file_by_id", file_id)

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
            if not text and ocr.is_configured():
                # Scanned PDFs and image-only docs land here. Fall back to
                # Claude vision OCR.
                log.info("Trying OCR fallback for %s", db_file.name)
                text = ocr.ocr_file(db_file.path)
            if not text:
                db_file.index_error = "no text extracted"
                db.session.commit()
                events.log_event(
                    events.FILE_INDEX_FAILED,
                    user=db_file.user,
                    workspace_id=db_file.workspace_id,
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
                workspace_id=db_file.workspace_id,
                file_id=db_file.id,
                chunks=len(chunks),
            )
            # Best-effort enrichment: summary + suggested questions.
            # Failures don't unindex the file.
            enrichment.enrich_file(db_file)
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
                workspace_id=db_file.workspace_id,
                file_id=db_file.id,
                reason=str(exc)[:200],
            )
            return False

    def delete(self, db_file):
        if not db_file:
            return False

        file_id = db_file.id
        user = db_file.user
        workspace_id = db_file.workspace_id
        name = db_file.name
        path = db_file.path

        try:
            db.session.delete(db_file)
            db.session.commit()
        except Exception:
            db.session.rollback()
            return False

        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

        events.log_event(
            events.FILE_DELETED,
            user=user,
            workspace_id=workspace_id,
            file_id=file_id,
            name=name,
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

    def search(self, workspace, user, file_name):
        """Within a workspace: name match across files visible to user.

        Anonymous callers (workspace=None) only see public files.
        """
        like = File.name.like(f"%{file_name}%")
        if workspace is None:
            return File.query.filter(File.is_public.is_(True), like).all()
        return (
            File.query.filter(File.workspace_id == workspace.id, like).all()
        )


def index_file_by_id(file_id: int) -> bool:
    """Module-level entry point — importable by RQ workers."""
    f = db.session.get(File, file_id)
    if f is None:
        return False
    return FileService().index_file(f)
