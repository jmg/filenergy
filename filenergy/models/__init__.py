from datetime import datetime, timezone

from werkzeug.security import check_password_hash, generate_password_hash

from filenergy import db


def utcnow():
    """Naive UTC `datetime`.

    We deliberately strip tzinfo so values match what SQLite returns on read
    (SQLAlchemy's vanilla `DateTime` column round-trips as naive). Comparing
    a naive read-back to a naive `now()` keeps `expires_at` checks simple.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class BaseModel(db.Model):

    __abstract__ = True

    created_at = db.Column(db.DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


class Workspace(BaseModel):
    """A tenant boundary. Files, conversations, and events all live under one."""

    __tablename__ = "workspace"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120))
    slug = db.Column(db.String(64), unique=True, index=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"))

    plan = db.Column(db.String(32), default="free")  # free / pro / team
    stripe_customer_id = db.Column(db.String(64), nullable=True)
    stripe_subscription_id = db.Column(db.String(64), nullable=True)
    subscription_status = db.Column(db.String(32), nullable=True)

    owner = db.relationship("User", foreign_keys=[owner_id])
    members = db.relationship(
        "WorkspaceMember", backref="workspace", lazy="dynamic",
        cascade="all, delete-orphan",
    )
    files = db.relationship(
        "File", backref="workspace", lazy="dynamic",
        cascade="all, delete-orphan",
    )
    conversations = db.relationship(
        "Conversation", backref="workspace", lazy="dynamic",
        cascade="all, delete-orphan",
    )

    def has_member(self, user) -> bool:
        if user is None or not getattr(user, "id", None):
            return False
        return WorkspaceMember.query.filter_by(
            workspace_id=self.id, user_id=user.id
        ).first() is not None

    def role_of(self, user):
        if user is None or not getattr(user, "id", None):
            return None
        m = WorkspaceMember.query.filter_by(
            workspace_id=self.id, user_id=user.id
        ).first()
        return m.role if m else None

    def __str__(self):
        return self.name or f"Workspace<{self.id}>"


class WorkspaceMember(BaseModel):
    """User ↔ Workspace with a role."""

    __tablename__ = "workspace_member"
    __table_args__ = (
        db.UniqueConstraint("workspace_id", "user_id", name="uq_member"),
    )

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspace.id"), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    role = db.Column(db.String(16), default="member")  # owner / admin / member

    user = db.relationship("User", backref=db.backref("memberships", lazy="dynamic"))


class WorkspaceInvitation(BaseModel):
    """Pending invite. Accept by token to create a WorkspaceMember row."""

    __tablename__ = "workspace_invitation"

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspace.id"), index=True)
    email = db.Column(db.String(255))
    token = db.Column(db.String(64), unique=True, index=True)
    role = db.Column(db.String(16), default="member")
    invited_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    accepted_at = db.Column(db.DateTime, nullable=True)
    expires_at = db.Column(db.DateTime)

    workspace = db.relationship("Workspace")
    invited_by = db.relationship("User")


# ---------------------------------------------------------------------------
# Domain
# ---------------------------------------------------------------------------


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
    summary = db.Column(db.Text, nullable=True)
    suggested_questions_json = db.Column(db.Text, nullable=True)

    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    workspace_id = db.Column(
        db.Integer, db.ForeignKey("workspace.id"), index=True
    )
    collection_id = db.Column(
        db.Integer, db.ForeignKey("collection.id"), nullable=True, index=True
    )

    user = db.relationship(
        "User", foreign_keys=[user_id],
        backref=db.backref("files", lazy="dynamic"),
    )

    chunks = db.relationship(
        "Chunk", backref="file", lazy="dynamic",
        cascade="all, delete-orphan",
    )
    share_links = db.relationship(
        "ShareLink", backref="file", lazy="dynamic",
        cascade="all, delete-orphan",
    )

    @property
    def index_status(self):
        if self.indexed_at:
            return "indexed"
        if self.index_error:
            return "error"
        return "pending"

    @property
    def suggested_questions(self) -> list:
        import json
        if not self.suggested_questions_json:
            return []
        try:
            return json.loads(self.suggested_questions_json)
        except Exception:
            return []


class Collection(BaseModel):
    """A folder/notebook within a workspace.

    Files can belong to one collection or none. Retrieval can be scoped
    to a collection so users can chat with a subset of their library.
    """

    __tablename__ = "collection"
    __table_args__ = (
        db.UniqueConstraint("workspace_id", "slug", name="uq_collection_slug"),
    )

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspace.id"), index=True)
    name = db.Column(db.String(120))
    slug = db.Column(db.String(64), index=True)
    description = db.Column(db.Text, nullable=True)

    workspace = db.relationship("Workspace")
    files = db.relationship(
        "File", backref="collection", lazy="dynamic",
        foreign_keys="File.collection_id",
    )


class Chunk(BaseModel):
    """A retrieval-sized slice of a file's extracted text plus its embedding."""

    __tablename__ = "chunk"

    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(db.Integer, db.ForeignKey("file.id"), index=True)
    position = db.Column(db.Integer)
    content = db.Column(db.Text)
    embedding = db.Column(db.Text)


