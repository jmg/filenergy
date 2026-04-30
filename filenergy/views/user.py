from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from filenergy.services import events
from filenergy.services.user import UserService

user_bp = Blueprint("user", __name__)


@user_bp.route("/login/")
def login():
    return render_template("user/login.html", next=request.args.get("next"))


@user_bp.route("/login/", methods=["POST"])
def login_post():
    email = request.form["email"].strip()
    password = request.form["password"].strip()

    user_svc = UserService()
    error = user_svc.login(email, password)
    if error:
        flash(error, "error")
        return redirect(url_for("user.login"))

    user = user_svc.get_one(email=email)
    events.log_event(events.USER_LOGGED_IN, user=user)

    return redirect(request.form.get("next") or url_for("index.index"))


@user_bp.route("/register/")
def register():
    return render_template("user/register.html")


@user_bp.route("/register/", methods=["POST"])
def register_post():
    email = request.form["email"].strip()
    password = request.form["password"].strip()
    password_again = request.form["password_again"].strip()

    user_svc = UserService()
    error = user_svc.register(email, password, password_again)
    if error:
        flash(error, "error")
        return redirect(url_for("user.register"))

    user = user_svc.get_one(email=email)
    events.log_event(events.USER_REGISTERED, user=user)

    return redirect(url_for("index.index"))


@user_bp.route("/logout/")
def logout():
    user = g.user if g.user.is_authenticated else None
    UserService().logout()
    if user is not None:
        events.log_event(events.USER_LOGGED_OUT, user=user)
    return redirect(url_for("index.index"))
