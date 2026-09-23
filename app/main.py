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
        funnel = app.state.db.lead_funnel()
        due = app.state.db.due_followups()
        due_html = ''.join(f'<tr><td><a href="/crm/{x.id}">{escape(x.first_name or x.username or str(x.telegram_user_id))}</a></td><td>{x.grade}</td><td>{escape(x.vehicle_code or "-")}</td><td>{x.next_follow_up.astimezone(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d %H:%M") if x.next_follow_up else "-"}</td></tr>' for x in due)
        body = ''.join(f'<tr><td>{x.grade}</td><td><a href="/crm/{x.id}">{escape(x.first_name or "-")}</a></td><td>@{escape(x.username or "-")}</td><td>{escape(x.vehicle_code or "-")}</td><td>{escape(x.last_message[:160])}</td><td>{x.updated_at:%Y-%m-%d %H:%M}</td></tr>' for x in rows)
        return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>FULIFENG CRM</title>
        <style>body{{font-family:Arial;max-width:1150px;margin:30px auto;padding:0 16px}}table{{width:100%;border-collapse:collapse}}td,th{{padding:9px;border-bottom:1px solid #ddd;text-align:left}}</style></head>
        <body><h1>FULIFENG AUTO 客户线索 CRM</h1><p><a href="/garage-dashboard">← 运营控制台</a></p><p><b>总客户 {funnel["total"]}</b>　A+ {funnel["grades"].get("A+",0)}　A {funnel["grades"].get("A",0)}　B {funnel["grades"].get("B",0)}　C {funnel["grades"].get("C",0)}</p><p>新线索 {funnel["stages"].get("new",0)} → 已联系 {funnel["stages"].get("contacted",0)} → 谈判中 {funnel["stages"].get("negotiating",0)} → 已成交 {funnel["stages"].get("won",0)}　|　已流失 {funnel["stages"].get("lost",0)}</p><h2>今天 / 已到期必须跟进</h2><table><tr><th>客户</th><th>等级</th><th>车辆</th><th>跟进时间</th></tr>{due_html or "<tr><td colspan=4>暂无到期跟进</td></tr>"}</table><h2>全部客户</h2><table><tr><th>等级</th><th>客户</th><th>Telegram</th><th>咨询车辆</th><th>最近消息</th><th>更新时间</th></tr>{body}</table></body></html>"""

    @app.get('/crm/{lead_id}', response_class=HTMLResponse)
    def crm_detail(lead_id: int, _=Depends(garage_auth)):
        x = app.state.db.lead(lead_id)
        if not x: return HTMLResponse('客户不存在', status_code=404)
        def ev(v): return escape(v or '', quote=True)
        follow = x.next_follow_up.astimezone(ZoneInfo(settings.timezone)).strftime('%Y-%m-%dT%H:%M') if x.next_follow_up else ''
        suggestions = getattr(app.state, 'lead_suggestions', {}).get(lead_id, [])
        messages = app.state.db.lead_messages(x.telegram_user_id)
        matches = app.state.db.match_vehicles_for_lead(lead_id, 5)
        match_html = ''.join(f'<div style="border:1px solid #ddd;padding:12px;margin:8px 0;border-radius:8px"><b><a href="/garage/{v.code}">{v.code}</a></b> · {escape(v.facts[:220])}<br><small>匹配依据：{escape("、".join(reasons) or "当前可售库存")}</small></div>' for score,v,d,reasons in matches)
        chat_html = ''.join(f'<div style="margin:8px 0;padding:10px;border-radius:8px;background:{"#eef" if m.direction == "in" else "#efe"}"><b>{"客户" if m.direction == "in" else "FULIFENG"}</b> · {m.created_at:%Y-%m-%d %H:%M}<br>{escape(m.text)}</div>' for m in messages)
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
        <textarea name="manager_note" rows="6" placeholder="销售备注">{ev(x.manager_note)}</textarea><button type="submit">保存客户资料</button></form><h2>AI俄语跟进助手</h2><form method="post" action="/crm/{lead_id}/suggest"><button type="submit">生成3条俄语跟进建议</button></form><form method="post" action="/crm/{lead_id}/recommend"><button type="submit">根据当前库存生成推荐话术</button></form>{suggestions_html}<h2>库存智能匹配</h2>{match_html or "暂无可售匹配车辆"}<h2>Telegram 聊天历史</h2>{chat_html or "暂无聊天记录"}</body></html>"""

    @app.post('/crm/{lead_id}/recommend')
    async def crm_recommend(lead_id: int, _=Depends(garage_auth)):
        lead = app.state.db.lead(lead_id)
        if not lead: return HTMLResponse('客户不存在', status_code=404)
        matches = app.state.db.match_vehicles_for_lead(lead_id, 3)
        if not matches: return HTMLResponse('当前没有可售匹配车辆', status_code=404)
        lines = []
        for score, v, d, reasons in matches:
            price = (' | ' + str(d.sale_price)) if d and d.sale_price else ''
            lines.append(v.code + price + ': ' + v.facts[:500])
        prompt = '客户需求: ' + (lead.last_message or '') + '\n预算: ' + (lead.budget or '') + '\n城市: ' + (lead.city or '') + '\n可售候选车辆:\n' + '\n'.join(lines)
        suggestions = await app.state.service.content.lead_followup_suggestions(lead, prompt)
        if not hasattr(app.state, 'lead_suggestions'): app.state.lead_suggestions = {}
        app.state.lead_suggestions[lead_id] = suggestions
        return RedirectResponse('/crm/' + str(lead_id), status_code=303)

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
            app.state.db.record_lead_message(lead.telegram_user_id, 'out', clean)
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
        app.state.db.recalculate_lead_grade(lead_id)
        return RedirectResponse('/crm/' + str(lead_id), status_code=303)

    @app.get('/garage-dashboard', response_class=HTMLResponse)
    def garage_dashboard(_=Depends(garage_auth)):
        s = app.state.db.garage_stats()
        intervention = app.state.db.intervention_leads()
        intervention_html = ''.join(f'<tr><td><b>{x.grade}</b></td><td><a href="/crm/{x.id}">{escape(x.first_name or x.username or str(x.telegram_user_id))}</a></td><td>{escape(x.vehicle_code or "-")}</td><td>{escape(x.budget or "-")}</td><td>{escape(x.city or "-")}</td><td>{escape(x.last_message[:120])}</td></tr>' for x in intervention)
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
        <h2>🔥 需要人工立即介入</h2><table><tr><th>等级</th><th>客户</th><th>车辆</th><th>预算</th><th>城市</th><th>最近消息</th></tr>{intervention_html or "<tr><td colspan=6>暂无 A+/A 高意向客户</td></tr>"}</table><h2>系统状态</h2><p>Telegram Bot：<b>{"运行中" if bot_ok else "未运行/启动中"}</b>　调度器：<b>{"运行中" if app.state.service.scheduler.running else "未运行"}</b>　时区：{escape(settings.timezone)}</p><h2>下一批投放计划</h2><table><tr><th>车辆</th><th>信息</th><th>时间</th><th>周期</th></tr>{upcoming_html}</table><h2>失败任务</h2><table><tr><th>车辆</th><th>时间</th><th>错误</th></tr>{failed_html or "<tr><td colspan=3>暂无失败任务</td></tr>"}</table><h2>最近发布记录</h2><table><tr><th>车辆</th><th>时间</th><th>方式</th><th>状态</th><th>Telegram ID</th></tr>{recent}</table></body></html>"""

    @app.get('/garage', response_class=HTMLResponse)
    def garage(q: str = '', status: str = '', _=Depends(garage_auth)):
        rows = app.state.db.vehicles(100)
        if q: rows = [x for x in rows if q.lower() in x.facts.lower() or q.lower() in (x.code or '').lower()]
        if status: rows = [x for x in rows if x.status == status]
        body = ''.join(f"""<div class="card"><div class="cover">{('<img src="/garage-media/'+escape((x.photo_file_ids.split('|')[0] if x.photo_file_ids else x.photo_file_id).removeprefix('local:'))+'">') if (x.photo_file_ids or x.photo_file_id).startswith('local:') else '🚘'}</div><div><h3><a href="/garage/{x.code}">{escape(x.code or '')}</a></h3><p>{escape(x.facts[:180])}</p><b>{escape(x.status)}</b><p>下次：{x.publish_at.astimezone(ZoneInfo(settings.timezone)).strftime('%Y-%m-%d %H:%M') if x.publish_at else '-'}</p></div></div>""" for x in rows)
        return """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
        <title>FULIFENG AUTO Garage</title><style>body{font-family:Arial;max-width:1100px;margin:30px auto;padding:0 16px}input,textarea,button{width:100%;padding:10px;margin:5px 0;box-sizing:border-box}table{width:100%;border-collapse:collapse;margin-top:25px}.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px;margin-top:25px}.card{border:1px solid #ddd;border-radius:12px;padding:12px}.cover{height:170px;background:#f3f3f3;display:flex;align-items:center;justify-content:center;font-size:50px;border-radius:8px}.cover img{width:100%;height:100%;object-fit:cover;border-radius:8px}td,th{padding:10px;border-bottom:1px solid #ddd;text-align:left}.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}@media(max-width:700px){.grid{grid-template-columns:1fr}}</style></head>
        <body><h1>FULIFENG AUTO 车辆车库</h1><p><a href='/garage-dashboard'>← 运营控制台</a>　<a href='/garage/batch'>📥 批量导入库存</a>　<a href='/garage/batch-photos'>🖼 批量匹配照片</a>　<a href='/garage/auto-schedule'>⏱ 自动排期</a></p><p>车辆资料录入 · 关键参数 · 投放时间</p><form method='get' action='/garage'><div class='grid'><input name='q' placeholder='搜索车型 / 编号'><select name='status'><option value=''>全部状态</option><option value='available'>在售</option><option value='reserved'>已预订</option><option value='sold'>已售</option></select></div><button type='submit'>搜索 / 筛选</button></form>
        <form method="post" action="/garage/add"><div class="grid">
        <input name="model" required placeholder="品牌 / 车型，例如 Audi Q3"><input name="year" placeholder="年份，例如 2022">
        <input name="mileage" placeholder="里程，例如 40000 km"><input name="condition" placeholder="车况，例如 原始油漆">
        <input name="price" placeholder="价格（可选）"><input type="datetime-local" name="publish_at">
        <select name="repeat_rule"><option value="once">仅投放一次</option><option value="daily">每天</option><option value="weekly">每周</option></select>
        <label><input type="checkbox" name="auto_publish" value="1" style="width:auto"> 开启自动投放</label>
        </div><textarea name="details" rows="4" placeholder="发动机、驱动、颜色、配置亮点、备注等关键参数"></textarea>
        <button type="submit">保存到车库</button></form>
        <div class="cards">""" + body + "</div></body></html>"

    @app.get('/garage/batch', response_class=HTMLResponse)
    def garage_batch(_=Depends(garage_auth)):
        sample = '品牌,车型,年份,新车/二手,里程km,发动机,变速箱,驱动,颜色,漆面车况,对外报价,配置亮点,采购价CNY,内部备注'
        return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>批量导入库存</title>
        <style>body{{font-family:Arial;max-width:900px;margin:30px auto;padding:0 16px}}textarea,button{{width:100%;padding:12px;margin:8px 0;box-sizing:border-box}}code{{display:block;white-space:pre-wrap;background:#f5f5f5;padding:12px}}</style></head><body>
        <a href="/garage">← 返回车库</a><h1>批量导入库存</h1><p>第一版支持 CSV 文本批量导入。一行一辆车；可直接从 Excel 复制为 CSV。导入后系统自动生成 FF 编号，采购价和内部备注只保存在后台。</p>
        <code>{sample}</code>
        <form method="post" action="/garage/batch"><textarea name="csv_text" rows="18" required placeholder="粘贴 CSV 内容，第一行可使用上面的表头"></textarea><button type="submit">批量创建库存</button></form></body></html>"""

    @app.post('/garage/batch', response_class=HTMLResponse)
    def garage_batch_import(csv_text: str = Form(...), _=Depends(garage_auth)):
        import csv, io
        aliases = {
            '品牌':'brand','brand':'brand','车型':'model','model':'model','年份':'year','year':'year',
            '新车/二手':'condition','车况类型':'condition','condition':'condition','里程km':'mileage_km','里程':'mileage_km','mileage':'mileage_km',
            '发动机':'engine','engine':'engine','变速箱':'transmission','transmission':'transmission','驱动':'drivetrain','drivetrain':'drivetrain',
            '颜色':'color','color':'color','漆面车况':'paint_condition','漆面':'paint_condition','paint_condition':'paint_condition',
            '对外报价':'sale_price','售价':'sale_price','sale_price':'sale_price','配置亮点':'highlights','亮点':'highlights','highlights':'highlights',
            '采购价CNY':'purchase_price_cny','采购价':'purchase_price_cny','purchase_price_cny':'purchase_price_cny',
            '内部备注':'notes','备注':'notes','notes':'notes'
        }
        raw = (csv_text or '').lstrip('\ufeff').strip()
        if not raw:
            return HTMLResponse('没有可导入的数据', status_code=400)
        reader = csv.reader(io.StringIO(raw))
        rows = list(reader)
        if not rows:
            return HTMLResponse('没有可导入的数据', status_code=400)
        first = [x.strip() for x in rows[0]]
        has_header = any(x in aliases for x in first)
        default_keys = ['brand','model','year','condition','mileage_km','engine','transmission','drivetrain','color','paint_condition','sale_price','highlights','purchase_price_cny','notes']
        keys = [aliases.get(x, '') for x in first] if has_header else default_keys
        data_rows = rows[1:] if has_header else rows
        created, errors = [], []
        for idx, row in enumerate(data_rows, start=2 if has_header else 1):
            if not any((x or '').strip() for x in row):
                continue
            vals = {k: (row[i].strip() if i < len(row) else '') for i,k in enumerate(keys) if k}
            brand, model = vals.get('brand',''), vals.get('model','')
            if not (brand or model):
                errors.append(f'第 {idx} 行：缺少品牌/车型')
                continue
            cond_raw = vals.get('condition','').lower()
            condition = 'new' if cond_raw in ('new','新车','новый') else 'used'
            vals['condition'] = condition
            public_parts = [brand, model, vals.get('year',''), vals.get('mileage_km',''), vals.get('engine',''),
                            vals.get('transmission',''), vals.get('drivetrain',''), vals.get('color',''),
                            vals.get('paint_condition',''), vals.get('sale_price',''), vals.get('highlights','')]
            facts = ' | '.join(x for x in public_parts if x)
            code = app.state.db.create_vehicle(facts, '', '', None, 'once', False)
            app.state.db.save_vehicle_detail(code, **vals)
            created.append(code)
        links = ''.join(f'<li><a href="/garage/{escape(code)}">{escape(code)}</a></li>' for code in created)
        errs = ''.join(f'<li>{escape(e)}</li>' for e in errors)
        return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>导入结果</title></head><body style="font-family:Arial;max-width:800px;margin:30px auto;padding:0 16px"><h1>批量导入完成</h1><p>成功创建：<b>{len(created)}</b> 辆；跳过/错误：<b>{len(errors)}</b> 行。</p><p>新车辆默认：在售、自动投放关闭、仅一次。请上传对应照片后再启用投放。</p><ul>{links}</ul>{('<h3>需要检查</h3><ul>'+errs+'</ul>') if errors else ''}<p><a href="/garage">返回车辆车库</a></p></body></html>"""

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
        <a href="/garage">← 返回车库</a><h1>{x.code}</h1><p>状态：{escape(x.status)}</p><h3>车辆图片</h3><div>{gallery or "暂无图片"}</div><form method="post" action="/garage/{x.code}/photos" enctype="multipart/form-data"><label>单独上传车辆图片（最多10张，每张≤10MB）</label><input type="file" name="photos" multiple required accept="image/jpeg,image/png,image/webp"><button type="submit">上传图片</button></form>
        <form method="post" action="/garage/{x.code}/details"><h3>结构化车辆参数</h3><div style="display:grid;grid-template-columns:1fr 1fr;gap:8px"><input name="brand" value="{dv('brand')}" placeholder="品牌"><input name="model" value="{dv('model')}" placeholder="车型"><input name="year" value="{dv('year')}" placeholder="年份"><select name="condition"><option value="used">二手车</option><option value="new">新车</option></select><input name="mileage_km" value="{dv('mileage_km')}" placeholder="里程 km"><input name="engine" value="{dv('engine')}" placeholder="发动机"><input name="transmission" value="{dv('transmission')}" placeholder="变速箱"><input name="drivetrain" value="{dv('drivetrain')}" placeholder="驱动方式"><input name="color" value="{dv('color')}" placeholder="颜色"><input name="paint_condition" value="{dv('paint_condition')}" placeholder="漆面/车况"><input name="purchase_price_cny" value="{dv('purchase_price_cny')}" placeholder="中国采购价 CNY"><input name="sale_price" value="{dv('sale_price')}" placeholder="对外报价"></div><textarea name="highlights" rows="3" placeholder="配置亮点">{dv('highlights')}</textarea><textarea name="notes" rows="3" placeholder="内部备注">{dv('notes')}</textarea><button type="submit">保存车辆参数</button></form><form method="post" action="/garage/{x.code}/edit" enctype="multipart/form-data">
        <label>车辆关键参数</label><textarea name="facts" rows="7">{value}</textarea>
        <label>俄语发布文案</label><textarea name="caption" rows="9">{caption}</textarea>
        <div style="padding:12px;border:1px solid #ddd;border-radius:10px;margin:12px 0"><b>当前投放状态：</b>{'已开启' if x.auto_publish else '已关闭'}<br><b>下次发布时间：</b>{x.publish_at.replace(tzinfo=ZoneInfo('UTC')).astimezone(ZoneInfo(settings.timezone)).strftime('%Y-%m-%d %H:%M') if x.publish_at else '未设置'}<br><b>投放周期：</b>{escape(x.repeat_rule)}</div>
        <label>投放时间（{escape(settings.timezone)}）</label><input type="datetime-local" name="publish_at" value="{x.publish_at.replace(tzinfo=ZoneInfo('UTC')).astimezone(ZoneInfo(settings.timezone)).strftime('%Y-%m-%dT%H:%M') if x.publish_at else ''}">
        <label>投放周期</label><select name="repeat_rule"><option value="once" {'selected' if x.repeat_rule == 'once' else ''}>仅一次</option><option value="daily" {'selected' if x.repeat_rule == 'daily' else ''}>每天</option><option value="weekly" {'selected' if x.repeat_rule == 'weekly' else ''}>每周</option></select>
        <label><input type="checkbox" name="auto_publish" value="1" style="width:auto" {'checked' if x.auto_publish else ''}> 开启自动投放</label>
        <label>车辆状态</label><select name="status"><option value="available" {'selected' if x.status == 'available' else ''}>在售</option><option value="reserved" {'selected' if x.status == 'reserved' else ''}>已预订</option><option value="sold" {'selected' if x.status == 'sold' else ''}>已售</option></select>
        <button type="submit">保存修改</button></form><form method="post" action="/garage/{x.code}/duplicate"><button type="submit">复制车辆</button></form><form method="post" action="/garage/{x.code}/promotion"><input type="hidden" name="enabled" value="0"><button type="submit">停止推广</button></form><form method="post" action="/garage/{x.code}/promotion"><input type="hidden" name="enabled" value="1"><button type="submit">恢复推广</button></form><form method="post" action="/garage/{x.code}/publish-now"><button type="submit">立即发布到 Telegram</button></form><form method="post" action="/garage/{x.code}/generate-copy"><button type="submit">AI生成/重写俄语文案</button></form></body></html>"""

    @app.get('/garage/auto-schedule', response_class=HTMLResponse)
    def garage_auto_schedule(_=Depends(garage_auth)):
        return """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>库存自动排期</title>
        <style>body{font-family:Arial;max-width:850px;margin:30px auto;padding:0 16px}input,button{width:100%;padding:12px;margin:7px 0;box-sizing:border-box}.tip{background:#f5f5f5;padding:14px;border-radius:10px}</style></head><body>
        <a href="/garage">← 返回车库</a><h1>批量俄语文案 + 自动排期</h1><div class="tip">只处理：在售 + 已有照片 + 尚未设置发布时间的车辆。系统先生成/保留俄语文案，再按莫斯科时间分配发布时段。不会自动发布无照片车辆。</div>
        <form method="post" action="/garage/auto-schedule"><label>每天最多发布几辆</label><input type="number" name="posts_per_day" min="1" max="10" value="4" required><label>发布时间（莫斯科时间，逗号分隔）</label><input name="times" value="09:00,12:30,16:00,19:30" required><label>最多处理库存数量</label><input type="number" name="limit" min="1" max="100" value="30" required><button type="submit">生成俄语文案并自动排期</button></form></body></html>"""

    @app.post('/garage/auto-schedule', response_class=HTMLResponse)
    async def garage_auto_schedule_run(posts_per_day: int = Form(4), times: str = Form('09:00,12:30,16:00,19:30'), limit: int = Form(30), _=Depends(garage_auth)):
        from datetime import timedelta
        slots = []
        for raw in times.split(','):
            raw = raw.strip()
            try:
                hh, mm = [int(x) for x in raw.split(':', 1)]
                if 0 <= hh <= 23 and 0 <= mm <= 59:
                    slots.append((hh, mm))
            except Exception:
                pass
        slots = slots[:max(1, min(posts_per_day, 10))]
        if not slots:
            return HTMLResponse('发布时间格式不正确，例如 09:00,12:30,16:00,19:30', status_code=400)
        candidates = []
        for x in app.state.db.vehicles(max(100, limit)):
            photos = [p for p in (x.photo_file_ids or '').split('|') if p] or ([x.photo_file_id] if x.photo_file_id else [])
            if x.status == 'available' and photos and x.publish_at is None:
                candidates.append(x)
            if len(candidates) >= max(1, min(limit, 100)):
                break
        tz = ZoneInfo(settings.timezone)
        cursor_day = datetime.now(tz).date()
        now_local = datetime.now(tz)
        planned, failed = [], []
        slot_index = 0
        for x in reversed(candidates):
            while True:
                day_offset = slot_index // len(slots)
                hh, mm = slots[slot_index % len(slots)]
                local_dt = datetime.combine(cursor_day + timedelta(days=day_offset), datetime.min.time(), tzinfo=tz).replace(hour=hh, minute=mm)
                slot_index += 1
                if local_dt > now_local + timedelta(minutes=2):
                    break
            try:
                caption = (x.caption or '').strip()
                if not caption:
                    detail = app.state.db.vehicle_detail(x.code)
                    caption = await app.state.service.content.structured_vehicle_listing(detail, x.facts)
                    app.state.db.update_vehicle(x.code, caption=caption)
                scheduled = local_dt.astimezone(ZoneInfo('UTC'))
                app.state.db.update_vehicle(x.code, publish_at=scheduled, repeat_rule='once', auto_publish=True)
                planned.append((x.code, local_dt.strftime('%Y-%m-%d %H:%M')))
            except Exception as exc:
                failed.append(f'{x.code}: {type(exc).__name__}')
        rows = ''.join(f'<li><a href="/garage/{escape(code)}">{escape(code)}</a> → {escape(at)} MSK</li>' for code,at in planned)
        errs = ''.join(f'<li>{escape(e)}</li>' for e in failed)
        return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>自动排期结果</title></head><body style="font-family:Arial;max-width:850px;margin:30px auto;padding:0 16px"><h1>自动排期完成</h1><p>成功安排：<b>{len(planned)}</b> 辆；失败：<b>{len(failed)}</b> 辆。</p><p>所有成功车辆已开启自动投放，周期为仅一次。</p><ul>{rows}</ul>{('<h3>需要检查</h3><ul>'+errs+'</ul>') if failed else ''}<p><a href="/garage">返回车库</a></p></body></html>"""

    @app.get('/garage/batch-photos', response_class=HTMLResponse)
    def garage_batch_photos(_=Depends(garage_auth)):
        return """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>批量匹配车辆照片</title>
        <style>body{font-family:Arial;max-width:900px;margin:30px auto;padding:0 16px}input,button{width:100%;padding:12px;margin:8px 0;box-sizing:border-box}.tip{background:#f5f5f5;padding:14px;border-radius:10px}</style></head><body>
        <a href="/garage">← 返回车库</a><h1>批量匹配车辆照片</h1><div class="tip"><b>图片命名规则</b><br>FF-00003-01.jpg<br>FF-00003-02.jpg<br>FF-00004-01.jpg<br><br>系统读取文件名前面的 FF-xxxxx，自动归入对应车辆。每辆最多保存10张，第一张作为封面。支持 JPG/JPEG/PNG/WEBP，每张≤10MB。</div>
        <form method="post" action="/garage/batch-photos" enctype="multipart/form-data"><input type="file" name="photos" multiple required accept="image/jpeg,image/png,image/webp"><button type="submit">上传并自动匹配</button></form></body></html>"""

    @app.post('/garage/batch-photos', response_class=HTMLResponse)
    async def garage_batch_photos_upload(photos: list[UploadFile] = File(default=[]), _=Depends(garage_auth)):
        import re
        grouped, errors = {}, []
        for photo in photos:
            if not photo.filename:
                continue
            match = re.search(r'(?i)(FF-\d{5})', Path(photo.filename).name)
            if not match:
                errors.append(f'{photo.filename}：文件名没有 FF-xxxxx')
                continue
            code = match.group(1).upper()
            x = app.state.db.vehicle(code)
            if not x:
                errors.append(f'{photo.filename}：找不到车辆 {code}')
                continue
            data = await photo.read()
            if not data:
                errors.append(f'{photo.filename}：空文件')
                continue
            if len(data) > 10 * 1024 * 1024:
                errors.append(f'{photo.filename}：超过10MB')
                continue
            suffix = Path(photo.filename).suffix.lower()
            if suffix not in ('.jpg','.jpeg','.png','.webp'):
                errors.append(f'{photo.filename}：格式不支持')
                continue
            filename = f'{code}-{uuid.uuid4().hex}{suffix}'
            (media_dir / filename).write_bytes(data)
            grouped.setdefault(code, []).append('local:' + filename)
        updated = []
        for code, stored in grouped.items():
            x = app.state.db.vehicle(code)
            existing = [p for p in (x.photo_file_ids or '').split('|') if p] or ([x.photo_file_id] if x.photo_file_id else [])
            merged = (existing + stored)[:10]
            app.state.db.set_vehicle_photos(code, merged)
            updated.append((code, len(stored), len(merged)))
        rows = ''.join(f'<li><a href="/garage/{escape(code)}">{escape(code)}</a>：本次匹配 {added} 张，当前共 {total} 张</li>' for code,added,total in updated)
        errs = ''.join(f'<li>{escape(e)}</li>' for e in errors)
        return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>照片匹配结果</title></head><body style="font-family:Arial;max-width:900px;margin:30px auto;padding:0 16px"><h1>批量照片处理完成</h1><p>成功匹配车辆：<b>{len(updated)}</b> 辆；需要检查：<b>{len(errors)}</b> 个文件。</p><ul>{rows}</ul>{('<h3>未匹配/错误</h3><ul>'+errs+'</ul>') if errors else ''}<p><a href="/garage/batch-photos">继续上传照片</a>　<a href="/garage">返回车库</a></p></body></html>"""

    @app.post('/garage/{code}/photos')
    async def garage_photo_upload(code: str, photos: list[UploadFile] = File(default=[]), _=Depends(garage_auth)):
        x = app.state.db.vehicle(code)
        if not x:
            return HTMLResponse('车辆不存在', status_code=404)
        stored = []
        for photo in photos[:10]:
            if not photo.filename:
                continue
            data = await photo.read()
            if not data:
                continue
            if len(data) > 10 * 1024 * 1024:
                return HTMLResponse('单张图片不能超过10MB', status_code=400)
            suffix = Path(photo.filename).suffix.lower()
            if suffix not in ('.jpg', '.jpeg', '.png', '.webp'):
                return HTMLResponse('仅支持 JPG/PNG/WEBP', status_code=400)
            filename = f'{code}-{uuid.uuid4().hex}{suffix}'
            (media_dir / filename).write_bytes(data)
            stored.append('local:' + filename)
        if not stored:
            return HTMLResponse('没有收到有效图片，请选择 JPG/PNG/WEBP 后重试', status_code=400)
        existing = [p for p in (x.photo_file_ids or '').split('|') if p] or ([x.photo_file_id] if x.photo_file_id else [])
        all_photos = (existing + stored)[:10]
        app.state.db.set_vehicle_photos(code, all_photos)
        return RedirectResponse('/garage/' + code, status_code=303)

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
                # Pass open file objects directly. python-telegram-bot then builds
                # attach:// multipart references correctly for media groups.
                from telegram import InputMediaPhoto
                handles = []
                try:
                    media = []
                    for i, p in enumerate(photos[:10]):
                        v = media_value(p)
                        if isinstance(v, Path):
                            fh = v.open('rb')
                            handles.append(fh)
                            v = fh
                        media.append(InputMediaPhoto(media=v, caption=caption[:1024] if i == 0 else None))
                    sent_group = await app.state.service.application.bot.send_media_group(chat_id=target, media=media)
                    sent = sent_group[0]
                finally:
                    for fh in handles:
                        fh.close()
            elif photos:
                v = media_value(photos[0])
                if isinstance(v, Path):
                    with v.open('rb') as fh:
                        sent = await app.state.service.application.bot.send_photo(chat_id=target, photo=fh, caption=caption[:1024])
                else:
                    sent = await app.state.service.application.bot.send_photo(chat_id=target, photo=v, caption=caption[:1024])
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
