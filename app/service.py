import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.exc import IntegrityError
from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from .content import ContentGenerator
from .database import CommandEvent, Lead, Publication
from .scoring import classify

log = logging.getLogger(__name__)
COMMANDS = {'start': 'Начать', 'help': 'Помощь', 'today': 'План на сегодня', 'stock': 'Наличие',
            'price': 'Стоимость', 'post': 'Публикация (админ)', 'setchat': 'Канал (админ)',
            'pause': 'Пауза (админ)', 'resume': 'Продолжить (админ)', 'stats': 'Статистика (админ)', 'car': 'Авто с фото (админ)'}
ADMIN_COMMANDS = {'today', 'post', 'setchat', 'pause', 'resume', 'stats', 'car'}


class BotService:
    def __init__(self, settings, db):
        self.settings, self.db = settings, db
        self.content = ContentGenerator(settings, db)
        self.scheduler = AsyncIOScheduler(timezone=settings.timezone)
        self.application = None
        self.lock = asyncio.Lock()
        self.polling_ok = False
        self.error_code = None

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
        self.scheduler.start()

    async def poll(self):
        offset = int(self.db.get('telegram_offset') or '0')
        while True:
            try:
                updates = await self.application.bot.get_updates(offset=offset, timeout=25, read_timeout=35,
                                                                  allowed_updates=['message'])
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
        log.info('Command received: /%s; admin=%s; chat_type=%s', name, admin, update.effective_chat.type)
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
            if not msg.reply_to_message or not msg.reply_to_message.photo:
                await msg.reply_text('Отправьте фото автомобиля, затем ответьте на него командой:\n/car Audi Q3 | 2022 | 40000 км | родная краска')
                return
            facts = ' '.join(args).strip()
            if not facts:
                await msg.reply_text('После /car укажите модель, год, пробег и состояние.')
                return
            target = self.db.get('target_chat')
            if not target:
                await msg.reply_text('Сначала задайте канал: /setchat @channel')
                return
            await msg.reply_text('Фото и данные получены. Готовлю публикацию…')
            caption = await self.content.sales_listing(facts)
            photo_id = msg.reply_to_message.photo[-1].file_id
            try:
                sent = await context.bot.send_photo(chat_id=target, photo=photo_id, caption=caption[:1024])
                await msg.reply_text(f'Опубликовано фото + объявление. message_id={sent.message_id}')
                log.info('Vehicle photo post sent: chat_id=%s; message_id=%s', target, sent.message_id)
            except Exception as exc:
                log.exception('Vehicle photo post failed')
                await msg.reply_text('Не удалось опубликовать в канал. Проверьте /setchat и права бота в канале. Ошибка: ' + type(exc).__name__)
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
        stock = self.db.catalog('stock', self.settings.stock_text)
        prices = self.db.catalog('price', self.settings.price_text)
        reply = await self.content.sales_reply(msg.text, stock, prices)
        sent = await context.bot.send_message(chat_id=update.effective_chat.id, text=reply)
        log.info('AI sales reply sent: chat_id=%s; admin=%s; message_id=%s', update.effective_chat.id, admin, sent.message_id)
