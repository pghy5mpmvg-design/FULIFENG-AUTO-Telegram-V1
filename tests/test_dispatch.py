import json
from telegram import Update
from telegram.ext import Application
from telegram.request import BaseRequest
import pytest
from app.config import Settings
from app.database import Database
from app.service import BotService


class TelegramTransport(BaseRequest):
    def __init__(self):
        self.sent = []

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        endpoint = url.rsplit('/', 1)[-1]
        if endpoint == 'getMe':
            result = {'id': 111, 'is_bot': True, 'first_name': 'Test', 'username': 'testbot'}
        elif endpoint == 'sendMessage':
            params = request_data.parameters
            self.sent.append(params['text'])
            result = {'message_id': 1, 'date': 1, 'chat': {'id': 123, 'type': 'private'}, 'text': params['text']}
        else:
            raise AssertionError(endpoint)
        return 200, json.dumps({'ok': True, 'result': result}).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize('command', ['/today', '/stats', '/post', '/help', '/today@testbot'])
async def test_real_ptb_dispatch(tmp_path, monkeypatch, command):
    transport = TelegramTransport()
    original_builder = Application.builder
    monkeypatch.setattr(Application, 'builder', staticmethod(lambda: original_builder().request(transport).get_updates_request(transport)))
    settings = Settings(bot_token='111:local-test-only', openai_api_key='', admin_ids={123}, database_url=f'sqlite:///{tmp_path}/db')
    db = Database(settings.database_url)
    db.initialize()
    service = BotService(settings, db)
    app = service.build()
    await app.initialize()
    try:
        update = Update.de_json({'update_id': 1, 'message': {'message_id': 1, 'date': 1,
            'chat': {'id': 123, 'type': 'private'}, 'from': {'id': 123, 'is_bot': False, 'first_name': 'Test'},
            'text': command, 'entities': [{'type': 'bot_command', 'offset': 0, 'length': len(command)}]}}, app.bot)
        await app.process_update(update)
        assert transport.sent
    finally:
        await app.shutdown()
        db.engine.dispose()
