Filenergy
=========

Filenergy is a file sharing sharing tool written in python using the flask micro-framework and jquery.upload for the file uploader interface.

###Live Demo

http://filenergy.crawley-cloud.com/

###Features

It allows your users to upload multiple files with a nice interface

![alt tag](https://raw.github.com/jmg/filenergy/master/screenshots/upload.png)

It provides a file search capability

![alt tag](https://raw.github.com/jmg/filenergy/master/screenshots/search.png)

And you users are able to watch for their uploaded files and download them

![alt tag](https://raw.github.com/jmg/filenergy/master/screenshots/myfiles.png)

![alt tag](https://raw.github.com/jmg/filenergy/master/screenshots/download.png)

========

###Setup

####Install the python dependencies

```
pip install -r requirements.txt
```

####Setup the database

```
python manage.py db upgrade
```

####Run the server

```
python manage.py runserver
```
