import asyncio
import logging
import re
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.exc import IntegrityError
from telegram import BotCommand, Update, InputMediaPhoto
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from .content import ContentGenerator
from .database import CommandEvent, Lead, Publication
from .scoring import classify

log = logging.getLogger(__name__)
COMMANDS = {'start': 'Начать', 'help': 'Помощь', 'today': 'План на сегодня', 'stock': 'Наличие',
            'price': 'Стоимость', 'post': 'Публикация (админ)', 'setchat': 'Канал (админ)',
            'pause': 'Пауза (админ)', 'resume': 'Продолжить (админ)', 'stats': 'Статистика (админ)', 'car': 'Авто с фото (админ)', 'edit': 'Изменить текст (админ)', 'publish': 'Опубликовать черновик (админ)', 'inventory': 'Склад (админ)', 'sold': 'Продано (админ)'}
ADMIN_COMMANDS = {'today', 'post', 'setchat', 'pause', 'resume', 'stats', 'car', 'edit', 'publish', 'inventory', 'sold'}


class BotService:
    def __init__(self, settings, db):
        self.settings, self.db = settings, db
        self.content = ContentGenerator(settings, db)
        self.scheduler = AsyncIOScheduler(timezone=settings.timezone)
        self.application = None
        self.lock = asyncio.Lock()
        self.polling_ok = False
        self.error_code = None
        self.car_drafts = {}

    def build(self):
        self.application = Application.builder().token(self.settings.bot_token).updater(None).build()
        # A dedicated polling task makes liveness and shutdown explicit.
        for name in COMMANDS:
            self.application.add_handler(CommandHandler(name, self.command))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, self.message))
        self.application.add_error_handler(self.on_error)
        return self.application

    async def on_error(self, update, context):
        log.error('Telegram handler failed: %s', type(context.error).__name__)

    async def start(self):
        self.build()
        await self.application.initialize()
        log.info('Connected Telegram bot: @%s; admin_count=%d', self.application.bot.username, len(self.settings.admin_ids))
        webhook = await self.application.bot.get_webhook_info()
        if webhook.url:
            raise RuntimeError('Existing webhook must be removed before enabling polling')
        await self.application.bot.set_my_commands([BotCommand(k, v) for k, v in COMMANDS.items()])
        await self.application.start()
        self.poll_task = asyncio.create_task(self.poll())
        for hour in (9, 13, 18, 21):
            self.scheduler.add_job(self.scheduled, CronTrigger(hour=hour, minute=0, timezone=self.settings.timezone),
                                   args=[hour], id=f'post-{hour}', max_instances=1, coalesce=True, misfire_grace_time=300)
        self.scheduler.add_job(self.publish_due_vehicles, 'interval', minutes=1, id='vehicle-due', max_instances=1, coalesce=True)
        self.scheduler.add_job(self.followup_reminders, 'interval', minutes=30, id='lead-followups', max_instances=1, coalesce=True)
        self.scheduler.start()

    async def poll(self):
        offset = int(self.db.get('telegram_offset') or '0')
        while True:
            try:
                updates = await self.application.bot.get_updates(offset=offset, timeout=25, read_timeout=35,
                                                                  allowed_updates=['message', 'channel_post'])
                self.polling_ok, self.error_code = True, None
                if updates:
                    log.info('Telegram received %d update(s)', len(updates))
                for update in updates:
                    await self.application.process_update(update)
                    offset = update.update_id + 1
                    self.db.set('telegram_offset', str(offset))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.polling_ok, self.error_code = False, type(exc).__name__
                log.warning('Polling failed: %s', self.error_code)
                await asyncio.sleep(5)

    async def stop(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        if hasattr(self, 'poll_task'):
            self.poll_task.cancel()
            try:
                await self.poll_task
            except asyncio.CancelledError:
                pass
        if self.application:
            if self.application.running:
                await self.application.stop()
            await self.application.shutdown()
        await self.content.close()

    async def publish_due_vehicles(self):
        if self.db.get('paused') != 'false':
            return
        target = self.db.get('target_chat')
        if not target:
            return
        for vehicle in self.db.due_vehicles():
            try:
                caption = vehicle.caption.strip() if vehicle.caption else ''
                if not caption:
                    caption = await self.content.sales_listing(vehicle.facts)
                photos = [p for p in (vehicle.photo_file_ids or '').split('|') if p] or ([vehicle.photo_file_id] if vehicle.photo_file_id else [])
                def media_value(p):
                    return Path('/app/data/garage_media') / p.removeprefix('local:') if p.startswith('local:') else p
                if len(photos) > 1:
                    media = [InputMediaPhoto(media=media_value(p), caption=caption[:1024] if i == 0 else None) for i, p in enumerate(photos[:10])]
                    sent_group = await self.application.bot.send_media_group(chat_id=target, media=media)
                    sent = sent_group[0]
                elif photos:
                    sent = await self.application.bot.send_photo(chat_id=target, photo=media_value(photos[0]), caption=caption[:1024])
                else:
                    sent = await self.application.bot.send_message(chat_id=target, text=caption[:4096])
                self.db.mark_vehicle_published(vehicle.code, sent.message_id)
                self.db.record_vehicle_publication(vehicle.code, target, sent.message_id, caption, mode='scheduled')
                self.db.advance_vehicle_schedule(vehicle.code)
                log.info('Scheduled vehicle published: code=%s; chat_id=%s; message_id=%s', vehicle.code, target, sent.message_id)
            except Exception as exc:
                self.db.record_vehicle_publication(vehicle.code, target, None, vehicle.caption or vehicle.facts, mode='scheduled', status='failed:' + type(exc).__name__)
                log.exception('Scheduled vehicle publish failed: code=%s; error=%s', vehicle.code, type(exc).__name__)

    async def followup_reminders(self):
        leads = self.db.due_followups()
        if not leads or not self.settings.admin_ids:
            return
        for lead in leads:
            key = 'followup_alert:' + str(lead.id) + ':' + (lead.next_follow_up.isoformat() if lead.next_follow_up else '')
            if self.db.get(key) == 'sent':
                continue
            vehicle = (' | ' + lead.vehicle_code) if lead.vehicle_code else ''
            note = (lead.manager_note or lead.last_message or '')[:500]
            text = f'⏰ CRM 跟进提醒 | {lead.grade}{vehicle}\n客户: @{lead.username or "-"} | {lead.first_name or "-"}\n预算: {lead.budget or "-"} | 城市: {lead.city or "-"}\n{note}'
            delivered = False
            for admin_id in self.settings.admin_ids:
                try:
                    await self.application.bot.send_message(chat_id=admin_id, text=text)
                    delivered = True
                except Exception as exc:
                    log.warning('Follow-up reminder failed: %s', type(exc).__name__)
            if delivered:
                self.db.set(key, 'sent')

    async def scheduled(self, hour):
        date = datetime.now(ZoneInfo(self.settings.timezone)).date().isoformat()
        await self.publish(f'schedule:{date}:{hour}', str(hour), scheduled=True)

    async def publish(self, key, slot='manual', scheduled=False):
        async with self.lock:
            target = self.db.get('target_chat')
            if self.db.get('paused') != 'false':
                return 'Публикации на паузе. /resume'
            if not target:
                return 'Сначала задайте канал: /setchat @channel'
            with self.db.session.begin() as s:
                if s.get(Publication, key):
                    return 'Эта публикация уже обработана. Проверьте /stats.'
                s.add(Publication(key=key, chat_id=target, status='pending'))
                try:
                    s.flush()
                except IntegrityError:
                    return 'Публикация уже обрабатывается.'
            text = await self.content.generate(slot)
            # Recheck pause after slow generation. /pause serializes with sends via lock.
            if self.db.get('paused') != 'false':
                with self.db.session.begin() as s:
                    s.get(Publication, key).status = 'cancelled'
                return 'Публикации на паузе.'
            try:
                sent = await self.application.bot.send_message(chat_id=target, text=text)
                with self.db.session.begin() as s:
                    row = s.get(Publication, key)
                    row.status, row.text, row.message_id = 'sent', text, sent.message_id
                return 'Опубликовано.'
            except Exception as exc:
                # Telegram has no idempotency key. Never automatically retry ambiguous sends.
                with self.db.session.begin() as s:
                    row = s.get(Publication, key)
                    row.status, row.text = 'uncertain', text
                log.warning('Publication uncertain: %s', type(exc).__name__)
                return 'Не удалось подтвердить отправку. Проверьте канал перед повторной публикацией.'

    async def command(self, update: Update, context):
        msg, user = update.effective_message, update.effective_user
        if not msg or not user or not msg.text:
            return
        name = msg.text.split()[0].split('@')[0][1:].lower()
        args = context.args or []
        admin = user.id in self.settings.admin_ids
        log.info('Command received: /%s; admin=%s; chat_type=%s; reply_has_photo=%s; args_count=%d', name, admin, update.effective_chat.type, bool(msg.reply_to_message and msg.reply_to_message.photo), len(args))
        if name in ADMIN_COMMANDS and (not admin or update.effective_chat.type != 'private'):
            await msg.reply_text('Команда доступна администратору в личном чате.')
            log.info('Command denied: /%s', name)
            return
        with self.db.session.begin() as s:
            s.add(CommandEvent(command=name))
        if name == 'start':
            with self.db.session.begin() as s:
                lead = s.get(Lead, user.id)
                if lead:
                    lead.opted_out = False
            sent = await context.bot.send_message(chat_id=update.effective_chat.id, text='Добро пожаловать в FULIFENG AUTO!\n\n🚗 Новый автомобиль\n🚙 Б/У автомобиль\n🔥 Подбор по бюджету\n📩 Связаться с менеджером\n\nУкажите модель, бюджет и город доставки.')
            log.info('Start reply sent: chat_id=%s; message_id=%s', update.effective_chat.id, sent.message_id)
        elif name == 'help':
            await msg.reply_text('/start /help /stock /price\nАдминистратор: /today /post [send] /setchat @channel /pause /resume /stats\nВаш Telegram ID: ' + str(user.id))
        elif name in ('stock', 'price'):
            if args:
                if not admin or update.effective_chat.type != 'private' or args[0] != 'set':
                    await msg.reply_text('Изменение: администратор в личном чате /' + name + ' set ТЕКСТ')
                    return
                value = ' '.join(args[1:]).strip()
                if not value or len(value) > 1500:
                    await msg.reply_text('Укажите от 1 до 1500 символов.')
                    return
                self.db.set(name, value)
            await msg.reply_text(self.db.catalog(name, getattr(self.settings, name + '_text'))[:3900])
        elif name == 'today':
            await msg.reply_text(f'09:00, 13:00, 18:00, 21:00 ({self.settings.timezone})\nПауза: {self.db.get("paused")}\nКанал: {self.db.get("target_chat") or "не задан"}')
        elif name == 'setchat':
            if len(args) != 1:
                await msg.reply_text('/setchat @channel или -100... (бот должен быть администратором)')
                return
            try:
                chat = await context.bot.get_chat(args[0])
                if chat.type not in ('channel', 'supergroup', 'group'):
                    raise ValueError('Only channels or groups')
                member = await context.bot.get_chat_member(chat.id, context.bot.id)
                if member.status not in ('administrator', 'creator') or (chat.type == 'channel' and not getattr(member, 'can_post_messages', False)):
                    raise ValueError('Missing posting rights')
                async with self.lock:
                    self.db.set('target_chat', str(chat.id))
                await msg.reply_text('Канал сохранён. Для запуска: /resume')
            except Exception as exc:
                log.warning('Target validation failed: %s', type(exc).__name__)
                await msg.reply_text('Не удалось проверить канал и права бота. Добавьте бота администратором.')
        elif name == 'pause':
            async with self.lock:
                self.db.set('paused', 'true')
            await msg.reply_text('Публикации остановлены.')
        elif name == 'resume':
            if not self.db.get('target_chat'):
                await msg.reply_text('Сначала /setchat @channel')
                return
            async with self.lock:
                self.db.set('paused', 'false')
            await msg.reply_text('Расписание включено.')
        elif name == 'post':
            if args == ['send']:
                await msg.reply_text(await self.publish(f'manual:{update.update_id}'))
            else:
                await msg.reply_text(await self.content.generate())
                await msg.reply_text('Это предварительный просмотр. Опубликовать: /post send')
        elif name == 'car':
            facts = ' '.join(args).strip()
            photo_source = msg.reply_to_message if msg.reply_to_message and msg.reply_to_message.photo else None
            if not photo_source:
                await msg.reply_text('Фото не найдено. Ответьте командой /car на сообщение с фото.')
                return
            if not facts:
                await msg.reply_text('После /car укажите модель, год, пробег и состояние.')
                return
            await msg.reply_text('Фото и данные получены. Готовлю текст…')
            caption = await self.content.sales_listing(facts)
            code = self.db.create_vehicle(facts, caption, photo_source.photo[-1].file_id)
            self.car_drafts[user.id] = {'code': code, 'photo_id': photo_source.photo[-1].file_id, 'caption': caption, 'facts': facts}
            await msg.reply_text('ID: ' + code + '\nПРЕДПРОСМОТР:\n\n' + caption[:3500] + '\n\nИзменить: /edit новый текст\nОпубликовать: /publish')
        elif name == 'edit':
            draft = self.car_drafts.get(user.id)
            text = ' '.join(args).strip()
            if not draft:
                await msg.reply_text('Сначала создайте черновик через /car.')
                return
            if not text:
                await msg.reply_text('/edit новый текст объявления')
                return
            draft['caption'] = text[:1024]
            await msg.reply_text('Текст обновлён:\n\n' + draft['caption'] + '\n\nДля публикации: /publish')
        elif name == 'publish':
            draft = self.car_drafts.get(user.id)
            if not draft:
                await msg.reply_text('Нет черновика. Сначала /car.')
                return
            target = self.db.get('target_chat')
            if not target:
                await msg.reply_text('Сначала задайте канал: /setchat @channel')
                return
            try:
                sent = await context.bot.send_photo(chat_id=target, photo=draft['photo_id'], caption=draft['caption'][:1024])
                self.db.mark_vehicle_published(draft['code'], sent.message_id)
                self.car_drafts.pop(user.id, None)
                await msg.reply_text(f'Опубликовано. message_id={sent.message_id}')
                log.info('Vehicle draft published: chat_id=%s; message_id=%s', target, sent.message_id)
            except Exception as exc:
                log.exception('Vehicle draft publish failed')
                await msg.reply_text('Ошибка публикации: ' + type(exc).__name__)
        elif name == 'inventory':
            rows = self.db.vehicles()
            if not rows:
                await msg.reply_text('Склад пуст.')
            else:
                await msg.reply_text('\n'.join(f'{x.code} | {x.status} | {x.facts[:120]}' for x in rows)[:3900])
        elif name == 'sold':
            if len(args) != 1:
                await msg.reply_text('/sold FF-00001')
                return
            if self.db.update_vehicle_status(args[0], 'sold'):
                await msg.reply_text(args[0].upper() + ' отмечен как проданный. Автопродвижение для него отключено.')
            else:
                await msg.reply_text('Автомобиль не найден.')
        elif name == 'stats':
            stats = self.db.stats()
            await msg.reply_text('Лиды: ' + ', '.join(f'{k}: {v}' for k, v in stats['leads'].items()) +
                                 f'\nПубликаций: {stats["sent"]}\nТребуют проверки: {stats["uncertain"]}\nD: только запись, без исходящих сообщений.')
        log.info('Command completed: /%s', name)

    async def message(self, update, context):
        user, msg = update.effective_user, update.effective_message
        if not user or user.is_bot or not msg:
            return
        admin = user.id in self.settings.admin_ids
        if not admin:
            score, grade, reasons, optout = classify(msg.text)
            grade = self.db.record_lead(user.id, user.username, msg.text, score, grade, reasons, optout)
            # D is recorded only: no reply, notification or proactive outreach.
            if grade == 'D':
                return
            match = re.search(r'\\bFF[- ]?(\\d{1,6})\\b', msg.text or '', re.I)
            vehicle_code = ('FF-' + match.group(1).zfill(5)) if match else ''
            crm_grade = self.db.upsert_lead(user.id, user.username or '', user.first_name or '',
                                            user.language_code or '', vehicle_code, msg.text or '')
            if crm_grade == 'A' and self.settings.admin_ids:
                vehicle_note = (' | ' + vehicle_code) if vehicle_code else ''
                alert = f'🔥 Новый лид {crm_grade}{vehicle_note}\\n@{user.username or "-"} | ID {user.id}\\n{(msg.text or "")[:700]}'
                for admin_id in self.settings.admin_ids:
                    try:
                        await context.bot.send_message(chat_id=admin_id, text=alert)
                    except Exception as exc:
                        log.warning('Lead alert failed: %s', type(exc).__name__)
        stock = self.db.catalog('stock', self.settings.stock_text)
        prices = self.db.catalog('price', self.settings.price_text)
        if not admin and vehicle_code:
            vehicle = self.db.vehicle(vehicle_code)
            if vehicle and vehicle.status == 'available':
                stock = vehicle_code + ': ' + vehicle.facts + '\\n' + stock
        reply = await self.content.sales_reply(msg.text, stock, prices)
        sent = await context.bot.send_message(chat_id=update.effective_chat.id, text=reply)
        log.info('AI sales reply sent: chat_id=%s; admin=%s; message_id=%s', update.effective_chat.id, admin, sent.message_id)
