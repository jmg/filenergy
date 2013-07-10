from django.views.generic import TemplateView
from django.http import HttpResponse, HttpResponseRedirect, HttpResponseServerError, Http404
import simplejson as json
import csv
import os.path
import mimetypes

from django.views.decorators.cache import cache_page
from django.utils.decorators import method_decorator

from fileuploader.utils import render, render_string, convert_to_bool


class Exporter(object):

    def csv(self, response, content):

        writer = csv.writer(response)
        for row in content:
            writer.writerow([value.encode("utf-8") for value in row])

        return response

    def export(self, format, response, content):

        return getattr(self, format)(response, content)


class BaseView(TemplateView):

    csrf_exempt = True

    def dispatch(self, request, *args, **kwargs):

        return TemplateView.dispatch(self, request, *args, **kwargs)

    def redirect(self, url):

        return HttpResponseRedirect(url)

    def response(self, response, no_cache=False, headers={}):
        
        http_response = HttpResponse(response)

        if no_cache:
            http_response["Cache-Control"] = "max-age=0"

        for key, value in headers.iteritems():
            http_response[key] = value
            
        return http_response

    def response_file(self, file_path):        
        
        mimetypes.init()

        try:            
            fsock = open(file_path,"r")

            file_name = os.path.basename(file_path)
            file_size = os.path.getsize(file_path)
            
            mime_type_guess = mimetypes.guess_type(file_name)
            if mime_type_guess is not None:
                response = HttpResponse(fsock, mimetype=mime_type_guess[0])

            response['Content-Disposition'] = 'attachment; filename=' + file_name            
            response["Content-Length"] = os.path.getsize(file_path)
        except IOError:
            response = HttpResponseNotFound()

        return response

    def response_error(self, response):

        return HttpResponseServerError(response)

    def response_404(self):

        raise Http404

    def json_response(self, response):

        return self.response(json.dumps(response))

    def json_loads(self, data):

        return json.loads(data)

    def json_dumps(self, data):

        return json.dumps(data)

    def render(self, template, context):

        return render(template, context)

    def render_string(self, string, context):

        return render_string(string, context)

    def get_list_args(self, startswith):

        return [key[len(startswith):] for key, value in self.request.POST.iteritems() if key.startswith(startswith)]

    def export(self, format, filename, content):

        response = HttpResponse(mimetype='text/%s' % format)
        response['Content-Disposition'] = 'attachment; filename=%s.%s' % (filename, format)
        Exporter().export(format, response, content)
        return response

    def get_params(self, data, params):

        dict_params = {}
        for param in params:
            dict_params[param] = data.get(param)
        return dict_params

    def convert_to_bool(self, data, params):

        convert_to_bool(data, params)

    def get_version_id(self):

        return self.request.session.get("version_id", DEFAULT_VERSION)


class AjaxBaseView(BaseView):

    def on_success(self):

        return self.json_response({"status": "ok"})

    def on_fail(self, error):

        return self.json_response({"status": "fail", "error": error})


class AjaxSaveBaseView(AjaxBaseView):

    def after_save(self, entity):

        pass

    def post(self, *args, **kwargs):

        try:
            self.entity = self.service.save(self.request)
            self.after_save(self.entity)
            return self.render(self.on_success)

        except ValidationError, e:
            return self.render(self.on_fail, str(e))

    def render(self, function, *args, **kwargs):

        return self.response(function(*args, **kwargs))


class AjaxFormSaveBaseView(AjaxSaveBaseView):

    def get_data(self, request):

        return request.POST

    def post(self, *args, **kwargs):

        form = self.form(self.get_data(self.request), *args, **kwargs)

        if (form.is_valid()):
            form.save()
            return self.render(self.on_success)

        return self.render(self.on_error, **form.errors)

    def on_error(self, **data):

        return self.json_response(data)


class CachedView(BaseView):

    @method_decorator(cache_page(60 * 60))
    def dispatch(self, request, *args, **kwargs):
        return super(BaseView, self).dispatch(request, *args, **kwargs)


class ValidationError(Exception):

    pass
