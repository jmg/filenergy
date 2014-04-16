from flask import abort
from filenergy import db


class BaseService(object):

    entity = None
    _repo = property(fget=lambda self: self.entity.query)

    def __getattr__(self, name):
        """
            Delegates automatically all undefined methods on the repository.
        """

        def decorator(*args, **kwargs):

            if self.entity is None:
                raise Exception("entity must be a sqlalchemy model object")

            method = getattr(self._repo, name)
            return method(*args, **kwargs)

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

    def update_or_create(self, *args, **kwargs):

        entity_id = kwargs.pop("id", None)
        if entity_id:
            entity = self.get_one(id=entity_id)
            if entity is None:
                entity = self.new(*args, **kwargs)
            self.set_attrs(entity, kwargs)
        else:
            entity = self.new(*args, **kwargs)

        return entity

    def set_attrs(self, entity, attrs):

        for key, value in attrs.iteritems():
            setattr(entity, key, value)

    def get_object_or_404(self, **kwargs):

        return self.get_one(**kwargs) or abort(404)

    def delete(self, *args, **kwargs):

        logical_delete = kwargs.pop("logical", False)

        objs = self.filter_by(*args, **kwargs)

        if not objs:
            return False

        for obj in objs:
            if not logical_delete:
                obj.delete()
            else:
                obj.active = False
                obj.save()

        return True

    def save(self, obj):

        db.session.add(obj)
        db.session.commit()