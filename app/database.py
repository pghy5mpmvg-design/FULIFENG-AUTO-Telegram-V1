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
    grade: Mapped[str] = mapped_column(String(10), default='C')
    reasons: Mapped[str] = mapped_column(Text, default='')
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False)
    first_name: Mapped[str] = mapped_column(String(200), default='')
    language_code: Mapped[str] = mapped_column(String(30), default='')
    vehicle_code: Mapped[str] = mapped_column(String(40), default='')
    last_message: Mapped[str] = mapped_column(Text, default='')
    status: Mapped[str] = mapped_column(String(30), default='new')
    budget: Mapped[str] = mapped_column(String(100), default='')
    city: Mapped[str] = mapped_column(String(150), default='')
    vehicle_preference: Mapped[str] = mapped_column(String(30), default='')
    purchase_timing: Mapped[str] = mapped_column(String(100), default='')
    manager_note: Mapped[str] = mapped_column(Text, default='')
    next_follow_up: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    @property
    def id(self): return self.user_id
    @property
    def telegram_user_id(self): return self.user_id


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
    photo_file_ids: Mapped[str] = mapped_column(Text, default='')
    status: Mapped[str] = mapped_column(String(20), default='available')
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    publish_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    repeat_rule: Mapped[str] = mapped_column(String(20), default='once')
    auto_publish: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class VehiclePublication(Base):
    __tablename__ = 'vehicle_publications'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vehicle_code: Mapped[str] = mapped_column(String(40), index=True)
    chat_id: Mapped[str] = mapped_column(String(100), default='')
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    caption: Mapped[str] = mapped_column(Text, default='')
    mode: Mapped[str] = mapped_column(String(20), default='manual')
    status: Mapped[str] = mapped_column(String(20), default='sent')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class VehicleDetail(Base):
    __tablename__ = 'vehicle_details'
    vehicle_code: Mapped[str] = mapped_column(String(40), primary_key=True)
    brand: Mapped[str] = mapped_column(String(100), default='')
    model: Mapped[str] = mapped_column(String(160), default='')
    year: Mapped[str] = mapped_column(String(20), default='')
    condition: Mapped[str] = mapped_column(String(30), default='used')
    mileage_km: Mapped[str] = mapped_column(String(40), default='')
    engine: Mapped[str] = mapped_column(String(100), default='')
    transmission: Mapped[str] = mapped_column(String(100), default='')
    drivetrain: Mapped[str] = mapped_column(String(100), default='')
    color: Mapped[str] = mapped_column(String(100), default='')
    paint_condition: Mapped[str] = mapped_column(String(200), default='')
    purchase_price_cny: Mapped[str] = mapped_column(String(60), default='')
    sale_price: Mapped[str] = mapped_column(String(80), default='')
    highlights: Mapped[str] = mapped_column(Text, default='')
    notes: Mapped[str] = mapped_column(Text, default='')
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LeadMessage(Base):
    __tablename__ = 'lead_messages'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    direction: Mapped[str] = mapped_column(String(10), default='in')
    text: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


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
        # Lightweight SQLite migrations for existing persistent Railway volume.
        if self.engine.dialect.name == 'sqlite':
            from sqlalchemy import text
            migrations = {
                'vehicles': {
                    'photo_file_ids': "TEXT DEFAULT ''", 'publish_at': 'DATETIME',
                    'repeat_rule': "VARCHAR(20) DEFAULT 'once'", 'auto_publish': 'BOOLEAN DEFAULT 0'
                },
                'leads': {
                    'first_name': "VARCHAR(200) DEFAULT ''", 'language_code': "VARCHAR(30) DEFAULT ''",
                    'vehicle_code': "VARCHAR(40) DEFAULT ''", 'last_message': "TEXT DEFAULT ''",
                    'status': "VARCHAR(30) DEFAULT 'new'", 'budget': "VARCHAR(100) DEFAULT ''",
                    'city': "VARCHAR(150) DEFAULT ''", 'vehicle_preference': "VARCHAR(30) DEFAULT ''",
                    'purchase_timing': "VARCHAR(100) DEFAULT ''", 'manager_note': "TEXT DEFAULT ''",
                    'next_follow_up': 'DATETIME', 'created_at': 'DATETIME'
                }
            }
            with self.engine.begin() as conn:
                for table, cols in migrations.items():
                    existing = {r[1] for r in conn.execute(text(f'PRAGMA table_info({table})'))}
                    for name, ddl in cols.items():
                        if name not in existing:
                            conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))
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

    def update_vehicle(self, code, facts=None, caption=None, photo_file_id=None, photo_file_ids=None, publish_at=None, repeat_rule=None, auto_publish=None):
        with self.session.begin() as s:
            row = s.scalar(select(Vehicle).where(Vehicle.code == code.upper()))
            if not row:
                return False
            if facts is not None: row.facts = facts[:4000]
            if caption is not None: row.caption = caption[:4000]
            if photo_file_id is not None: row.photo_file_id = photo_file_id
            if photo_file_ids is not None: row.photo_file_ids = photo_file_ids
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

    def vehicle_detail(self, code):
        with self.session() as s:
            return s.get(VehicleDetail, code.upper())

    def save_vehicle_detail(self, code, **values):
        allowed = {'brand','model','year','condition','mileage_km','engine','transmission','drivetrain','color',
                   'paint_condition','purchase_price_cny','sale_price','highlights','notes'}
        with self.session.begin() as s:
            row = s.get(VehicleDetail, code.upper())
            if row is None:
                row = VehicleDetail(vehicle_code=code.upper())
                s.add(row)
            for key, value in values.items():
                if key in allowed:
                    setattr(row, key, (value or '')[:4000] if key in ('highlights','notes') else (value or '')[:200])
            row.updated_at = now()
            return True

    def duplicate_vehicle(self, code):
        with self.session.begin() as s:
            src = s.scalar(select(Vehicle).where(Vehicle.code == code.upper()))
            if not src:
                return None
            row = Vehicle(facts=src.facts, caption=src.caption, photo_file_id=src.photo_file_id,
                          photo_file_ids=src.photo_file_ids, status='available', repeat_rule='once',
                          auto_publish=False)
            s.add(row); s.flush(); row.code = f'FF-{row.id:05d}'
            return row.code

    def set_vehicle_photos(self, code, photos):
        with self.session.begin() as s:
            row = s.scalar(select(Vehicle).where(Vehicle.code == code.upper()))
            if not row: return False
            row.photo_file_ids = '|'.join(photos)
            row.photo_file_id = photos[0] if photos else ''
            row.updated_at = now()
            return True

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

    def record_vehicle_publication(self, code, chat_id, message_id, caption, mode='manual', status='sent'):
        with self.session.begin() as s:
            s.add(VehiclePublication(vehicle_code=code, chat_id=str(chat_id), message_id=message_id,
                                     caption=caption[:4000], mode=mode, status=status))

    def vehicle_publications(self, code, limit=30):
        with self.session() as s:
            return s.scalars(select(VehiclePublication).where(
                VehiclePublication.vehicle_code == code.upper()
            ).order_by(VehiclePublication.id.desc()).limit(limit)).all()

    def upcoming_vehicles(self, limit=20):
        with self.session() as s:
            return s.scalars(select(Vehicle).where(
                Vehicle.auto_publish.is_(True), Vehicle.status == 'available', Vehicle.publish_at.is_not(None)
            ).order_by(Vehicle.publish_at).limit(limit)).all()

    def failed_vehicle_publications(self, limit=20):
        with self.session() as s:
            return s.scalars(select(VehiclePublication).where(
                VehiclePublication.status != 'sent'
            ).order_by(VehiclePublication.id.desc()).limit(limit)).all()

    def upsert_lead(self, telegram_user_id, username='', first_name='', language_code='', vehicle_code='', last_message=''):
        with self.session.begin() as s:
            row = s.scalar(select(Lead).where(Lead.user_id == telegram_user_id))
            if row is None:
                row = Lead(user_id=telegram_user_id)
                s.add(row)
            row.username = username or row.username
            row.first_name = first_name or row.first_name
            row.language_code = language_code or row.language_code
            row.vehicle_code = vehicle_code or row.vehicle_code
            row.last_message = (last_message or '')[:4000]
            text = (last_message or '').lower()
            hot = any(x in text for x in ['купить','цена','стоимость','заказать','оплата','доставка','buy','price','购买','价格','付款','运输'])
            row.grade = 'A' if vehicle_code and hot else ('B' if hot or vehicle_code else 'C')
            row.updated_at = now()
            return row.grade

    def record_lead_message(self, telegram_user_id, direction, text):
        with self.session.begin() as s:
            s.add(LeadMessage(telegram_user_id=telegram_user_id, direction=direction, text=(text or '')[:4000]))

    def lead_messages(self, telegram_user_id, limit=100):
        with self.session() as s:
            rows = s.scalars(select(LeadMessage).where(
                LeadMessage.telegram_user_id == telegram_user_id
            ).order_by(LeadMessage.id.desc()).limit(limit)).all()
            return list(reversed(rows))

    def lead_funnel(self):
        with self.session() as s:
            rows = s.scalars(select(Lead)).all()
            stages = {k: 0 for k in ('new','contacted','negotiating','won','lost')}
            grades = {k: 0 for k in ('A+','A','B','C')}
            for x in rows:
                stages[x.status] = stages.get(x.status, 0) + 1
                grades[x.grade] = grades.get(x.grade, 0) + 1
            return {'total': len(rows), 'stages': stages, 'grades': grades}

    def lead(self, lead_id):
        with self.session() as s:
            return s.get(Lead, int(lead_id))

    def update_lead(self, lead_id, grade=None, status=None, budget=None, city=None, vehicle_preference=None,
                    purchase_timing=None, manager_note=None, next_follow_up=None):
        with self.session.begin() as s:
            row = s.get(Lead, int(lead_id))
            if not row: return False
            if grade in ('A+','A','B','C'): row.grade = grade
            if status in ('new','contacted','negotiating','won','lost'): row.status = status
            for k,v in [('budget',budget),('city',city),('vehicle_preference',vehicle_preference),
                        ('purchase_timing',purchase_timing),('manager_note',manager_note)]:
                if v is not None: setattr(row,k,(v or '')[:4000] if k=='manager_note' else (v or '')[:200])
            if next_follow_up is not None: row.next_follow_up = next_follow_up
            row.updated_at = now()
            return True

    def recalculate_lead_grade(self, lead_id):
        with self.session.begin() as s:
            row = s.get(Lead, int(lead_id))
            if not row: return None
            score = 0
            if row.vehicle_code: score += 2
            if row.budget: score += 2
            if row.city: score += 1
            if row.purchase_timing: score += 2
            if row.vehicle_preference: score += 1
            text = (row.last_message or '').lower()
            if any(k in text for k in ['оплата','готов купить','оформить','счет','счёт','договор','payment','invoice','付款','合同']): score += 3
            elif any(k in text for k in ['цена','стоимость','доставка','price','delivery','价格','运输']): score += 2
            if row.status == 'negotiating': score += 2
            if row.status == 'won': score += 4
            row.grade = 'A+' if score >= 8 else ('A' if score >= 5 else ('B' if score >= 2 else 'C'))
            row.updated_at = now()
            return row.grade

    def match_vehicles_for_lead(self, lead_id, limit=5):
        lead = self.lead(lead_id)
        if not lead: return []
        with self.session() as s:
            vehicles = s.scalars(select(Vehicle).where(Vehicle.status == 'available').order_by(Vehicle.updated_at.desc())).all()
            details = {d.vehicle_code: d for d in s.scalars(select(VehicleDetail)).all()}
        pref = (lead.vehicle_preference or '').lower()
        budget_text = (lead.budget or '').lower()
        city_text = (lead.city or '').lower()
        scored = []
        for v in vehicles:
            d = details.get(v.code)
            hay = ' '.join(filter(None, [v.facts, getattr(d,'brand',''), getattr(d,'model',''),
                                         getattr(d,'highlights',''), getattr(d,'condition','')])).lower()
            score, reasons = 0, []
            if lead.vehicle_code and v.code == lead.vehicle_code:
                score += 10; reasons.append('客户指定车辆')
            if pref and d and d.condition and pref in (d.condition or '').lower():
                score += 3; reasons.append('新车/二手偏好匹配')
            tokens = [t for t in (lead.last_message or '').lower().replace(',', ' ').split() if len(t) >= 3]
            hits = sum(1 for t in set(tokens) if t in hay)
            if hits:
                score += min(hits, 4); reasons.append('车型/需求关键词匹配')
            if budget_text and d and d.sale_price and any(x in str(d.sale_price).lower() for x in budget_text.split()):
                score += 1; reasons.append('价格信息可供核对')
            if score or not lead.vehicle_code:
                scored.append((score, v, d, reasons))
        scored.sort(key=lambda x: (x[0], x[1].updated_at), reverse=True)
        return scored[:limit]

    def intervention_leads(self, limit=30):
        with self.session() as s:
            return s.scalars(select(Lead).where(
                Lead.grade.in_(('A+','A')), Lead.status.not_in(('won','lost'))
            ).order_by(Lead.grade.asc(), Lead.updated_at.desc()).limit(limit)).all()

    def due_followups(self, at=None, limit=50):
        at = at or now()
        with self.session() as s:
            return s.scalars(select(Lead).where(Lead.next_follow_up.is_not(None), Lead.next_follow_up <= at,
                Lead.status.not_in(('won','lost')), Lead.grade.in_(('A+','A','B','C'))).order_by(Lead.next_follow_up).limit(limit)).all()

    def leads(self, limit=100):
        with self.session() as s:
            return s.scalars(select(Lead).order_by(Lead.updated_at.desc()).limit(limit)).all()

    def garage_stats(self):
        with self.session() as s:
            vehicles = s.scalars(select(Vehicle)).all()
            pubs = s.scalars(select(VehiclePublication)).all()
            today = now().date()
            return {
                'total': len(vehicles),
                'available': sum(v.status == 'available' for v in vehicles),
                'reserved': sum(v.status == 'reserved' for v in vehicles),
                'sold': sum(v.status == 'sold' for v in vehicles),
                'auto': sum(v.auto_publish for v in vehicles),
                'due': sum(bool(v.auto_publish and v.status == 'available' and v.publish_at and v.publish_at <= now()) for v in vehicles),
                'sent_today': sum(p.status == 'sent' and p.created_at.date() == today for p in pubs),
                'recent': sorted(pubs, key=lambda p: p.id, reverse=True)[:12],
            }

    def stats(self):
        with self.session() as s:
            leads = s.scalars(select(Lead)).all()
            posts = s.scalars(select(Publication)).all()
            return {'leads': {g: sum(x.grade == g for x in leads) for g in ['A+', 'A', 'B', 'C', 'D']},
                    'sent': sum(x.status == 'sent' for x in posts),
                    'uncertain': sum(x.status in ('pending', 'uncertain') for x in posts)}

