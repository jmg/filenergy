from flask import abort

from filenergy import db


class BaseService:

    entity = None

    @property
    def _repo(self):
        return self.entity.query

    def __getattr__(self, name):
        """Delegate any unknown method to the SQLAlchemy query object."""

        def decorator(*args, **kwargs):
            if self.entity is None:
                raise Exception("entity must be a SQLAlchemy model object")
            return getattr(self._repo, name)(*args, **kwargs)

        return decorator

    def get_one(self, *args, **kwargs):
        return self.filter_by(*args, **kwargs).first()

    def new(self, *args, **kwargs):
        return self.entity(*args, **kwargs)

    def get_or_new(self, *args, **kwargs):
        obj = self.get_one(*args, **kwargs)
        if obj is None:
            obj = self.new(*args, **kwargs)
        return obj

    def get_object_or_404(self, **kwargs):
        return self.get_one(**kwargs) or abort(404)

    def save(self, obj):
        db.session.add(obj)
        db.session.commit()
        return obj
