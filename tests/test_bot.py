from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.database import Database, Lead
from app.main import create_app
from app.scoring import classify
from app.service import BotService, COMMANDS

@pytest.fixture
def setup(tmp_path):
    settings = Settings(bot_token='', openai_api_key='', admin_ids={123}, database_url=f'sqlite:///{tmp_path}/test.db')
    db = Database(settings.database_url)
    db.initialize()
    service = BotService(settings, db)
    service.application = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42))))
    yield settings, db, service
    db.engine.dispose()

def update(command, user_id=123, update_id=1, chat_type='private'):
    return SimpleNamespace(update_id=update_id, effective_user=SimpleNamespace(id=user_id, username='test', is_bot=False), effective_chat=SimpleNamespace(type=chat_type), effective_message=SimpleNamespace(text=command, reply_text=AsyncMock()))

def context(args=None):
    return SimpleNamespace(args=args or [], bot=SimpleNamespace(id=111, get_chat=AsyncMock(return_value=SimpleNamespace(id=-100123, type='channel')), get_chat_member=AsyncMock(return_value=SimpleNamespace(status='administrator', can_post_messages=True))))

@pytest.mark.parametrize('text,grade', [('hello','D'), ('доставка','C'), ('куплю авто','B'), ('куплю авто бюджет','A'), ('куплю авто бюджет сегодня доставка','A+'), ('куплю авто не пишите','D')])
def test_scoring(text, grade):
    assert classify(text)[1] == grade

@pytest.mark.asyncio
async def test_d_recorded_without_reply(setup):
    _, db, service = setup
    u = update('hello', user_id=999)
    await service.message(u, context())
    u.effective_message.reply_text.assert_not_called()
    service.application.bot.send_message.assert_not_called()
    with db.session() as s:
        assert s.get(Lead, 999).grade == 'D'

@pytest.mark.asyncio
async def test_optout_sticky_until_start(setup):
    _, db, service = setup
    await service.message(update('не пишите', user_id=999), context())
    u = update('куплю авто бюджет сегодня доставка', user_id=999)
    await service.message(u, context())
    u.effective_message.reply_text.assert_not_called()
    await service.command(update('/start', user_id=999), context())
    await service.message(u, context())
    u.effective_message.reply_text.assert_awaited_once()

@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['today','post','setchat','pause','resume','stats'])
async def test_admin_gate(setup, name):
    _, db, service = setup
    u = update('/'+name, user_id=999)
    await service.command(u, context(['send']))
    assert 'администратору' in u.effective_message.reply_text.call_args.args[0]
    assert db.get('paused') == 'true'
    service.application.bot.send_message.assert_not_called()

@pytest.mark.asyncio
async def test_pause_target_resume_publish_idempotency(setup):
    _, db, service = setup
    assert 'паузе' in await service.publish('a')
    await service.command(update('/resume'), context())
    assert db.get('paused') == 'true'
    await service.command(update('/setchat @test'), context(['@test']))
    await service.command(update('/resume'), context())
    assert db.get('paused') == 'false'
    assert await service.publish('a') == 'Опубликовано.'
    await service.publish('a')
    service.application.bot.send_message.assert_awaited_once()
    await service.command(update('/pause'), context())
    await service.publish('b')
    service.application.bot.send_message.assert_awaited_once()
    assert db.stats()['sent'] == 1

@pytest.mark.asyncio
async def test_uncertain_send_never_retried(setup):
    _, db, service = setup
    db.set('paused','false')
    db.set('target_chat','-100123')
    service.application.bot.send_message.side_effect = TimeoutError()
    await service.publish('uncertain')
    await service.publish('uncertain')
    service.application.bot.send_message.assert_awaited_once()
    assert db.stats()['uncertain'] == 1

@pytest.mark.asyncio
async def test_fallback_no_key_and_api_failure(setup):
    _, db, service = setup
    assert 'FULIFENG AUTO' in await service.content.generate()
    service.content.client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(side_effect=RuntimeError())))
    assert 'FULIFENG AUTO' in await service.content.generate()

@pytest.mark.asyncio
@pytest.mark.parametrize('name', list(COMMANDS))
async def test_all_commands_respond(setup, name):
    _, _, service = setup
    u = update('/'+name)
    await service.command(u, context())
    assert u.effective_message.reply_text.await_count >= 1

@pytest.mark.asyncio
async def test_catalog_changes_persist(setup):
    _, db, service = setup
    await service.command(update('/stock set Проверено'), context(['set','Проверено']))
    db.initialize()
    assert db.get('stock') == 'Проверено'

def test_health_bootstrap_explicitly_not_ready(tmp_path):
    settings = Settings(bot_token='', openai_api_key='', admin_ids=set(), database_url=f'sqlite:///{tmp_path}/health.db')
    with TestClient(create_app(settings)) as client:
        response = client.get('/health')
        assert response.status_code == 200
        assert response.json()['ready'] is False
        assert response.json()['bot'] == 'not_configured'

@pytest.mark.asyncio
async def test_schedule_moscow(setup):
    from apscheduler.triggers.cron import CronTrigger
    from datetime import datetime
    from zoneinfo import ZoneInfo
    settings, _, _ = setup
    for hour in (9,13,18,21):
        trigger = CronTrigger(hour=hour, minute=0, timezone=settings.timezone)
        fire = trigger.get_next_fire_time(None, datetime(2026,9,19,0,tzinfo=ZoneInfo(settings.timezone)))
        assert fire.hour == hour and fire.minute == 0
        assert fire.utcoffset().total_seconds() == 10800

@pytest.mark.asyncio
async def test_admin_commands_rejected_in_group(setup):
    _, db, service = setup
    u = update('/resume', chat_type='group')
    await service.command(u, context())
    assert 'администратору' in u.effective_message.reply_text.call_args.args[0]
    assert db.get('paused') == 'true'

@pytest.mark.asyncio
async def test_setchat_rejects_missing_permissions(setup):
    _, db, service = setup
    ctx = context(['@test'])
    ctx.bot.get_chat_member.return_value = SimpleNamespace(status='member')
    await service.command(update('/setchat @test'), ctx)
    assert db.get('target_chat') == ''

@pytest.mark.asyncio
async def test_duplicate_survives_service_restart(setup):
    settings, db, service = setup
    db.set('target_chat', '-100123')
    db.set('paused', 'false')
    await service.scheduled(9)
    restarted = BotService(settings, db)
    restarted.application = service.application
    await restarted.scheduled(9)
    service.application.bot.send_message.assert_awaited_once()

def test_railway_without_volume_does_not_start_bot(tmp_path, monkeypatch):
    monkeypatch.setenv('RAILWAY_SERVICE_ID', 'test')
    monkeypatch.delenv('RAILWAY_VOLUME_MOUNT_PATH', raising=False)
    settings = Settings(bot_token='', openai_api_key='', admin_ids=set(), database_url=f'sqlite:///{tmp_path}/health.db')
    with TestClient(create_app(settings)) as client:
        assert client.get('/health').json()['bot'] == 'needs_persistent_volume'
        assert client.get('/health').json()['ready'] is False
