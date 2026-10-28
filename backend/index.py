# Приём заявок с сайта фестиваля «От Страха к Любви» → сообщение в рабочую группу Telegram и/или Max.
# Облачная функция Yandex Cloud, Python 3.12, без сторонних библиотек. Точка входа: index.handler
#
# Переменные окружения (задаются в настройках функции, в код не вписываются).
# Канал включается, когда задан его токен; заявка уходит во все включённые каналы.
#   TG_TOKEN         токен бота Telegram (выдаёт @BotFather)
#   TG_CHAT_ID       id группы Telegram, куда бот пишет заявки (как узнать — backend/README.md, шаг 4)
#   TG_API           необязательно: другой адрес API Telegram, если прямой недоступен; по умолчанию https://api.telegram.org
#   MAX_TOKEN        токен бота Max (business.max.ru → Чат-боты → ⋮ → Настройки)
#   MAX_CHAT_ID      id группы Max
#   ALLOWED_ORIGINS  с каких сайтов принимать заявки, через запятую; по умолчанию https://fest.mirra-spb.ru
#
# Сайт шлёт POST с JSON (Content-Type: text/plain — так браузер не делает лишний CORS-запрос).
# Каждая заявка дополнительно пишется в журнал функции (Cloud Logging) — на случай, если мессенджер не ответит.

import base64
import datetime
import html
import json
import os
import re
import ssl
import urllib.error
import urllib.request

TG_TOKEN = os.environ.get('TG_TOKEN', '').strip()
TG_CHAT_ID = os.environ.get('TG_CHAT_ID', '').strip()
TG_API = os.environ.get('TG_API', 'https://api.telegram.org').strip().rstrip('/')
MAX_TOKEN = os.environ.get('MAX_TOKEN', '').strip()
MAX_CHAT_ID = os.environ.get('MAX_CHAT_ID', '').strip()
ORIGINS = [o.strip().rstrip('/') for o in os.environ.get('ALLOWED_ORIGINS', 'https://fest.mirra-spb.ru').split(',') if o.strip()]

# Основной адрес API Max подписан сертификатом Минцифры — его нет в стандартном списке доверенных,
# поэтому кладём корневой и промежуточный сертификаты рядом с кодом. Если не выйдет — запасной адрес.
MAX_HOSTS = ['https://platform-api2.max.ru', 'https://platform-api.max.ru']
_CA = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'russian_trusted_ca.pem')


def _ctx(host):
    ctx = ssl.create_default_context()
    if 'api2.max.ru' in host and os.path.exists(_CA):
        ctx.load_verify_locations(_CA)
    return ctx


def _json_request(url, method, body, headers, host):
    data = json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=dict(headers, **{'Content-Type': 'application/json'}))
    with urllib.request.urlopen(req, timeout=8, context=_ctx(host)) as r:
        return json.loads(r.read().decode('utf-8') or '{}')


# ---------- Telegram ----------

def tg_api(name, body=None):
    """Метод Bot API Telegram. Возвращает поле result; при ошибке — исключение с описанием от Telegram."""
    try:
        res = _json_request('%s/bot%s/%s' % (TG_API, TG_TOKEN, name), 'POST', body or {}, {}, TG_API)
    except urllib.error.HTTPError as e:
        res = json.loads(e.read().decode('utf-8', 'replace') or '{}')
    except Exception as e:
        raise RuntimeError(('Telegram недоступен: %r' % e).replace(TG_TOKEN, '***'))   # токен — часть адреса, в журнал не пускаем
    if not res.get('ok'):
        err = RuntimeError('Telegram %s: %s %s' % (name, res.get('error_code'), res.get('description')))
        err.params = res.get('parameters') or {}
        raise err
    return res.get('result')


