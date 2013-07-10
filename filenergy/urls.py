from django.conf.urls.defaults import patterns, include, url
from django.contrib import admin
from settings import MEDIA_ROOT

from django_conventions import UrlsManager

import fileuploader.views as views_root

admin.autodiscover()

urlpatterns = patterns('',

    url(r'^admin/', include(admin.site.urls)),
    (r'^accounts/', include('registration.urls')),
    (r'^media/(?P<path>.*)$', 'django.views.static.serve', {'document_root': MEDIA_ROOT, 'show_indexes': True}),

)

UrlsManager(urlpatterns, views_root)
