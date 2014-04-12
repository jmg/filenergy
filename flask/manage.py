from flask.ext.script import Manager
from flask.ext.migrate import Migrate, MigrateCommand
from filenergy import app, db

migrate = Migrate(app, db)

manager = Manager(app)
manager.add_command('db', MigrateCommand)

manager.run()