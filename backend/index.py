# Приём заявок с сайта фестиваля «От Страха к Любви» → сообщение в беседу ВКонтакте, а если не дошло — письмо на почту центра
# (а также Telegram и Max, если они когда-нибудь станут доступны).
# Облачная функция Yandex Cloud, Python 3.12, без сторонних библиотек. Точка входа: index.handler
#
# Переменные окружения (задаются в настройках функции, в код не вписываются).
# Канал включается, когда заданы его переменные. Заявка уходит в первый по очереди (ВКонтакте),
# а в следующий (почта) — только если предыдущий не доставил.
#   MAIL_USER        ящик Яндекс Почты, с которого уходят письма, например zayavki@yandex.ru
#   MAIL_PASSWORD    пароль приложения для этого ящика (id.yandex.ru → Безопасность → Пароли приложений)
#   MAIL_TO          куда слать, через запятую; по умолчанию — на тот же MAIL_USER
#   VK_TOKEN         ключ доступа сообщества ВКонтакте (Управление → Работа с API), с правом «сообщения сообщества»
#   VK_PEER_ID       id беседы, куда пишет сообщество, вида 2000000001 (как узнать — backend/README.md)
#   TG_TOKEN, TG_CHAT_ID, TG_API   Telegram — из Яндекс Облака сейчас недоступен, даже через посредника
#   MAX_TOKEN, MAX_CHAT_ID         Max — нужен проверенный профиль организации
#   ALLOWED_ORIGINS  с каких сайтов принимать заявки, через запятую; по умолчанию https://fest.mirra-spb.ru
#
# Таблица заявок на Яндекс Диске центра (не канал: пишется всегда, после отправки в ВК или почту):
#   DISK_TOKEN       OAuth-токен Яндекс Диска с доступом только к папке приложения (как получить — backend/README.md)
#   DISK_PATH        файл таблицы; по умолчанию app:/Заявки на фестиваль.csv — это «Приложения/<имя приложения>/» на Диске
#
# Сайт шлёт POST с JSON (Content-Type: text/plain — так браузер не делает лишний CORS-запрос).
# Каждая заявка дополнительно пишется в журнал функции (Cloud Logging) — на случай, если канал не ответит.

import base64
import csv
import datetime
import io
import html
import json
import os
import random
import re
import smtplib
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from email.utils import formataddr

env = lambda k, d='': os.environ.get(k, d).strip()
MAIL_USER, MAIL_PASSWORD = env('MAIL_USER'), env('MAIL_PASSWORD')
MAIL_TO = [a.strip() for a in env('MAIL_TO', MAIL_USER).split(',') if a.strip()]
MAIL_HOST, MAIL_PORT = env('MAIL_HOST', 'smtp.yandex.ru'), int(env('MAIL_PORT', '465'))
VK_TOKEN, VK_PEER_ID = env('VK_TOKEN'), env('VK_PEER_ID')
TG_TOKEN, TG_CHAT_ID = env('TG_TOKEN'), env('TG_CHAT_ID')
TG_API = env('TG_API', 'https://api.telegram.org').rstrip('/')
MAX_TOKEN, MAX_CHAT_ID = env('MAX_TOKEN'), env('MAX_CHAT_ID')
DISK_TOKEN, DISK_PATH = env('DISK_TOKEN'), env('DISK_PATH', 'app:/Заявки на фестиваль.csv')
ORIGINS = [o.strip().rstrip('/') for o in env('ALLOWED_ORIGINS', 'https://fest.mirra-spb.ru').split(',') if o.strip()]
TIMEOUT = 8   # секунд на один запрос к внешнему сервису; таймаут самой функции — 20 с

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
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ctx(host)) as r:
        return json.loads(r.read().decode('utf-8') or '{}')


def _hide(text, *secrets):
    """Убрать секреты из текста ошибки, прежде чем он попадёт в журнал или ответ."""
    for s in secrets:
        if s:
            text = text.replace(s, '***')
    return text


# ---------- почта ----------

def mail_send(req):
    msg = EmailMessage()
    msg['Subject'] = 'Заявка на фестиваль: %s, %s' % (req['name'], req['phone'])
    msg['From'] = formataddr(('Сайт фестиваля «От Страха к Любви»', MAIL_USER))
    msg['To'] = ', '.join(MAIL_TO)
    msg.set_content(as_text(req))
    msg.add_alternative('<div style="font:15px/1.5 Arial,sans-serif">%s</div>' % as_html(req).replace('\n', '<br>\n'), subtype='html')
    try:
        with smtplib.SMTP_SSL(MAIL_HOST, MAIL_PORT, timeout=TIMEOUT, context=ssl.create_default_context()) as s:
            s.login(MAIL_USER, MAIL_PASSWORD)
            s.send_message(msg)
    except Exception as e:
        raise RuntimeError(_hide('Почта: %r' % e, MAIL_PASSWORD))