def tg_send(text):
    body = {'chat_id': TG_CHAT_ID, 'text': text, 'parse_mode': 'HTML', 'disable_web_page_preview': True}
    try:
        tg_api('sendMessage', body)
    except RuntimeError as e:
        # группу превратили в супергруппу — у неё новый id; отправляем туда и подсказываем в журнале
        new_id = getattr(e, 'params', {}).get('migrate_to_chat_id')
        if not new_id:
            raise
        print(json.dumps({'event': 'tg_chat_migrated', 'new TG_CHAT_ID': new_id}, ensure_ascii=False))
        tg_api('sendMessage', dict(body, chat_id=new_id))


def tg_setup():
    me = tg_api('getMe')
    chats = {}
    for u in tg_api('getUpdates', {'limit': 100, 'timeout': 0}) or []:
        for key in ('my_chat_member', 'message', 'channel_post'):
            chat = (u.get(key) or {}).get('chat')
            if chat and chat.get('type') in ('group', 'supergroup'):
                chats[chat['id']] = '«%s»' % chat.get('title', '')
    return {'бот': '@' + (me.get('username') or '?'),
            'группы': [{'TG_CHAT_ID': cid, 'название': t} for cid, t in chats.items()],
            'подсказка': 'Если список пуст: добавьте бота в группу (или удалите и добавьте заново), напишите в группе /start@ник_бота и запустите тест ещё раз.'}


# ---------- Max ----------

def max_api(method, path, body=None):
    """Запрос к API Max; пробует основной адрес, потом запасной. Возвращает разобранный JSON."""
    last = None
    for host in MAX_HOSTS:
        try:
            return _json_request(host + path, method, body, {'Authorization': MAX_TOKEN}, host)
        except urllib.error.HTTPError as e:
            # API ответил — дело не в адресе, второй пробовать бессмысленно
            raise RuntimeError('Max %s %s: HTTP %s %s' % (method, path, e.code, e.read()[:300].decode('utf-8', 'replace')))
        except Exception as e:
            last = e
    raise RuntimeError('Max недоступен: %r' % last)


def max_send(text):
    max_api('POST', '/messages?chat_id=%s&disable_link_preview=true' % MAX_CHAT_ID, {'text': text, 'format': 'html'})


def max_setup():
    me = max_api('GET', '/me')
    upd = max_api('GET', '/updates?limit=100&timeout=0')
    chats = {}
    for u in upd.get('updates', []):
        t = u.get('update_type')
        if t == 'bot_added':
            chats[u.get('chat_id')] = 'бота добавил(а) ' + (u.get('user') or {}).get('first_name', '?')
        elif t == 'message_created':
            rcp = ((u.get('message') or {}).get('recipient') or {})
            if rcp.get('chat_type') == 'chat':
                chats.setdefault(rcp.get('chat_id'), 'сообщение в группе')
    for cid in list(chats):
        try:
            chats[cid] += ' · «%s»' % max_api('GET', '/chats/%s' % cid).get('title', '')
        except Exception:
            pass
    return {'бот': me.get('first_name') or me.get('username'),
            'группы': [{'MAX_CHAT_ID': cid, 'что видно': info} for cid, info in chats.items()],
            'подсказка': 'Если список пуст: сделайте бота администратором группы, напишите в группе любое сообщение и запустите тест ещё раз.'}


# ---------- общее ----------

def channels():
    """Включённые каналы: (название, есть ли id группы, отправка, настройка)."""
    out = []
    if TG_TOKEN:
        out.append(('Telegram', bool(TG_CHAT_ID), tg_send, tg_setup))
    if MAX_TOKEN:
        out.append(('Max', bool(MAX_CHAT_ID), max_send, max_setup))
    return out


def reply(code, obj, origin=None):
    headers = {'Content-Type': 'application/json; charset=utf-8'}
    if origin:
        headers.update({'Access-Control-Allow-Origin': origin, 'Vary': 'Origin',
                        'Access-Control-Allow-Methods': 'POST, OPTIONS', 'Access-Control-Allow-Headers': 'Content-Type'})
    return {'statusCode': code, 'headers': headers, 'body': json.dumps(obj, ensure_ascii=False)}


