from base import BaseView

from django.contrib.auth import logout, login, authenticate
from django.contrib.auth.models import User


class RegisterView(BaseView):    

    def post(self, *args, **kargs):

        params = {
            "username": self.request.POST.get("username"),
            "password": self.request.POST.get("password"),
            "email": self.request.POST.get("email"),
        }

        if params["password"] != self.request.POST.get("password-confirmation"):
            return self.json_response({"status": "fail", "error": "Passwords must match"})

        password = params.pop("password")
        try:
            user = User(**params)
            user.set_password(password)
            user.save()
        except:
            return self.json_response({"status": "fail", "error": "The username already exists"})

        user = authenticate(username=params["username"], password=password)
        login(self.request, user)

        return self.json_response({"status": "ok", "url": "/file/upload/"})