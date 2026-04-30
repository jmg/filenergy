from flask import Blueprint, flash, redirect, render_template, request, url_for

from filenergy.services.user import UserService

user_bp = Blueprint("user", __name__)


@user_bp.route("/login/")
def login():
    return render_template("user/login.html", next=request.args.get("next"))


@user_bp.route("/login/", methods=["POST"])
def login_post():
    email = request.form["email"].strip()
    password = request.form["password"].strip()

    error = UserService().login(email, password)
    if error:
        flash(error, "error")
        return redirect(url_for("user.login"))

    return redirect(request.form.get("next") or url_for("index.index"))


@user_bp.route("/register/")
def register():
    return render_template("user/register.html")


@user_bp.route("/register/", methods=["POST"])
def register_post():
    email = request.form["email"].strip()
    password = request.form["password"].strip()
    password_again = request.form["password_again"].strip()

    error = UserService().register(email, password, password_again)
    if error:
        flash(error, "error")
        return redirect(url_for("user.register"))

    return redirect(url_for("index.index"))


@user_bp.route("/logout/")
def logout():
    UserService().logout()
    return redirect(url_for("index.index"))
