import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form
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
        body = ''.join(f"<tr><td>{x.code}</td><td>{x.facts}</td><td>{x.status}</td><td>{x.created_at:%Y-%m-%d}</td></tr>" for x in rows)
        return """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
        <title>FULIFENG AUTO Garage</title><style>body{font-family:Arial;max-width:1100px;margin:30px auto;padding:0 16px}input,textarea,button{width:100%;padding:10px;margin:5px 0;box-sizing:border-box}table{width:100%;border-collapse:collapse;margin-top:25px}td,th{padding:10px;border-bottom:1px solid #ddd;text-align:left}.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}@media(max-width:700px){.grid{grid-template-columns:1fr}}</style></head>
        <body><h1>FULIFENG AUTO 车辆车库</h1><p>车辆资料录入 · 关键参数 · 投放时间</p>
        <form method="post" action="/garage/add"><div class="grid">
        <input name="model" required placeholder="品牌 / 车型，例如 Audi Q3"><input name="year" placeholder="年份，例如 2022">
        <input name="mileage" placeholder="里程，例如 40000 km"><input name="condition" placeholder="车况，例如 原始油漆">
        <input name="price" placeholder="价格（可选）"><input name="publish_at" placeholder="投放时间，例如 2026-09-20 18:00">
        </div><textarea name="details" rows="4" placeholder="发动机、驱动、颜色、配置亮点、备注等关键参数"></textarea>
        <button type="submit">保存到车库</button></form>
        <table><tr><th>编号</th><th>车辆信息</th><th>状态</th><th>录入日期</th></tr>""" + body + "</table></body></html>"

    @app.post('/garage/add')
    def garage_add(model: str = Form(...), year: str = Form(''), mileage: str = Form(''), condition: str = Form(''),
                   price: str = Form(''), publish_at: str = Form(''), details: str = Form('')):
        facts = ' | '.join(x for x in [model, year, mileage, condition, ('价格: ' + price) if price else '',
                                       ('投放: ' + publish_at) if publish_at else '', details] if x.strip())
        app.state.db.create_vehicle(facts, '', '')
        return RedirectResponse('/garage', status_code=303)

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
