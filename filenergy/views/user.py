from filenergy import app, db
from flask import render_template, request, url_for, redirect, flash

from filenergy.services.user import UserService


@app.route("/user/login/")
def login():

    return render_template("user/login.html", next=request.args.get('next'))


@app.route("/user/login/", methods=["POST"])
def login_post():

    email = request.form['email'].strip()
    password = request.form['password'].strip()

    error = UserService().login(email, password)
    if error:
        flash(error, 'error')
        return redirect(url_for('login'))

    return redirect(request.form.get('next') or "/")


@app.route("/user/register/")
def register():

    return render_template("user/register.html")


@app.route("/user/register/", methods=["POST"])
def register_post():

    email = request.form['email'].strip()
    password = request.form['password'].strip()
    password_again = request.form['password_again'].strip()

    error = UserService().register(email, password, password_again)
    if error:
        flash(error, 'error')
        return redirect(url_for('register'))

    return redirect("/")


@app.route("/user/logout/")
def logout():

    UserService().logout()
    return redirect(url_for('index'))