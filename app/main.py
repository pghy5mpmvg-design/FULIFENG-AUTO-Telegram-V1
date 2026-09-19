import logging
import os
import uuid
import secrets
from pathlib import Path
from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, UploadFile, File, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
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
    media_dir = Path('/app/data/garage_media') if os.getenv('RAILWAY_SERVICE_ID') else Path('./data/garage_media')
    media_dir.mkdir(parents=True, exist_ok=True)
    security = HTTPBasic()
    garage_user = os.getenv('GARAGE_USER', 'admin')
    garage_password = os.getenv('GARAGE_PASSWORD', '')
    def garage_auth(credentials: HTTPBasicCredentials = Depends(security)):
        valid = bool(garage_password) and secrets.compare_digest(credentials.username, garage_user) and secrets.compare_digest(credentials.password, garage_password)
        if not valid:
            raise HTTPException(status_code=401, detail='Unauthorized', headers={'WWW-Authenticate':'Basic'})
        return credentials.username


    @app.get('/crm', response_class=HTMLResponse)
    def crm(_=Depends(garage_auth)):
        rows = app.state.db.leads()
        due = app.state.db.due_followups()
        due_html = ''.join(f'<tr><td><a href="/crm/{x.id}">{escape(x.first_name or x.username or str(x.telegram_user_id))}</a></td><td>{x.grade}</td><td>{escape(x.vehicle_code or "-")}</td><td>{x.next_follow_up.astimezone(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d %H:%M") if x.next_follow_up else "-"}</td></tr>' for x in due)
        body = ''.join(f'<tr><td>{x.grade}</td><td><a href="/crm/{x.id}">{escape(x.first_name or "-")}</a></td><td>@{escape(x.username or "-")}</td><td>{escape(x.vehicle_code or "-")}</td><td>{escape(x.last_message[:160])}</td><td>{x.updated_at:%Y-%m-%d %H:%M}</td></tr>' for x in rows)
        return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>FULIFENG CRM</title>
        <style>body{{font-family:Arial;max-width:1150px;margin:30px auto;padding:0 16px}}table{{width:100%;border-collapse:collapse}}td,th{{padding:9px;border-bottom:1px solid #ddd;text-align:left}}</style></head>
        <body><h1>FULIFENG AUTO 客户线索 CRM</h1><p><a href="/garage-dashboard">← 运营控制台</a></p><h2>今天 / 已到期必须跟进</h2><table><tr><th>客户</th><th>等级</th><th>车辆</th><th>跟进时间</th></tr>{due_html or "<tr><td colspan=4>暂无到期跟进</td></tr>"}</table><h2>全部客户</h2><table><tr><th>等级</th><th>客户</th><th>Telegram</th><th>咨询车辆</th><th>最近消息</th><th>更新时间</th></tr>{body}</table></body></html>"""

    @app.get('/crm/{lead_id}', response_class=HTMLResponse)
    def crm_detail(lead_id: int, _=Depends(garage_auth)):
        x = app.state.db.lead(lead_id)
        if not x: return HTMLResponse('客户不存在', status_code=404)
        def ev(v): return escape(v or '', quote=True)
        follow = x.next_follow_up.astimezone(ZoneInfo(settings.timezone)).strftime('%Y-%m-%dT%H:%M') if x.next_follow_up else ''
        suggestions = getattr(app.state, 'lead_suggestions', {}).get(lead_id, [])
        suggestions_html = ''.join(f'<form method="post" action="/crm/{lead_id}/send-suggestion"><textarea name="text" rows="3">{escape(t)}</textarea><button type="submit">确认并发送给客户</button></form>' for t in suggestions)
        return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>CRM {lead_id}</title>
        <style>body{{font-family:Arial;max-width:850px;margin:30px auto;padding:0 16px}}input,select,textarea{{width:100%;padding:10px;margin:6px 0;box-sizing:border-box}}button{{padding:12px 20px}}</style></head><body>
        <p><a href="/crm">← CRM</a></p><h1>{ev(x.first_name)} @{ev(x.username)}</h1><p>Telegram ID: {x.telegram_user_id}　咨询车辆: {ev(x.vehicle_code) or "-"}</p>
        <p>最近消息：{escape(x.last_message or "")}</p><form method="post" action="/crm/{lead_id}">
        <label>客户等级</label><select name="grade"><option>{x.grade}</option><option>A+</option><option>A</option><option>B</option><option>C</option></select>
        <label>销售阶段</label><select name="status"><option value="{ev(x.status)}">{ev(x.status)}</option><option value="new">新线索</option><option value="contacted">已联系</option><option value="negotiating">谈判中</option><option value="won">已成交</option><option value="lost">已流失</option></select>
        <input name="budget" value="{ev(x.budget)}" placeholder="预算"><input name="city" value="{ev(x.city)}" placeholder="城市 / 国家">
        <select name="vehicle_preference"><option value="{ev(x.vehicle_preference)}">{ev(x.vehicle_preference) or "新车/二手偏好"}</option><option value="new">新车</option><option value="used">二手车</option><option value="either">均可</option></select>
        <input name="purchase_timing" value="{ev(x.purchase_timing)}" placeholder="预计购买时间"><label>下次跟进</label><input type="datetime-local" name="next_follow_up" value="{follow}">
        <textarea name="manager_note" rows="6" placeholder="销售备注">{ev(x.manager_note)}</textarea><button type="submit">保存客户资料</button></form><h2>AI俄语跟进助手</h2><form method="post" action="/crm/{lead_id}/suggest"><button type="submit">生成3条俄语跟进建议</button></form>{suggestions_html}</body></html>"""

    @app.post('/crm/{lead_id}/suggest')
    async def crm_suggest(lead_id: int, _=Depends(garage_auth)):
        lead = app.state.db.lead(lead_id)
        if not lead: return HTMLResponse('客户不存在', status_code=404)
        vehicle = app.state.db.vehicle(lead.vehicle_code) if lead.vehicle_code else None
        suggestions = await app.state.service.content.lead_followup_suggestions(lead, vehicle.facts if vehicle else '')
        if not hasattr(app.state, 'lead_suggestions'): app.state.lead_suggestions = {}
        app.state.lead_suggestions[lead_id] = suggestions
        return RedirectResponse('/crm/' + str(lead_id), status_code=303)

    @app.post('/crm/{lead_id}/send-suggestion')
    async def crm_send_suggestion(lead_id: int, text: str = Form(...), _=Depends(garage_auth)):
        lead = app.state.db.lead(lead_id)
        if not lead: return HTMLResponse('客户不存在', status_code=404)
        clean = (text or '').strip()
        if not clean or len(clean) > 1500: return HTMLResponse('消息长度无效', status_code=400)
        try:
            await app.state.service.application.bot.send_message(chat_id=lead.telegram_user_id, text=clean)
            app.state.db.update_lead(lead_id, status='contacted')
        except Exception as exc:
            logging.getLogger(__name__).exception('CRM approved send failed: %s', type(exc).__name__)
            return HTMLResponse('发送失败：' + escape(type(exc).__name__), status_code=502)
        return RedirectResponse('/crm/' + str(lead_id), status_code=303)

    @app.post('/crm/{lead_id}')
    def crm_update(lead_id: int, grade: str = Form('C'), status: str = Form('new'), budget: str = Form(''),
                   city: str = Form(''), vehicle_preference: str = Form(''), purchase_timing: str = Form(''),
                   next_follow_up: str = Form(''), manager_note: str = Form(''), _=Depends(garage_auth)):
        follow = None
        if next_follow_up:
            try:
                follow = datetime.fromisoformat(next_follow_up).replace(tzinfo=ZoneInfo(settings.timezone)).astimezone(timezone.utc)
            except ValueError:
                return HTMLResponse('跟进时间格式错误', status_code=400)
        if not app.state.db.update_lead(lead_id, grade, status, budget, city, vehicle_preference, purchase_timing, manager_note, follow):
            return HTMLResponse('客户不存在', status_code=404)
        return RedirectResponse('/crm/' + str(lead_id), status_code=303)

    @app.get('/garage-dashboard', response_class=HTMLResponse)
    def garage_dashboard(_=Depends(garage_auth)):
        s = app.state.db.garage_stats()
        upcoming = app.state.db.upcoming_vehicles()
        failures = app.state.db.failed_vehicle_publications()
        recent = ''.join(f'<tr><td>{escape(p.vehicle_code)}</td><td>{p.created_at:%Y-%m-%d %H:%M}</td><td>{escape(p.mode)}</td><td>{escape(p.status)}</td><td>{p.message_id or "-"}</td></tr>' for p in s['recent'])
        upcoming_html = ''.join(f'<tr><td><a href="/garage/{v.code}">{v.code}</a></td><td>{escape(v.facts[:100])}</td><td>{v.publish_at.astimezone(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d %H:%M") if v.publish_at else "-"}</td><td>{escape(v.repeat_rule)}</td></tr>' for v in upcoming)
        failed_html = ''.join(f'<tr><td>{escape(p.vehicle_code)}</td><td>{p.created_at:%Y-%m-%d %H:%M}</td><td>{escape(p.status)}</td></tr>' for p in failures)
        bot_ok = bool(app.state.service.polling_ok)
        return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>FULIFENG AUTO 运营控制台</title>
        <style>body{{font-family:Arial;max-width:1100px;margin:30px auto;padding:0 16px}}.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px}}.box{{padding:18px;border:1px solid #ddd;border-radius:12px}}.n{{font-size:30px;font-weight:bold}}table{{width:100%;border-collapse:collapse;margin-top:20px}}td,th{{padding:9px;border-bottom:1px solid #ddd;text-align:left}}a{{text-decoration:none}}</style></head><body>
        <h1>FULIFENG AUTO 运营控制台</h1><p><a href="/garage">→ 进入车辆车库</a>　<a href="/crm">→ 客户线索 CRM</a></p><div class="stats">
        <div class="box">总库存<div class="n">{s['total']}</div></div><div class="box">在售<div class="n">{s['available']}</div></div>
        <div class="box">已预订<div class="n">{s['reserved']}</div></div><div class="box">已售<div class="n">{s['sold']}</div></div>
        <div class="box">自动推广<div class="n">{s['auto']}</div></div><div class="box">等待投放<div class="n">{s['due']}</div></div>
        <div class="box">今日已发布<div class="n">{s['sent_today']}</div></div></div>
        <h2>系统状态</h2><p>Telegram Bot：<b>{"运行中" if bot_ok else "未运行/启动中"}</b>　调度器：<b>{"运行中" if app.state.service.scheduler.running else "未运行"}</b>　时区：{escape(settings.timezone)}</p><h2>下一批投放计划</h2><table><tr><th>车辆</th><th>信息</th><th>时间</th><th>周期</th></tr>{upcoming_html}</table><h2>失败任务</h2><table><tr><th>车辆</th><th>时间</th><th>错误</th></tr>{failed_html or "<tr><td colspan=3>暂无失败任务</td></tr>"}</table><h2>最近发布记录</h2><table><tr><th>车辆</th><th>时间</th><th>方式</th><th>状态</th><th>Telegram ID</th></tr>{recent}</table></body></html>"""

    @app.get('/garage', response_class=HTMLResponse)
    def garage(q: str = '', status: str = '', _=Depends(garage_auth)):
        rows = app.state.db.vehicles(100)
        if q: rows = [x for x in rows if q.lower() in x.facts.lower() or q.lower() in (x.code or '').lower()]
        if status: rows = [x for x in rows if x.status == status]
        body = ''.join(f"""<div class="card"><div class="cover">{('<img src="/garage-media/'+escape((x.photo_file_ids.split('|')[0] if x.photo_file_ids else x.photo_file_id).removeprefix('local:'))+'">') if (x.photo_file_ids or x.photo_file_id).startswith('local:') else '🚘'}</div><div><h3><a href="/garage/{x.code}">{escape(x.code or '')}</a></h3><p>{escape(x.facts[:180])}</p><b>{escape(x.status)}</b><p>下次：{x.publish_at.astimezone(ZoneInfo(settings.timezone)).strftime('%Y-%m-%d %H:%M') if x.publish_at else '-'}</p></div></div>""" for x in rows)
        return """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
        <title>FULIFENG AUTO Garage</title><style>body{font-family:Arial;max-width:1100px;margin:30px auto;padding:0 16px}input,textarea,button{width:100%;padding:10px;margin:5px 0;box-sizing:border-box}table{width:100%;border-collapse:collapse;margin-top:25px}.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px;margin-top:25px}.card{border:1px solid #ddd;border-radius:12px;padding:12px}.cover{height:170px;background:#f3f3f3;display:flex;align-items:center;justify-content:center;font-size:50px;border-radius:8px}.cover img{width:100%;height:100%;object-fit:cover;border-radius:8px}td,th{padding:10px;border-bottom:1px solid #ddd;text-align:left}.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}@media(max-width:700px){.grid{grid-template-columns:1fr}}</style></head>
        <body><h1>FULIFENG AUTO 车辆车库</h1><p><a href='/garage-dashboard'>← 运营控制台</a></p><p>车辆资料录入 · 关键参数 · 投放时间</p><form method='get' action='/garage'><div class='grid'><input name='q' placeholder='搜索车型 / 编号'><select name='status'><option value=''>全部状态</option><option value='available'>在售</option><option value='reserved'>已预订</option><option value='sold'>已售</option></select></div><button type='submit'>搜索 / 筛选</button></form>
        <form method="post" action="/garage/add"><div class="grid">
        <input name="model" required placeholder="品牌 / 车型，例如 Audi Q3"><input name="year" placeholder="年份，例如 2022">
        <input name="mileage" placeholder="里程，例如 40000 km"><input name="condition" placeholder="车况，例如 原始油漆">
        <input name="price" placeholder="价格（可选）"><input type="datetime-local" name="publish_at">
        <select name="repeat_rule"><option value="once">仅投放一次</option><option value="daily">每天</option><option value="weekly">每周</option></select>
        <label><input type="checkbox" name="auto_publish" value="1" style="width:auto"> 开启自动投放</label>
        </div><textarea name="details" rows="4" placeholder="发动机、驱动、颜色、配置亮点、备注等关键参数"></textarea>
        <button type="submit">保存到车库</button></form>
        <div class="cards">""" + body + "</div></body></html>"

    @app.post('/garage/add')
    def garage_add(model: str = Form(...), year: str = Form(''), mileage: str = Form(''), condition: str = Form(''),
                   price: str = Form(''), publish_at: str = Form(''), repeat_rule: str = Form('once'),
                   auto_publish: str = Form(''), details: str = Form(''), _=Depends(garage_auth)):
        facts = ' | '.join(x for x in [model, year, mileage, condition, ('价格: ' + price) if price else '', details] if x.strip())
        scheduled = None
        if publish_at:
            local_dt = datetime.fromisoformat(publish_at).replace(tzinfo=ZoneInfo(settings.timezone))
            scheduled = local_dt.astimezone(ZoneInfo('UTC'))
        app.state.db.create_vehicle(facts, '', '', scheduled, repeat_rule if repeat_rule in ('once','daily','weekly') else 'once', auto_publish == '1')
        return RedirectResponse('/garage', status_code=303)

    @app.get('/garage/{code}', response_class=HTMLResponse)
    def garage_vehicle(code: str, _=Depends(garage_auth)):
        x = app.state.db.vehicle(code)
        if not x:
            return HTMLResponse('车辆不存在', status_code=404)
        value = escape(x.facts, quote=True)
        d = app.state.db.vehicle_detail(x.code)
        def dv(name): return escape(getattr(d, name, '') if d else '', quote=True)
        caption = escape(x.caption or '', quote=True)
        photos = [p for p in (x.photo_file_ids or '').split('|') if p] or ([x.photo_file_id] if x.photo_file_id else [])
        gallery = ''.join(f'<div style="display:inline-block;margin:6px"><img src="/garage-media/{escape(p.removeprefix("local:"))}" style="width:150px;height:110px;object-fit:cover"><form method="post" action="/garage/{x.code}/photo-cover"><input type="hidden" name="photo" value="{escape(p, quote=True)}"><button>设为封面</button></form><form method="post" action="/garage/{x.code}/photo-delete"><input type="hidden" name="photo" value="{escape(p, quote=True)}"><button>删除</button></form></div>' for p in photos if p.startswith('local:'))
        history = app.state.db.vehicle_publications(x.code)
        history_html = ''.join(f'<tr><td>{h.created_at:%Y-%m-%d %H:%M}</td><td>{escape(h.mode)}</td><td>{escape(h.status)}</td><td>{h.message_id or "-"}</td></tr>' for h in history)
        return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>{x.code}</title>
        <style>body{{font-family:Arial;max-width:850px;margin:30px auto;padding:0 16px}}input,textarea,select,button{{width:100%;padding:10px;margin:6px 0;box-sizing:border-box}}</style></head><body>
        <a href="/garage">← 返回车库</a><h1>{x.code}</h1><p>状态：{escape(x.status)}</p><h3>车辆图片</h3><div>{gallery or "暂无图片"}</div>
        <form method="post" action="/garage/{x.code}/details"><h3>结构化车辆参数</h3><div style="display:grid;grid-template-columns:1fr 1fr;gap:8px"><input name="brand" value="{dv('brand')}" placeholder="品牌"><input name="model" value="{dv('model')}" placeholder="车型"><input name="year" value="{dv('year')}" placeholder="年份"><select name="condition"><option value="used">二手车</option><option value="new">新车</option></select><input name="mileage_km" value="{dv('mileage_km')}" placeholder="里程 km"><input name="engine" value="{dv('engine')}" placeholder="发动机"><input name="transmission" value="{dv('transmission')}" placeholder="变速箱"><input name="drivetrain" value="{dv('drivetrain')}" placeholder="驱动方式"><input name="color" value="{dv('color')}" placeholder="颜色"><input name="paint_condition" value="{dv('paint_condition')}" placeholder="漆面/车况"><input name="purchase_price_cny" value="{dv('purchase_price_cny')}" placeholder="中国采购价 CNY"><input name="sale_price" value="{dv('sale_price')}" placeholder="对外报价"></div><textarea name="highlights" rows="3" placeholder="配置亮点">{dv('highlights')}</textarea><textarea name="notes" rows="3" placeholder="内部备注">{dv('notes')}</textarea><button type="submit">保存车辆参数</button></form><form method="post" action="/garage/{x.code}/edit" enctype="multipart/form-data">
        <label>车辆关键参数</label><textarea name="facts" rows="7">{value}</textarea>
        <label>俄语发布文案</label><textarea name="caption" rows="9">{caption}</textarea>
        <label>投放时间</label><input type="datetime-local" name="publish_at">
        <label>投放周期</label><select name="repeat_rule"><option value="once">仅一次</option><option value="daily">每天</option><option value="weekly">每周</option></select>
        <label><input type="checkbox" name="auto_publish" value="1" style="width:auto"> 开启自动投放</label>
        <label>车辆图片（可多选，第一张作为封面）</label><input type="file" name="photos" multiple accept="image/jpeg,image/png,image/webp">
        <label>车辆状态</label><select name="status"><option value="available">在售</option><option value="reserved">已预订</option><option value="sold">已售</option></select>
        <button type="submit">保存修改</button></form><form method="post" action="/garage/{x.code}/duplicate"><button type="submit">复制车辆</button></form><form method="post" action="/garage/{x.code}/promotion"><input type="hidden" name="enabled" value="0"><button type="submit">停止推广</button></form><form method="post" action="/garage/{x.code}/promotion"><input type="hidden" name="enabled" value="1"><button type="submit">恢复推广</button></form><form method="post" action="/garage/{x.code}/publish-now"><button type="submit">立即发布到 Telegram</button></form><form method="post" action="/garage/{x.code}/generate-copy"><button type="submit">AI生成/重写俄语文案</button></form></body></html>"""

    @app.post('/garage/{code}/edit')
    async def garage_vehicle_edit(code: str, facts: str = Form(...), caption: str = Form(''), publish_at: str = Form(''),
                                  repeat_rule: str = Form('once'), auto_publish: str = Form(''), status: str = Form('available'), photos: list[UploadFile] = File(default=[]), _=Depends(garage_auth)):
        scheduled = None
        if publish_at:
            local_dt = datetime.fromisoformat(publish_at).replace(tzinfo=ZoneInfo(settings.timezone))
            scheduled = local_dt.astimezone(ZoneInfo('UTC'))
        stored = []
        for photo in photos[:10]:
            if not photo.filename:
                continue
            data = await photo.read()
            if len(data) > 10 * 1024 * 1024:
                return HTMLResponse('单张图片不能超过10MB', status_code=400)
            suffix = Path(photo.filename).suffix.lower()
            if suffix not in ('.jpg', '.jpeg', '.png', '.webp'):
                return HTMLResponse('仅支持 JPG/PNG/WEBP', status_code=400)
            filename = f'{code}-{uuid.uuid4().hex}{suffix}'
            (media_dir / filename).write_bytes(data)
            stored.append('local:' + filename)
        x = app.state.db.vehicle(code)
        existing = [p for p in (x.photo_file_ids or '').split('|') if p] if x else []
        all_photos = existing + stored
        cover = all_photos[0] if all_photos else None
        ok = app.state.db.update_vehicle(code, facts=facts, caption=caption, photo_file_id=cover,
                                         photo_file_ids='|'.join(all_photos), publish_at=scheduled,
                                         repeat_rule=repeat_rule, auto_publish=(auto_publish == '1' and status == 'available'))
        if status in ('available','reserved','sold'):
            app.state.db.update_vehicle_status(code, status)
        if not ok:
            return HTMLResponse('车辆不存在', status_code=404)
        return RedirectResponse('/garage/' + code, status_code=303)

    @app.post('/garage/{code}/generate-copy')
    async def garage_generate_copy(code: str, _=Depends(garage_auth)):
        x = app.state.db.vehicle(code)
        if not x:
            return HTMLResponse('车辆不存在', status_code=404)
        detail = app.state.db.vehicle_detail(code)
        caption = await app.state.service.content.structured_vehicle_listing(detail, x.facts)
        app.state.db.update_vehicle(code, caption=caption)
        return RedirectResponse('/garage/' + code, status_code=303)

    @app.post('/garage/{code}/publish-now')
    async def garage_publish_now(code: str, _=Depends(garage_auth)):
        x = app.state.db.vehicle(code)
        if not x:
            return HTMLResponse('车辆不存在', status_code=404)
        if x.status != 'available':
            return HTMLResponse('只有在售车辆可以发布', status_code=400)
        target = app.state.db.get('target_chat')
        if not target:
            return HTMLResponse('请先在 Telegram 设置目标频道 /setchat', status_code=400)
        caption = x.caption.strip() if x.caption else await app.state.service.content.sales_listing(x.facts)
        photos = [p for p in (x.photo_file_ids or '').split('|') if p] or ([x.photo_file_id] if x.photo_file_id else [])
        def media_value(p):
            return media_dir / p.removeprefix('local:') if p.startswith('local:') else p
        try:
            if len(photos) > 1:
                from telegram import InputMediaPhoto
                media = [InputMediaPhoto(media=media_value(p), caption=caption[:1024] if i == 0 else None) for i,p in enumerate(photos[:10])]
                sent_group = await app.state.service.application.bot.send_media_group(chat_id=target, media=media)
                sent = sent_group[0]
            elif photos:
                sent = await app.state.service.application.bot.send_photo(chat_id=target, photo=media_value(photos[0]), caption=caption[:1024])
            else:
                sent = await app.state.service.application.bot.send_message(chat_id=target, text=caption[:4096])
            app.state.db.mark_vehicle_published(code, sent.message_id)
            app.state.db.record_vehicle_publication(code, target, sent.message_id, caption, mode='manual')
        except Exception as exc:
            logging.getLogger(__name__).exception('Garage immediate publish failed: %s', type(exc).__name__)
            return HTMLResponse('发布失败：' + escape(type(exc).__name__), status_code=502)
        return RedirectResponse('/garage/' + code, status_code=303)

    @app.post('/garage/{code}/details')
    def garage_details(code: str, brand: str = Form(''), model: str = Form(''), year: str = Form(''),
                       condition: str = Form('used'), mileage_km: str = Form(''), engine: str = Form(''),
                       transmission: str = Form(''), drivetrain: str = Form(''), color: str = Form(''),
                       paint_condition: str = Form(''), purchase_price_cny: str = Form(''), sale_price: str = Form(''),
                       highlights: str = Form(''), notes: str = Form(''), _=Depends(garage_auth)):
        x = app.state.db.vehicle(code)
        if not x: return HTMLResponse('车辆不存在', status_code=404)
        app.state.db.save_vehicle_detail(code, brand=brand, model=model, year=year, condition=condition,
            mileage_km=mileage_km, engine=engine, transmission=transmission, drivetrain=drivetrain, color=color,
            paint_condition=paint_condition, purchase_price_cny=purchase_price_cny, sale_price=sale_price,
            highlights=highlights, notes=notes)
        facts = ' | '.join(v for v in [f'{brand} {model}'.strip(), year, ('新车' if condition == 'new' else '二手车'),
            (mileage_km + ' km') if mileage_km else '', engine, transmission, drivetrain, color, paint_condition,
            ('报价: ' + sale_price) if sale_price else '', highlights] if v)
        app.state.db.update_vehicle(code, facts=facts)
        return RedirectResponse('/garage/' + code, status_code=303)

    @app.post('/garage/{code}/duplicate')
    def garage_duplicate(code: str, _=Depends(garage_auth)):
        new_code = app.state.db.duplicate_vehicle(code)
        if not new_code: return HTMLResponse('车辆不存在', status_code=404)
        return RedirectResponse('/garage/' + new_code, status_code=303)

    @app.post('/garage/{code}/promotion')
    def garage_promotion(code: str, enabled: str = Form('0'), _=Depends(garage_auth)):
        x = app.state.db.vehicle(code)
        if not x: return HTMLResponse('车辆不存在', status_code=404)
        app.state.db.update_vehicle(code, auto_publish=(enabled == '1' and x.status == 'available'))
        return RedirectResponse('/garage/' + code, status_code=303)

    @app.post('/garage/{code}/photo-cover')
    def garage_photo_cover(code: str, photo: str = Form(...), _=Depends(garage_auth)):
        x = app.state.db.vehicle(code)
        if not x: return HTMLResponse('车辆不存在', status_code=404)
        photos = [p for p in (x.photo_file_ids or '').split('|') if p] or ([x.photo_file_id] if x.photo_file_id else [])
        if photo in photos:
            photos = [photo] + [p for p in photos if p != photo]
            app.state.db.set_vehicle_photos(code, photos)
        return RedirectResponse('/garage/' + code, status_code=303)

    @app.post('/garage/{code}/photo-delete')
    def garage_photo_delete(code: str, photo: str = Form(...), _=Depends(garage_auth)):
        x = app.state.db.vehicle(code)
        if not x:
            return HTMLResponse('车辆不存在', status_code=404)
        photos = [p for p in (x.photo_file_ids or '').split('|') if p] or ([x.photo_file_id] if x.photo_file_id else [])
        photos = [p for p in photos if p != photo]
        if photo.startswith('local:'):
            path = media_dir / Path(photo.removeprefix('local:')).name
            if path.exists():
                path.unlink()
        app.state.db.update_vehicle(code, photo_file_id=photos[0] if photos else '', photo_file_ids='|'.join(photos))
        return RedirectResponse('/garage/' + code, status_code=303)

    @app.get('/garage-media/{filename}')
    def garage_media(filename: str, _=Depends(garage_auth)):
        safe = Path(filename).name
        path = media_dir / safe
        if not path.exists():
            raise HTTPException(status_code=404)
        return FileResponse(path)

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