def mail_setup():
    """Проверить вход в ящик, ничего не отправляя."""
    try:
        with smtplib.SMTP_SSL(MAIL_HOST, MAIL_PORT, timeout=TIMEOUT, context=ssl.create_default_context()) as s:
            s.login(MAIL_USER, MAIL_PASSWORD)
    except Exception as e:
        raise RuntimeError(_hide('Почта: %r' % e, MAIL_PASSWORD))
    return {'вход в ящик': 'выполнен', 'получателей': len(MAIL_TO)}   # адреса не показываем: ответ видит любой, кто вызовет настройку


# ---------- ВКонтакте ----------

def vk_api(name, **params):
    """Метод API ВКонтакте с ключом сообщества. Возвращает поле response; при ошибке — исключение с текстом от ВК."""
    data = urllib.parse.urlencode(dict(params, access_token=VK_TOKEN, v='5.199')).encode('utf-8')
    try:
        with urllib.request.urlopen('https://api.vk.com/method/' + name, data=data, timeout=TIMEOUT) as r:
            res = json.loads(r.read().decode('utf-8'))
    except Exception as e:
        raise RuntimeError(_hide('ВКонтакте недоступен: %r' % e, VK_TOKEN))
    if 'error' in res:
        raise RuntimeError('ВКонтакте %s: %s %s' % (name, res['error'].get('error_code'), res['error'].get('error_msg')))
    return res.get('response')


def vk_send(req):
    vk_api('messages.send', peer_id=VK_PEER_ID, random_id=random.randint(1, 2 ** 31 - 1),
           message=as_text(req), dont_parse_links=1)


def vk_setup():
    res = vk_api('messages.getConversations', count=100, filter='all')
    chats = [{'VK_PEER_ID': c['conversation']['peer']['id'],
              'название': '«%s»' % (c['conversation'].get('chat_settings') or {}).get('title', '')}
             for c in (res or {}).get('items', []) if c['conversation']['peer'].get('type') == 'chat']
    return {'беседы': chats,
            'подсказка': 'Если список пуст: проверьте, что сообщество добавлено в беседу, и напишите в беседе любое сообщение.'}


# ---------- Telegram ----------

def tg_api(name, body=None):
    """Метод Bot API Telegram. Возвращает поле result; при ошибке — исключение с описанием от Telegram."""
    try:
        res = _json_request('%s/bot%s/%s' % (TG_API, TG_TOKEN, name), 'POST', body or {}, {}, TG_API)
    except urllib.error.HTTPError as e:
        res = json.loads(e.read().decode('utf-8', 'replace') or '{}')
    except Exception as e:
        raise RuntimeError(_hide('Telegram недоступен: %r' % e, TG_TOKEN))   # токен — часть адреса, в журнал не пускаем
    if not res.get('ok'):
        err = RuntimeError('Telegram %s: %s %s' % (name, res.get('error_code'), res.get('description')))
        err.params = res.get('parameters') or {}
        raise err
    return res.get('result')


def tg_send(req):
    body = {'chat_id': TG_CHAT_ID, 'text': as_html(req), 'parse_mode': 'HTML', 'disable_web_page_preview': True}
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


def max_send(req):
    max_api('POST', '/messages?chat_id=%s&disable_link_preview=true' % MAX_CHAT_ID, {'text': as_html(req), 'format': 'html'})


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


# ---------- таблица на Яндекс Диске ----------

DISK_API = 'https://cloud-api.yandex.net/v1/disk/resources'
TABLE_HEAD = ['Дата и время', 'Имя', 'Телефон', 'Telegram / Max', 'Когда придёт', 'Сколько человек',
              'Особенно хочет на', 'Рассказывать о мероприятиях', 'Куда пришла заявка']


def _cell(v):
    # Excel считает формулой ячейку, которая начинается с = + - @ — и в лучшем случае покажет ошибку
    v = str(v)
    return "'" + v if v[:1] in ('=', '+', '-', '@') else v


def _phone(p):
    """Российский номер — в виде 8 (900) 123-45-67: так Excel не превратит его в число или формулу."""
    dg = re.sub(r'\D', '', p)
    if len(dg) == 10 and dg[0] == '9':
        dg = '8' + dg
    if len(dg) == 11 and dg[0] in '78':
        return '8 (%s) %s-%s-%s' % (dg[1:4], dg[4:7], dg[7:9], dg[9:])
    return p


def table_row(d, where):
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3))).strftime('%d.%m.%Y %H:%M')
    return [now, d['name'], _phone(d['phone']), d['messenger'], d['plan'], d['count'], d['mk'],
            'да' if d['news'] else 'нет', where]


def _csv(rows):
    buf = io.StringIO()
    csv.writer(buf, delimiter=';', lineterminator='\r\n').writerows([[_cell(v) for v in r] for r in rows])
    return buf.getvalue()


