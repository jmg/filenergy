import datetime
from django.db import models

from django.contrib.auth.models import User


class BaseEntity(models.Model):

    created_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(null=True, blank=True)

    def __init__(self, *args, **kwargs):

        now = datetime.datetime.now()
        kwargs.update(created_at=now, updated_at=now)
        models.Model.__init__(self, *args, **kwargs)

    def object_age(self):

        return get_time_since(self.created_at)

    class Meta:
        abstract = True


class AdminProfile(BaseEntity):

    user = models.ForeignKey(User)


class File(BaseEntity):

    name = models.CharField(max_length=1000)
    path = models.CharField(max_length=1000)
    url = models.CharField(max_length=1000, null=True, blank=True)

    encryption_key = models.CharField(max_length=1000, null=True, blank=True)
    requested = models.BooleanField(default=False)

    user = models.ForeignKey(User, null=True)
