from datetime import datetime, timezone

from werkzeug.security import check_password_hash, generate_password_hash

from filenergy import db


def utcnow():
    return datetime.now(timezone.utc)


class BaseModel(db.Model):

    __abstract__ = True

    created_at = db.Column(db.DateTime, default=utcnow)


class User(BaseModel):

    __tablename__ = "user"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(255), unique=True)
    password = db.Column(db.String(255))
    email = db.Column(db.String(255), unique=True)
    is_superuser = db.Column(db.Boolean(), default=False)

    @property
    def is_authenticated(self):
        return True

    @property
    def is_active(self):
        return True

    @property
    def is_anonymous(self):
        return False

    def get_id(self):
        return str(self.id)

    def set_password(self, password):
        self.password = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password, password)

    def __str__(self):
        return self.email or self.username or f"User<{self.id}>"


class File(BaseModel):

    __tablename__ = "file"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(1000))
    path = db.Column(db.String(1000))
    url = db.Column(db.String(1024), unique=True, index=True)
    size_bytes = db.Column(db.Integer, default=0)
    is_public = db.Column(db.Boolean(), default=False)

    indexed_at = db.Column(db.DateTime, nullable=True)
    index_error = db.Column(db.Text, nullable=True)
    text_content = db.Column(db.Text, nullable=True)

    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    user = db.relationship("User", backref=db.backref("files", lazy="dynamic"))

    chunks = db.relationship(
        "Chunk",
        backref="file",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )

    @property
    def index_status(self):
        if self.indexed_at:
            return "indexed"
        if self.index_error:
            return "error"
        return "pending"


class Chunk(BaseModel):
    """A retrieval-sized slice of a file's extracted text plus its embedding."""

    __tablename__ = "chunk"

    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(db.Integer, db.ForeignKey("file.id"), index=True)
    position = db.Column(db.Integer)
    content = db.Column(db.Text)
    embedding = db.Column(db.Text)


class Conversation(BaseModel):
    """A multi-turn /ask thread anchored to a user."""

    __tablename__ = "conversation"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    title = db.Column(db.String(255))

    user = db.relationship("User", backref=db.backref("conversations", lazy="dynamic"))
    messages = db.relationship(
        "Message",
        backref="conversation",
        lazy="dynamic",
        order_by="Message.id",
        cascade="all, delete-orphan",
    )


class Message(BaseModel):
    """One user or assistant turn inside a Conversation."""

    __tablename__ = "message"

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(
        db.Integer, db.ForeignKey("conversation.id"), index=True
    )
    role = db.Column(db.String(16))  # "user" | "assistant"
    content = db.Column(db.Text)
    sources_json = db.Column(db.Text, nullable=True)


class Event(BaseModel):
    """Analytics: every meaningful action a user takes.

    Cheap to query for product usage, billing, and rate-limit auditing.
    """

    __tablename__ = "event"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True, nullable=True)
    type = db.Column(db.String(64), index=True)
    metadata_json = db.Column(db.Text, nullable=True)

    user = db.relationship("User", backref=db.backref("events", lazy="dynamic"))