def disk_append(row, timeout):
    """Дописать строку в CSV на Диске: скачать, добавить, загрузить обратно.
    CSV в UTF-8 с BOM и разделителем «;» — так его сразу правильно открывает русский Excel."""
    auth = {'Authorization': 'OAuth ' + DISK_TOKEN}
    def link(kind, extra=''):
        req = urllib.request.Request('%s/%s?path=%s%s' % (DISK_API, kind, urllib.parse.quote(DISK_PATH), extra), headers=auth)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8'))['href']
    try:
        try:
            with urllib.request.urlopen(link('download'), timeout=timeout) as r:
                old = r.read().decode('utf-8-sig')   # не UTF-8 — ошибка, и файл не перезаписываем
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            old = _csv([TABLE_HEAD])                 # таблицы ещё нет — начинаем с заголовка
        if old and not old.endswith('\n'):
            old += '\r\n'
        data = ('﻿' + old + _csv([row])).encode('utf-8')
        req = urllib.request.Request(link('upload', '&overwrite=true'), data=data, method='PUT')
        with urllib.request.urlopen(req, timeout=timeout):
            pass
    except Exception as e:
        raise RuntimeError(_hide('Диск: %r' % e, DISK_TOKEN))


# ---------- общее ----------

def channels():
    """Включённые каналы в порядке очереди: (название, задан ли адресат, отправка, настройка).
    Заявка уходит в первый; следующий пробуется, только если предыдущий не доставил."""
    out = []
    if VK_TOKEN:
        out.append(('ВКонтакте', bool(VK_PEER_ID), vk_send, vk_setup))
    if MAIL_USER and MAIL_PASSWORD:
        out.append(('Почта', bool(MAIL_TO), mail_send, mail_setup))
    if TG_TOKEN:
        out.append(('Telegram', bool(TG_CHAT_ID), tg_send, tg_setup))
    if MAX_TOKEN:
        out.append(('Max', bool(MAX_CHAT_ID), max_send, max_setup))
    return out


def run_all(jobs):
    """Выполнить задачи одновременно; вернуть {название: результат или исключение}."""
    def safe(f):
        try:
            return f()
        except Exception as e:
            return e
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
        futures = {name: pool.submit(safe, f) for name, f in jobs}
        return {name: fut.result() for name, fut in futures.items()}


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


def _rows(d):
    rows = [('Имя', d['name']), ('Телефон', d['phone']), ('Telegram / Max', d['messenger']),
            ('Когда придёт', d['plan']), ('Сколько человек', d['count']), ('Особенно хочет на', d['mk']),
            ('Рассказывать о мероприятиях центра', 'да' if d['news'] else 'нет')]
    return [(k, v) for k, v in rows if v]


def _stamp():
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3))).strftime('%d.%m.%Y %H:%M')
    return '%s по Москве · с сайта fest.mirra-spb.ru' % now


def as_html(d):
    lines = ['🌿 <b>Новая заявка на фестиваль</b>', '']
    lines += ['<b>%s:</b> %s' % (k, html.escape(v)) for k, v in _rows(d)]
    return '\n'.join(lines + ['', '<i>%s</i>' % _stamp()])


def as_text(d):
    lines = ['🌿 Новая заявка на фестиваль', '']
    lines += ['%s: %s' % (k, v) for k, v in _rows(d)]
    return '\n'.join(lines + ['', _stamp()])


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
        return reply(500, {'ok': False, 'error': 'не включён ни один канал: задайте переменные почты или ВКонтакте'}, allowed)

    # Настройка из консоли Yandex Cloud: тело запроса {"setup": true}.
    # Проверяет каналы и показывает беседы/группы — пока хотя бы у одного мессенджера не задан адресат
    # (или мессенджеров нет вовсе, только почта).
    if raw.get('setup') is True:
        messengers = [has for n, has, _, _ in chs if n != 'Почта']
        if messengers and all(messengers):
            return reply(403, {'ok': False, 'error': 'все адресаты уже заданы — режим настройки выключен; проверяйте тестовой заявкой с сайта'})
        res = run_all([(n, setup) for n, _, _, setup in chs])
        return reply(200, {n: ({'ошибка': str(r)} if isinstance(r, Exception) else r) for n, r in res.items()})

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

    started = time.monotonic()
    print(json.dumps({'event': 'request', **d}, ensure_ascii=False))
    where = None
    for n, has, send, _ in chs:
        if not has:
            continue
        try:
            send(d)
            where = n
            print(json.dumps({'event': 'delivered', 'channel': n}, ensure_ascii=False))
            break
        except Exception as e:
            print(json.dumps({'event': 'send_failed', 'channel': n, 'error': str(e)}, ensure_ascii=False))

    # Таблица — после отправки и только в остаток времени: функцию обрывают на 20-й секунде,
    # а заявка уже доставлена, и гость не должен увидеть ошибку из-за Диска.
    left = 18 - (time.monotonic() - started)
    if DISK_TOKEN and left > 2:
        try:
            disk_append(table_row(d, where or 'не доставлена'), timeout=min(5, left / 4))
            print(json.dumps({'event': 'table_saved'}, ensure_ascii=False))
        except Exception as e:
            print(json.dumps({'event': 'table_failed', 'error': str(e)}, ensure_ascii=False))

    if where:
        return reply(200, {'ok': True}, allowed)
    return reply(502, {'ok': False, 'error': 'не удалось переслать заявку'}, allowed)
