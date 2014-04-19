import os

env = os.environ.get("ENV", "LOCAL")

secret_key = "\xa90\x91\xcd\xce\xf2\xbe\x1d\x87\xbb;\xa7\xf3\x91K\xde\x05*D\x9b6\xe4U\xbf"
login_view = '/user/login/'
UPLOAD_DIR = "files"

configs = {
    "LOCAL": {
        "SQLALCHEMY_DATABASE_URI": 'sqlite:////tmp/test.db',
    },
    "PROD": {
        "SQLALCHEMY_DATABASE_URI": 'sqlite:////tmp/test.db',
    }
}

config = configs[env]
