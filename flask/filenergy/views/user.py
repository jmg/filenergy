from filenergy import app, login_manager
from flask import render_template, request, url_for

from filenergy.models import User


@app.route("/user/login/")
def login():
    return render_template("user/login.html")


@app.route("/user/login/", methods=["POST"])
def login_post():

    username = request.form['username']
    password = request.form['password']
    registered_user = User.query.filter_by(username=username,password=password).first()
    if registered_user is None:
        flash('Username or Password is invalid' , 'error')
        return redirect(url_for('login'))

    login_user(registered_user)
    flash('Logged in successfully')
    return redirect(request.args.get('next') or "/")


@app.route("/user/register/")
def register():
    return render_template("user/register.html")


@app.route("/user/register/", methods=["POST"])
def register_post():

    user = User(username=request.form['email'], email=request.form['email'], password=request.form['password'])
    db.session.add(user)
    db.session.commit()

    flash('User successfully registered')
    return redirect(url_for('login'))