def clean(v, n):
    """Строка без управляющих символов, не длиннее n."""
    v = re.sub(r'[\x00-\x1f\x7f]+', ' ', str(v or '')).strip()
    return v[:n]


def format_request(d):
    rows = [('Имя', d['name']), ('Телефон', d['phone']), ('Telegram / Max', d['messenger']),
            ('Когда придёт', d['plan']), ('Сколько человек', d['count']), ('Особенно хочет на', d['mk']),
            ('Рассказывать о мероприятиях центра', 'да' if d['news'] else 'нет')]
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3))).strftime('%d.%m.%Y %H:%M')
    lines = ['🌿 <b>Новая заявка на фестиваль</b>', '']
    lines += ['<b>%s:</b> %s' % (k, html.escape(v)) for k, v in rows if v]
    lines += ['', '<i>%s по Москве · с сайта fest.mirra-spb.ru</i>' % now]
    return '\n'.join(lines)


def handler(event, context):
    headers = {k.lower(): v for k, v in (event.get('headers') or {}).items()}
    origin = (headers.get('origin') or '').rstrip('/')
    allowed = origin if origin in ORIGINS else None
    method = event.get('httpMethod', 'POST')

    if method == 'OPTIONS':
        return reply(204 if allowed else 403, {}, allowed)

    body = event.get('body') or ''
    if event.get('isBase64Encoded'):
        body = base64.b64decode(body).decode('utf-8', 'replace')
    try:
        raw = json.loads(body) if body else {}
    except ValueError:
        return reply(400, {'ok': False, 'error': 'bad json'}, allowed)

    chs = channels()
    if not chs:
        return reply(500, {'ok': False, 'error': 'не задан ни TG_TOKEN, ни MAX_TOKEN'}, allowed)

    # Настройка из консоли Yandex Cloud: тело запроса {"setup": true}.
    # Показывает группы бота — только для каналов, у которых id группы ещё не задан.
    if raw.get('setup') is True:
        todo = [c for c in chs if not c[1]]
        if not todo:
            return reply(403, {'ok': False, 'error': 'id групп уже заданы — режим настройки выключен'})
        res = {}
        for name, _, _, setup in todo:
            try:
                res[name] = setup()
            except Exception as e:
                res[name] = {'ошибка': str(e)}
        return reply(200, res)

    if not allowed:
        return reply(403, {'ok': False, 'error': 'origin'})

    # Ловушки для спам-ботов: скрытое поле заполнено или форма отправлена быстрее, чем за 3 секунды.
    # Отвечаем «принято», чтобы бот не подбирал обход, но никуда не пересылаем.
    if raw.get('website') or (isinstance(raw.get('ms'), (int, float)) and raw['ms'] < 3000):
        print(json.dumps({'event': 'spam', 'origin': origin}, ensure_ascii=False))
        return reply(200, {'ok': True}, allowed)

    d = {'name': clean(raw.get('name'), 80), 'phone': clean(raw.get('phone'), 20),
         'messenger': clean(raw.get('messenger'), 64), 'plan': clean(raw.get('plan'), 40),
         'count': clean(raw.get('count'), 20), 'mk': clean(raw.get('mk'), 120),
         'news': raw.get('news') is True}
    digits = re.sub(r'\D', '', d['phone'])
    if not d['name'] or not 10 <= len(digits) <= 15 or raw.get('consent') is not True:
        return reply(400, {'ok': False, 'error': 'проверьте имя, телефон и согласие'}, allowed)

    print(json.dumps({'event': 'request', **d}, ensure_ascii=False))
    text, delivered = format_request(d), []
    for name, has_chat, send, _ in chs:
        if not has_chat:
            continue
        try:
            send(text)
            delivered.append(name)
        except Exception as e:
            print(json.dumps({'event': 'send_failed', 'channel': name, 'error': str(e)}, ensure_ascii=False))
    if not delivered:
        return reply(502, {'ok': False, 'error': 'не удалось переслать заявку'}, allowed)
    return reply(200, {'ok': True}, allowed)