class Conversation(BaseModel):

    __tablename__ = "conversation"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    workspace_id = db.Column(
        db.Integer, db.ForeignKey("workspace.id"), index=True
    )
    title = db.Column(db.String(255))

    user = db.relationship(
        "User", foreign_keys=[user_id],
        backref=db.backref("conversations", lazy="dynamic"),
    )
    messages = db.relationship(
        "Message", backref="conversation", lazy="dynamic",
        order_by="Message.id", cascade="all, delete-orphan",
    )


class Message(BaseModel):

    __tablename__ = "message"

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(
        db.Integer, db.ForeignKey("conversation.id"), index=True
    )
    role = db.Column(db.String(16))
    content = db.Column(db.Text)
    sources_json = db.Column(db.Text, nullable=True)


class Event(BaseModel):

    __tablename__ = "event"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("user.id"), index=True, nullable=True
    )
    workspace_id = db.Column(
        db.Integer, db.ForeignKey("workspace.id"), index=True, nullable=True
    )
    type = db.Column(db.String(64), index=True)
    metadata_json = db.Column(db.Text, nullable=True)

    user = db.relationship(
        "User", foreign_keys=[user_id],
        backref=db.backref("events", lazy="dynamic"),
    )


# ---------------------------------------------------------------------------
# Programmatic access + sharing
# ---------------------------------------------------------------------------


class ApiKey(BaseModel):
    """Bearer tokens scoped to a workspace.

    Only the SHA-256 hash is stored; the plaintext is shown once at creation.
    """

    __tablename__ = "api_key"

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspace.id"), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    name = db.Column(db.String(120))
    prefix = db.Column(db.String(16))  # display-friendly first chars
    token_hash = db.Column(db.String(128), unique=True, index=True)
    last_used_at = db.Column(db.DateTime, nullable=True)
    revoked_at = db.Column(db.DateTime, nullable=True)

    workspace = db.relationship("Workspace")
    user = db.relationship("User")

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


class ShareLink(BaseModel):
    """Unguessable public URL with optional TTL and download cap."""

    __tablename__ = "share_link"

    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(db.Integer, db.ForeignKey("file.id"), index=True)
    token = db.Column(db.String(64), unique=True, index=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    max_downloads = db.Column(db.Integer, nullable=True)
    download_count = db.Column(db.Integer, default=0)
    revoked_at = db.Column(db.DateTime, nullable=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))

    created_by = db.relationship("User")

    def is_active(self, *, now=None) -> bool:
        if self.revoked_at is not None:
            return False
        now = now or utcnow()
        if self.expires_at is not None and now >= self.expires_at:
            return False
        if self.max_downloads is not None and (
            self.download_count or 0
        ) >= self.max_downloads:
            return False
        return True


class WebhookSubscription(BaseModel):
    """Outbound webhook target. Customer registers a URL + secret for HMAC."""

    __tablename__ = "webhook_subscription"

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspace.id"), index=True)
    url = db.Column(db.String(2000))
    secret = db.Column(db.String(128))
    events_json = db.Column(db.Text)
    enabled = db.Column(db.Boolean(), default=True)
    last_status = db.Column(db.Integer, nullable=True)
    last_attempt_at = db.Column(db.DateTime, nullable=True)
    failure_count = db.Column(db.Integer, default=0)

    workspace = db.relationship("Workspace")
    deliveries = db.relationship(
        "WebhookDelivery", backref="subscription", lazy="dynamic",
        cascade="all, delete-orphan",
    )

    @property
    def event_types(self) -> list[str]:
        import json
        try:
            return json.loads(self.events_json or "[]")
        except Exception:
            return []


class WebhookDelivery(BaseModel):
    """One attempted webhook delivery, success or failure."""

    __tablename__ = "webhook_delivery"

    id = db.Column(db.Integer, primary_key=True)
    subscription_id = db.Column(
        db.Integer, db.ForeignKey("webhook_subscription.id"), index=True
    )
    event_type = db.Column(db.String(64))
    payload_json = db.Column(db.Text)
    response_status = db.Column(db.Integer, nullable=True)
    response_body = db.Column(db.Text, nullable=True)
    delivered_at = db.Column(db.DateTime, nullable=True)
    error = db.Column(db.Text, nullable=True)
