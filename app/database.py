from datetime import datetime, timezone, timedelta
from pathlib import Path

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Setting(Base):
    __tablename__ = 'settings'
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Lead(Base):
    __tablename__ = 'leads'
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String(200), default='')
    latest_message: Mapped[str] = mapped_column(Text, default='')
    score: Mapped[int] = mapped_column(Integer, default=0)
    grade: Mapped[str] = mapped_column(String(2), default='D')
    reasons: Mapped[str] = mapped_column(Text, default='')
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Publication(Base):
    __tablename__ = 'publications'
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    status: Mapped[str] = mapped_column(String(30), default='pending')
    chat_id: Mapped[str] = mapped_column(String(100))
    text: Mapped[str] = mapped_column(Text, default='')
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Vehicle(Base):
    __tablename__ = 'vehicles'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True, nullable=True)
    facts: Mapped[str] = mapped_column(Text)
    caption: Mapped[str] = mapped_column(Text, default='')
    photo_file_id: Mapped[str] = mapped_column(Text, default='')
    status: Mapped[str] = mapped_column(String(20), default='available')
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    publish_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    repeat_rule: Mapped[str] = mapped_column(String(20), default='once')
    auto_publish: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CommandEvent(Base):
    __tablename__ = 'command_events'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    command: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Database:
    def __init__(self, url):
        if url.startswith('sqlite:///'):
            path = url.removeprefix('sqlite:///')
            if path != ':memory:':
                Path(path).parent.mkdir(parents=True, exist_ok=True)
        kwargs = {'connect_args': {'check_same_thread': False}} if url.startswith('sqlite') else {}
        self.engine = create_engine(url, pool_pre_ping=True, **kwargs)
        self.session = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self, target_chat=''):
        Base.metadata.create_all(self.engine)
        with self.session.begin() as s:
            for key, value in [('paused', 'true'), ('target_chat', target_chat)]:
                if s.get(Setting, key) is None:
                    s.add(Setting(key=key, value=value))

    def get(self, key):
        with self.session() as s:
            row = s.get(Setting, key)
            return row.value if row else ''

    def set(self, key, value):
        with self.session.begin() as s:
            s.merge(Setting(key=key, value=value))

    def catalog(self, key, default):
        return self.get(key) or default

    def record_lead(self, user_id, username, text, score, grade, reasons, opted_out):
        with self.session.begin() as s:
            lead = s.get(Lead, user_id)
            if lead is None:
                lead = Lead(user_id=user_id)
                s.add(lead)
            # Opt-out is sticky; only explicit /start permits future classification again.
            lead.opted_out = bool(lead.opted_out or opted_out)
            lead.username, lead.latest_message = username or '', text[:4000]
            lead.score, lead.grade = (0, 'D') if lead.opted_out else (score, grade)
            lead.reasons, lead.updated_at = reasons, now()
            return lead.grade


    def create_vehicle(self, facts, caption, photo_file_id, publish_at=None, repeat_rule='once', auto_publish=False):
        with self.session.begin() as s:
            row = Vehicle(facts=facts[:4000], caption=caption[:4000], photo_file_id=photo_file_id, publish_at=publish_at, repeat_rule=repeat_rule, auto_publish=auto_publish)
            s.add(row)
            s.flush()
            row.code = f'FF-{row.id:05d}'
            return row.code

    def vehicle(self, code):
        with self.session() as s:
            return s.scalar(select(Vehicle).where(Vehicle.code == code.upper()))

    def update_vehicle(self, code, facts=None, caption=None, photo_file_id=None, publish_at=None, repeat_rule=None, auto_publish=None):
        with self.session.begin() as s:
            row = s.scalar(select(Vehicle).where(Vehicle.code == code.upper()))
            if not row:
                return False
            if facts is not None: row.facts = facts[:4000]
            if caption is not None: row.caption = caption[:4000]
            if photo_file_id is not None: row.photo_file_id = photo_file_id
            if publish_at is not None: row.publish_at = publish_at
            if repeat_rule is not None: row.repeat_rule = repeat_rule
            if auto_publish is not None: row.auto_publish = auto_publish
            row.updated_at = now()
            return True

    def update_vehicle_status(self, code, status):
        with self.session.begin() as s:
            row = s.scalar(select(Vehicle).where(Vehicle.code == code.upper()))
            if not row:
                return False
            row.status, row.updated_at = status, now()
            return True

    def mark_vehicle_published(self, code, message_id):
        with self.session.begin() as s:
            row = s.scalar(select(Vehicle).where(Vehicle.code == code.upper()))
            if row:
                row.telegram_message_id, row.updated_at = message_id, now()

    def due_vehicles(self, at=None, limit=20):
        at = at or now()
        with self.session() as s:
            return s.scalars(select(Vehicle).where(
                Vehicle.auto_publish.is_(True),
                Vehicle.status == 'available',
                Vehicle.publish_at.is_not(None),
                Vehicle.publish_at <= at
            ).order_by(Vehicle.publish_at).limit(limit)).all()

    def advance_vehicle_schedule(self, code):
        with self.session.begin() as s:
            row = s.scalar(select(Vehicle).where(Vehicle.code == code.upper()))
            if not row:
                return
            if row.repeat_rule == 'daily':
                row.publish_at = row.publish_at + timedelta(days=1)
            elif row.repeat_rule == 'weekly':
                row.publish_at = row.publish_at + timedelta(days=7)
            else:
                row.auto_publish = False
            row.updated_at = now()

    def vehicles(self, limit=20):
        with self.session() as s:
            return s.scalars(select(Vehicle).order_by(Vehicle.id.desc()).limit(limit)).all()

    def stats(self):
        with self.session() as s:
            leads = s.scalars(select(Lead)).all()
            posts = s.scalars(select(Publication)).all()
            return {'leads': {g: sum(x.grade == g for x in leads) for g in ['A+', 'A', 'B', 'C', 'D']},
                    'sent': sum(x.status == 'sent' for x in posts),
                    'uncertain': sum(x.status in ('pending', 'uncertain') for x in posts)}

