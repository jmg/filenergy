from filenergy import app, db, login_manager
from flask import render_template, request, url_for, redirect, flash
from flask.ext.login import login_user, logout_user, current_user, login_required
from sqlalchemy.sql import exists

from filenergy.services.user import UserService


@app.route("/user/login/")
def login():

    return render_template("user/login.html", next=request.args.get('next'))


@app.route("/user/login/", methods=["POST"])
def login_post():

    email = request.form['email'].strip()
    password = request.form['password'].strip()

    user = UserService().get_one(email=email)
    if user is None or not user.check_password(password):
        flash("Email or password incorrect.", 'error')
        return redirect(url_for('login'))

    login_user(user)
    return redirect(request.form.get('next') or "/")


@app.route("/user/register/")
def register():

    return render_template("user/register.html")


@app.route("/user/register/", methods=["POST"])
def register_post():

    email = request.form['email'].strip()
    password = request.form['password'].strip()
    password_again = request.form['password_again'].strip()
    username = email

    if password != password_again:
        flash("Passwords don't match.", 'error')
        return redirect(url_for('register'))

    if db.session.query(exists().where(UserService.entity.email==email)).scalar():
        flash("An user with that email already exists.", 'error')
        return redirect(url_for('register'))

    user = UserService().new(username=username, email=email)
    user.set_password(password)

    UserService().save(user)

    login_user(user)

    return redirect("/")


@app.route("/user/logout/")
def logout():

    logout_user()
    return redirect(url_for('index'))