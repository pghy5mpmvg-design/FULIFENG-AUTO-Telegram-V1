import logging
import os
import uuid
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import text

from .config import Settings
from .database import Database
from .service import BotService

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
# HTTP libraries can include Telegram's token-bearing URL in logs.
for name in ('httpx', 'httpcore', 'telegram', 'openai'):
    logging.getLogger(name).setLevel(logging.CRITICAL)


def create_app(settings=None):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        db = Database(settings.database_url)
        db.initialize(settings.target_chat)
        service = BotService(settings, db)
        app.state.db, app.state.service = db, service
        app.state.bot_state = 'not_configured'
        storage_ready = not (os.getenv('RAILWAY_SERVICE_ID') and settings.database_url.startswith('sqlite') and
                             os.getenv('RAILWAY_VOLUME_MOUNT_PATH') != '/app/data')
        app.state.storage_ready = storage_ready
        try:
            if not storage_ready:
                app.state.bot_state = 'needs_persistent_volume'
                logging.getLogger(__name__).warning('Attach Railway volume at /app/data before enabling Telegram')
            elif settings.bot_token:
                try:
                    await service.start()
                    app.state.bot_state = 'starting'
                except Exception as exc:
                    app.state.bot_state = 'failed'
                    logging.getLogger(__name__).error('Bot startup failed: %s', type(exc).__name__)
            yield
        finally:
            await service.stop()
            db.engine.dispose()

    app = FastAPI(title='FULIFENG AUTO Telegram V1', lifespan=lifespan, docs_url=None, redoc_url=None)


    @app.get('/garage', response_class=HTMLResponse)
    def garage():
        rows = app.state.db.vehicles(100)
        body = ''.join(f"<tr><td><a href="/garage/{x.code}">{escape(x.code or '')}</a></td><td>{escape(x.facts)}</td><td>{escape(x.status)}</td><td>{x.publish_at.astimezone(ZoneInfo(settings.timezone)).strftime('%Y-%m-%d %H:%M') if x.publish_at else '-'}</td><td>{'开启' if x.auto_publish else '关闭'} / {escape(x.repeat_rule)}</td></tr>" for x in rows)
        return """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
        <title>FULIFENG AUTO Garage</title><style>body{font-family:Arial;max-width:1100px;margin:30px auto;padding:0 16px}input,textarea,button{width:100%;padding:10px;margin:5px 0;box-sizing:border-box}table{width:100%;border-collapse:collapse;margin-top:25px}td,th{padding:10px;border-bottom:1px solid #ddd;text-align:left}.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}@media(max-width:700px){.grid{grid-template-columns:1fr}}</style></head>
        <body><h1>FULIFENG AUTO 车辆车库</h1><p>车辆资料录入 · 关键参数 · 投放时间</p>
        <form method="post" action="/garage/add"><div class="grid">
        <input name="model" required placeholder="品牌 / 车型，例如 Audi Q3"><input name="year" placeholder="年份，例如 2022">
        <input name="mileage" placeholder="里程，例如 40000 km"><input name="condition" placeholder="车况，例如 原始油漆">
        <input name="price" placeholder="价格（可选）"><input type="datetime-local" name="publish_at">
        <select name="repeat_rule"><option value="once">仅投放一次</option><option value="daily">每天</option><option value="weekly">每周</option></select>
        <label><input type="checkbox" name="auto_publish" value="1" style="width:auto"> 开启自动投放</label>
        </div><textarea name="details" rows="4" placeholder="发动机、驱动、颜色、配置亮点、备注等关键参数"></textarea>
        <button type="submit">保存到车库</button></form>
        <table><tr><th>编号</th><th>车辆信息</th><th>状态</th><th>下次投放</th><th>自动投放</th></tr>""" + body + "</table></body></html>"

    @app.post('/garage/add')
    def garage_add(model: str = Form(...), year: str = Form(''), mileage: str = Form(''), condition: str = Form(''),
                   price: str = Form(''), publish_at: str = Form(''), repeat_rule: str = Form('once'),
                   auto_publish: str = Form(''), details: str = Form('')):
        facts = ' | '.join(x for x in [model, year, mileage, condition, ('价格: ' + price) if price else '', details] if x.strip())
        scheduled = None
        if publish_at:
            local_dt = datetime.fromisoformat(publish_at).replace(tzinfo=ZoneInfo(settings.timezone))
            scheduled = local_dt.astimezone(ZoneInfo('UTC'))
        app.state.db.create_vehicle(facts, '', '', scheduled, repeat_rule if repeat_rule in ('once','daily','weekly') else 'once', auto_publish == '1')
        return RedirectResponse('/garage', status_code=303)

    @app.get('/garage/{code}', response_class=HTMLResponse)
    def garage_vehicle(code: str):
        x = app.state.db.vehicle(code)
        if not x:
            return HTMLResponse('车辆不存在', status_code=404)
        value = escape(x.facts, quote=True)
        caption = escape(x.caption or '', quote=True)
        return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>{x.code}</title>
        <style>body{{font-family:Arial;max-width:850px;margin:30px auto;padding:0 16px}}input,textarea,select,button{{width:100%;padding:10px;margin:6px 0;box-sizing:border-box}}</style></head><body>
        <a href="/garage">← 返回车库</a><h1>{x.code}</h1>
        <form method="post" action="/garage/{x.code}/edit" enctype="multipart/form-data">
        <label>车辆关键参数</label><textarea name="facts" rows="7">{value}</textarea>
        <label>俄语发布文案</label><textarea name="caption" rows="9">{caption}</textarea>
        <label>投放时间</label><input type="datetime-local" name="publish_at">
        <label>投放周期</label><select name="repeat_rule"><option value="once">仅一次</option><option value="daily">每天</option><option value="weekly">每周</option></select>
        <label><input type="checkbox" name="auto_publish" value="1" style="width:auto"> 开启自动投放</label>
        <label>车辆主图</label><input type="file" name="photo" accept="image/jpeg,image/png,image/webp">
        <button type="submit">保存修改</button></form></body></html>"""

    @app.post('/garage/{code}/edit')
    async def garage_vehicle_edit(code: str, facts: str = Form(...), caption: str = Form(''), publish_at: str = Form(''),
                                  repeat_rule: str = Form('once'), auto_publish: str = Form(''), photo: UploadFile | None = File(None)):
        scheduled = None
        if publish_at:
            local_dt = datetime.fromisoformat(publish_at).replace(tzinfo=ZoneInfo(settings.timezone))
            scheduled = local_dt.astimezone(ZoneInfo('UTC'))
        photo_file_id = None
        if photo and photo.filename:
            data = await photo.read()
            if len(data) > 10 * 1024 * 1024:
                return HTMLResponse('图片不能超过10MB', status_code=400)
            target = app.state.db.get('target_chat')
            if not target:
                return HTMLResponse('请先在 Telegram 设置目标频道 /setchat', status_code=400)
            sent = await app.state.service.application.bot.send_photo(chat_id=target, photo=data, caption='车库图片上传：' + code)
            photo_file_id = sent.photo[-1].file_id
        ok = app.state.db.update_vehicle(code, facts=facts, caption=caption, photo_file_id=photo_file_id,
                                         publish_at=scheduled, repeat_rule=repeat_rule, auto_publish=auto_publish == '1')
        if not ok:
            return HTMLResponse('车辆不存在', status_code=404)
        return RedirectResponse('/garage/' + code, status_code=303)

    @app.get('/health')
    def health():
        try:
            with app.state.db.engine.connect() as conn:
                conn.execute(text('SELECT 1'))
        except Exception:
            return JSONResponse({'status': 'error', 'database': 'unavailable'}, status_code=503)
        service = app.state.service
        bot_state = 'running' if service.polling_ok else app.state.bot_state
        if service.error_code:
            bot_state = 'degraded'
        # Missing secrets permits infrastructure bootstrap; ready explicitly stays false.
        failed = bot_state in ('failed', 'degraded')
        return JSONResponse({'status': 'degraded' if failed else 'ok', 'database': 'ok',
                             'bot': bot_state, 'ready': bot_state == 'running' and bool(settings.admin_ids),
                             'admin_configured': bool(settings.admin_ids), 'storage_ready': app.state.storage_ready,
                             'scheduler': service.scheduler.running,
                             'paused': app.state.db.get('paused') != 'false',
                             'timezone': settings.timezone}, status_code=503 if failed else 200)

    return app


app = create_app()
