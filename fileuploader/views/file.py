from base import BaseView
from fileuploader.service.file import FileService


class BaseFileView(BaseView):

    login_exempt = False


class MineView(BaseFileView):

    def get(self, *args, **kargs):

        context = {}
        context["files"] = FileService().filter(user=self.request.user)

        return self.render_to_response(context)
        

class UploadView(BaseFileView):

    def post(self, *args, **kwargs):

        FileService().save_file(self.request.FILES, self.request.user)

        return self.redirect("/file/mine/")


class DownloadView(BaseFileView):

    login_exempt = True

    def get(self, *args, **kwargs):

        context = {}
        context["file"] = FileService().get_object_or_404(url=self.request.GET.get("h"))
        context["file_size"] = FileService().get_size(context["file"])

        return self.render_to_response(context)


class DownloadNowView(BaseFileView):

    login_exempt = True

    def get(self, *args, **kwargs):

        context = {}
        db_file = FileService().get_object_or_404(url=self.request.GET.get("h"))
        
        return self.response_file(db_file.path)


class SearchView(BaseFileView):

    login_exempt = True

    def post(self, *args, **kwargs):

        context = {}
        file_name = self.request.POST.get("name")        
        context["files"] = FileService().filter(name__contains=file_name)

        return self.render_to_response(context)


class DeleteView(BaseFileView):

    def post(self, *args, **kwargs):

        db_file = FileService().get_one(user=self.request.user, id=self.request.POST.get("id"))

        if not FileService().delete(db_file):
            self.response("fail")

        return self.response("ok")