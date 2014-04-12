from filenergy import app, db, login_manager
from flask import render_template, request, url_for, redirect
from flask.ext.login import login_user, logout_user, current_user, login_required

from filenergy.models import User


@app.route("/user/login/")
def login():

    return render_template("user/login.html")


@app.route("/user/login/", methods=["POST"])
def login_post():

    email = request.form['email']
    password = request.form['password']
    registered_user = User.query.filter_by(email=email,password=password).first()
    if registered_user is None:
        return redirect(url_for('login'))

    login_user(registered_user)
    return redirect(request.args.get('next') or "/")


@app.route("/user/register/")
def register():

    return render_template("user/register.html")


@app.route("/user/register/", methods=["POST"])
def register_post():

    user = User(username=request.form['email'], email=request.form['email'], password=request.form['password'])
    db.session.add(user)
    db.session.commit()

    return redirect(url_for('login'))


@app.route("/user/logout/")
def logout():

    logout_user()
    return redirect(url_for('index'))