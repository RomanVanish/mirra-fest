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
#   REF_CODES        кодовые слова спикеров для поля «Кто пригласил»: слово=имя через точку с запятой,
#                    например «Опора=Галина Власова; Зеркало=Инна Кочурова» (полный список — backend/README.md).
#                    Регистр, «ё» и пробелы не важны, русское слово совпадает со своей латинской записью: Опора = opora.
#                    Слово из списка в заявке и таблице заменяется именем спикера, остальное пишется как есть
#
# Таблица заявок на Яндекс Диске центра (не канал: пишется всегда, после отправки в ВК или почту):
#   DISK_TOKEN       OAuth-токен Яндекс Диска (как получить — backend/README.md)
#   DISK_PATH        файл таблицы, например disk:/__Клиенты/Заявки фестиваля/Заявки на фестиваль Душа.csv
#                    (токену нужен доступ ко всему Диску); по умолчанию app:/Заявки на фестиваль.csv —
#                    папка «Приложения/<имя приложения>/», для неё хватает доступа только к папке приложения
#
# Оплата билетов — интернет-эквайринг Т-Банка (включается, когда заданы обе переменные; нужна и таблица на Диске):
#   TBANK_TERMINAL   номер терминала из кабинета Т-Бизнеса (тестовый — с приставкой DEMO)
#   TBANK_PASSWORD   пароль этого терминала
#   TICKET_PRICE     цена билета в рублях, по умолчанию 2888
#   TBANK_TAXATION   система налогообложения для чека (usn_income или usn_income_outcome) — задать, когда к магазину
#                    подключена онлайн-касса: тогда в каждый платёж добавляется чек. TBANK_TAX — НДС (по умолчанию none),
#                    TBANK_PAYMENT_METHOD — признак расчёта (по умолчанию full_payment)
#   TBANK_NOTIFY_URL адрес этой функции для уведомлений банка, по умолчанию — нынешний адрес функции
# Гость заполняет форму → функция создаёт платёж и отдаёт сайту ссылку на страницу банка → после оплаты банк присылает
# уведомление сюда же → «✅ Оплачено» в ВК и отметка в таблице. Таблица служит и списком оплат: по одному телефону
# принимается одна оплата.
#
# Сайт шлёт POST с JSON (Content-Type: text/plain — так браузер не делает лишний CORS-запрос).
# Каждая заявка дополнительно пишется в журнал функции (Cloud Logging) — на случай, если канал не ответит.

import base64
import csv
import datetime
import hashlib
import hmac
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
TBANK_TERMINAL, TBANK_PASSWORD = env('TBANK_TERMINAL'), env('TBANK_PASSWORD')
TBANK_NOTIFY_URL = env('TBANK_NOTIFY_URL', 'https://functions.yandexcloud.net/d4eoevfjg5c2b9e13dq3')
TBANK_TAXATION, TBANK_TAX = env('TBANK_TAXATION'), env('TBANK_TAX', 'none')
TBANK_PAYMENT_METHOD = env('TBANK_PAYMENT_METHOD', 'full_payment')
PRICE = int(env('TICKET_PRICE', '2888'))
# Кодовое слово сравниваем латиницей: в ссылке спикера оно латинское (?kod=opora), чтобы ссылка
# не превращалась в %D0%9A…, а гость может вписать его и по-русски — «Опора» и «opora» совпадут
_LAT = dict(zip('абвгдеёжзийклмнопрстуфхцчшщъыьэюя',
                'a b v g d e e zh z i i k l m n o p r s t u f kh ts ch sh shch _ y _ e iu ia'.split()))
_kod = lambda s: re.sub(r'[^0-9a-z]', '', ''.join(_LAT.get(c, c) for c in s.lower()))
REF_CODES = {_kod(k): v.strip() for k, _, v in (p.partition('=') for p in env('REF_CODES').split(';')) if _kod(k) and v.strip()}
TIMEOUT = 8   # секунд на один запрос к внешнему сервису; таймаут самой функции — 20 с

# Основной адрес API Max и API Т-Банка подписаны сертификатом Минцифры — его нет в стандартном списке доверенных,
# поэтому кладём корневой и промежуточный сертификаты рядом с кодом. Для Max если не выйдет — запасной адрес.
MAX_HOSTS = ['https://platform-api2.max.ru', 'https://platform-api.max.ru']
TB_API = 'https://securepay.tinkoff.ru/v2/'
_CA = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'russian_trusted_ca.pem')


def _ctx(host):
    ctx = ssl.create_default_context()
    if ('api2.max.ru' in host or 'securepay.tinkoff.ru' in host) and os.path.exists(_CA):
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


def _disk_fetch(href, timeout):
    """Скачать файл по временной ссылке Диска. В ссылке может быть кириллица в имени файла, а urllib принимает только ASCII;
    сервер скачивания может отказать клиенту без обычного User-Agent."""
    url = urllib.parse.quote(href, safe="%/:=&?~#+!$,;'@()*[]")
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (fest-zayavki)'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


TABLE_HEAD = ['Дата и время', 'Имя', 'Телефон', 'Telegram / Max', 'Когда придёт', 'Сколько человек',
              'Особенно хочет на', 'Рассказывать о мероприятиях', 'Куда пришла заявка', 'Кто пригласил',
              'Номер заказа', 'Оплата']
# Новые столбцы добавляются в конец, чтобы не сдвигать прежние строки: «Кто пригласил» — 05.11,
# «Номер заказа» и «Оплата» — 06.11. Заголовок старой таблицы (начало нового) заменяется новым.
COL = {name: i for i, name in enumerate(TABLE_HEAD)}


def _cell(v):
    v = str(v)
    # телефон +79001234567 пишем как ="+79001234567": иначе Excel сделает из него число и потеряет плюс
    if re.fullmatch(r'\+\d{10,15}', v):
        return '="%s"' % v
    # Excel считает формулой ячейку, которая начинается с = + - @ — и в лучшем случае покажет ошибку
    return "'" + v if v[:1] in ('=', '+', '-', '@') else v


def _uncell(v):
    """Обратное к _cell: значение ячейки из файла, как его записала функция."""
    m = re.fullmatch(r'="(.*)"', v)
    if m:
        return m.group(1)
    return v[1:] if v[:1] == "'" and v[1:2] in ('=', '+', '-', '@') else v


def _phone(p):
    """Номер в виде +79001234567; российский с 8 или без кода страны приводим к +7."""
    dg = re.sub(r'\D', '', p)
    if len(dg) == 10 and dg[0] == '9':
        dg = '7' + dg
    elif len(dg) == 11 and dg[0] == '8':
        dg = '7' + dg[1:]
    return '+' + dg


def _now():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))


def table_row(d, where):
    return [_now().strftime('%d.%m.%Y %H:%M'), d['name'], _phone(d['phone']), d['messenger'], d['plan'], d['count'], d['mk'],
            'да' if d['news'] else 'нет', where, REF_CODES.get(_kod(d['ref']), d['ref']), d.get('order', ''), d.get('paid', '')]


def col(row, name):
    i = COL[name]
    return row[i] if i < len(row) else ''


def set_col(row, name, value):
    row.extend([''] * (COL[name] + 1 - len(row)))
    row[COL[name]] = value


def ref_text(ref):
    """Строка «Кто пригласил» для сообщения: имя спикера и его слово, или то, что вписал гость."""
    who = REF_CODES.get(_kod(ref))
    return '%s (слово «%s»)' % (who, ref) if who else '%s (не из списка слов)' % ref if ref else ''


def _csv(rows):
    buf = io.StringIO()
    csv.writer(buf, delimiter=';', lineterminator='\r\n').writerows([[_cell(v) for v in r] for r in rows])
    return buf.getvalue()


def _left(deadline, cap=8):
    """Сколько можно ждать очередной запрос: до cap секунд, но не дольше срока deadline (time.monotonic)."""
    left = deadline - time.monotonic()
    if left < 1:
        raise TimeoutError('не хватило времени')
    return min(cap, left)


def _disk_link(kind, deadline, extra=''):
    req = urllib.request.Request('%s/%s?path=%s%s' % (DISK_API, kind, urllib.parse.quote(DISK_PATH), extra),
                                 headers={'Authorization': 'OAuth ' + DISK_TOKEN})
    with urllib.request.urlopen(req, timeout=_left(deadline)) as r:
        return json.loads(r.read().decode('utf-8'))['href']


def disk_read(deadline):
    """Таблица заявок с Диска списком строк, первая — заголовок. Файла ещё нет — только заголовок.
    CSV в UTF-8 с BOM и разделителем «;» — так его сразу правильно открывает русский Excel.
    Если Диск не отдал таблицу вовремя (бывает сразу после прошлой записи), скачивание повторяется ещё раз."""
    try:
        for attempt in (1, 2):
            try:
                text = _disk_fetch(_disk_link('download', deadline), _left(deadline)).decode('utf-8-sig')   # не UTF-8 — ошибка, файл не трогаем
                break
            except urllib.error.HTTPError:
                raise
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt == 2 or deadline - time.monotonic() < 3:
                    raise
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return [list(TABLE_HEAD)]
        raise RuntimeError(_hide('Диск, чтение таблицы: %r' % e, DISK_TOKEN))
    except Exception as e:
        raise RuntimeError(_hide('Диск, чтение таблицы: %r' % e, DISK_TOKEN))
    rows = [[_uncell(v) for v in r] for r in csv.reader(io.StringIO(text), delimiter=';') if r]
    if not rows:
        return [list(TABLE_HEAD)]
    if len(rows[0]) < len(TABLE_HEAD) and rows[0] == TABLE_HEAD[:len(rows[0])]:
        rows[0] = list(TABLE_HEAD)                   # таблица со старым заголовком — дописываем новые столбцы
    return rows


def disk_write(rows, deadline):
    """Записать таблицу на Диск целиком (поверх прежней). Папки нет — создаём её и пробуем ещё раз."""
    data = ('﻿' + _csv(rows)).encode('utf-8')
    try:
        try:
            href = _disk_link('upload', deadline, '&overwrite=true')
        except urllib.error.HTTPError as e:
            # 409 — нет папки таблицы, например «__Клиенты/Заявки фестиваля»; папку приложения (app:/) Диск создаёт сам
            folder = DISK_PATH.rsplit('/', 1)[0]
            if e.code != 409 or folder.endswith(':'):
                raise
            req = urllib.request.Request('%s?path=%s' % (DISK_API, urllib.parse.quote(folder)),
                                         headers={'Authorization': 'OAuth ' + DISK_TOKEN}, method='PUT')
            urllib.request.urlopen(req, timeout=_left(deadline)).close()
            href = _disk_link('upload', deadline, '&overwrite=true')
        with urllib.request.urlopen(urllib.request.Request(href, data=data, method='PUT'), timeout=_left(deadline)):
            pass
    except Exception as e:
        raise RuntimeError(_hide('Диск, запись таблицы: %r' % e, DISK_TOKEN))


def disk_append(row, deadline):
    """Дописать строку в таблицу: скачать, добавить, загрузить обратно."""
    rows = disk_read(deadline)
    rows.append(row)
    disk_write(rows, deadline)


def disk_check():
    """Проверка Диска из консоли: {"check": "disk"}. Ничего не записывает, показывает шаги и ошибки Яндекса.
    Нужна, потому что раздел «Логи» в облаке бывает недоступен."""
    if not DISK_TOKEN:
        return {'DISK_TOKEN': 'не задан — функция не видит переменную; сохраните изменения в Редакторе'}
    auth = {'Authorization': 'OAuth ' + DISK_TOKEN}
    folder = DISK_PATH.rsplit('/', 1)[0]
    out = {'DISK_PATH': DISK_PATH}
    for step, url in (('токен', 'https://cloud-api.yandex.net/v1/disk/'),
                      ('папка «%s»' % folder, '%s?path=%s&limit=0' % (DISK_API, urllib.parse.quote(folder))),
                      ('чтение файла', '%s/download?path=%s' % (DISK_API, urllib.parse.quote(DISK_PATH))),
                      ('запись файла', '%s/upload?path=%s&overwrite=true' % (DISK_API, urllib.parse.quote(DISK_PATH)))):
        try:
            urllib.request.urlopen(urllib.request.Request(url, headers=auth), timeout=TIMEOUT).close()
            out[step] = 'можно'
        except urllib.error.HTTPError as e:
            msg = (json.loads(e.read().decode('utf-8', 'replace') or '{}') or {}).get('message', '')
            out[step] = 'HTTP %s %s' % (e.code, msg)
        except Exception as e:
            out[step] = _hide('ошибка: %r' % e, DISK_TOKEN)
    if out.get('чтение файла', '').startswith('HTTP 404'):
        out['чтение файла'] += ' (файла ещё нет — это нормально до первой заявки)'
    elif out.get('чтение файла') == 'можно':
        # ссылку получить мало: скачиваем сам файл так же, как при записи заявки (содержимое в ответ не попадает)
        try:
            req = urllib.request.Request('%s/download?path=%s' % (DISK_API, urllib.parse.quote(DISK_PATH)), headers=auth)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                href = json.loads(r.read().decode('utf-8'))['href']
            raw = _disk_fetch(href, TIMEOUT)
            raw.decode('utf-8-sig')
            out['скачивание файла'] = 'можно, %d байт, кодировка UTF-8' % len(raw)
        except Exception as e:
            out['скачивание файла'] = _hide('ошибка: %r' % e, DISK_TOKEN)
    return out


# ---------- оплата: интернет-эквайринг Т-Банка ----------

TB_PAID = {'AUTHORIZED', 'CONFIRMED'}          # одностадийная оплата: AUTHORIZED сразу переходит в CONFIRMED
TB_BUSY = {'AUTHORIZING', '3DS_CHECKING', '3DS_CHECKED', 'CONFIRMING', 'CONFIRM_CHECKING', 'PAY_CHECKING'}
TB_OPEN = {'NEW', 'FORM_SHOWED'}               # ссылка выдана, гость ещё не платил — перед новой попыткой отменяем
TB_REFUND = {'REFUNDED', 'PARTIAL_REFUNDED', 'REVERSED', 'PARTIAL_REVERSED'}


def tb_token(params):
    """Подпись запроса и уведомления: корневые поля без Token и вложенных объектов, плюс пароль терминала,
    по алфавиту ключей, значения подряд → SHA-256. Булевы значения банк подписывает как true/false."""
    pairs = {k: v for k, v in params.items() if k != 'Token' and not isinstance(v, (dict, list))}
    pairs['Password'] = TBANK_PASSWORD
    s = lambda v: 'true' if v is True else 'false' if v is False else str(v)
    return hashlib.sha256(''.join(s(pairs[k]) for k in sorted(pairs)).encode('utf-8')).hexdigest()


def tb_call(method, deadline, **params):
    """Запрос к API эквайринга как есть, вместе с ответом об ошибке."""
    params = dict(params, TerminalKey=TBANK_TERMINAL)
    params['Token'] = tb_token(params)
    req = urllib.request.Request(TB_API + method, data=json.dumps(params, ensure_ascii=False).encode('utf-8'),
                                 method='POST', headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=_left(deadline), context=_ctx(TB_API)) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception as e:
        raise RuntimeError(_hide('Т-Банк %s недоступен: %r' % (method, e), TBANK_PASSWORD))


def tb_api(method, deadline, **params):
    res = tb_call(method, deadline, **params)
    if not res.get('Success'):
        raise RuntimeError('Т-Банк %s: ошибка %s %s %s' % (method, res.get('ErrorCode'), res.get('Message', ''), res.get('Details', '')))
    return res


def rub(kop):
    return '{:,}'.format(kop // 100).replace(',', ' ') + ' ₽'


def tickets(n):
    m10, m100 = n % 10, n % 100
    return '%d %s' % (n, 'билет' if m10 == 1 and m100 != 11 else 'билета' if 2 <= m10 <= 4 and not 12 <= m100 <= 14 else 'билетов')


def tb_init(d, deadline):
    """Создать платёж; вернуть ссылку на страницу оплаты. Срок ссылки — час: потом гость оформит заявку заново."""
    n = int(d['count'])
    params = dict(Amount=PRICE * 100 * n, OrderId=d['order'], PayType='O',
                  Description='Фестиваль От Страха к Любви, 21.11.2026: ' + tickets(n),
                  NotificationURL=TBANK_NOTIFY_URL, SuccessURL=ORIGINS[0] + '/?pay=ok', FailURL=ORIGINS[0] + '/?pay=fail',
                  RedirectDueDate=(_now() + datetime.timedelta(hours=1)).isoformat(timespec='seconds'),
                  DATA={'OperationInitiatorType': '0'})
    if TBANK_TAXATION:
        # онлайн-касса подключена — чек обязателен; его содержимое подтверждает бухгалтер
        params['Receipt'] = {'Phone': _phone(d['phone']), 'Taxation': TBANK_TAXATION,
                             'Items': [{'Name': 'Билет на фестиваль От Страха к Любви, 21.11.2026', 'Price': PRICE * 100,
                                        'Quantity': n, 'Amount': PRICE * 100 * n, 'Tax': TBANK_TAX,
                                        'PaymentMethod': TBANK_PAYMENT_METHOD, 'PaymentObject': 'service'}]}
    return tb_api('Init', deadline, **params)['PaymentURL']


def tb_check():
    """Проверка из консоли: {"check": "tbank"}. Спрашивает у банка несуществующий заказ — платёж не создаётся.
    Ошибка про заказ значит, что связь, сертификат, терминал и пароль в порядке."""
    if not (TBANK_TERMINAL and TBANK_PASSWORD):
        return {'TBANK_TERMINAL / TBANK_PASSWORD': 'не заданы'}
    try:
        res = tb_call('CheckOrder', time.monotonic() + 9, OrderId='check-0')
    except Exception as e:
        return {'связь с банком': str(e)}
    return {'терминал': TBANK_TERMINAL, 'связь с банком': 'есть', 'ответ банка': {k: res.get(k) for k in ('Success', 'ErrorCode', 'Message', 'Details')},
            'подсказка': '«Терминал не найден» — проверьте TBANK_TERMINAL; про неверный токен — пароль не от этого терминала; '
                         'про заказ или пустой список платежей — всё в порядке'}


def paid_text(row, order, amount, title):
    """Сообщение «оплачено» или «возврат» для чата: кто, сколько, от кого."""
    lines = [title, '']
    if row:
        lines += ['Имя: ' + col(row, 'Имя'), 'Телефон: ' + col(row, 'Телефон'), 'Сколько человек: ' + col(row, 'Сколько человек')]
        if col(row, 'Кто пригласил'):
            lines.append('Кто пригласил: ' + col(row, 'Кто пригласил'))
    else:
        lines += ['Телефон: +' + order.split('-')[0], '⚠ Заявки с этим номером заказа нет в таблице — отметьте оплату вручную']
    return '\n'.join(lines + ['Заказ: ' + order, '', _stamp()])


def tb_notification(raw, started):
    """Уведомление банка о платеже. Отвечаем OK (иначе банк будет повторять); оплату отмечаем один раз."""
    if not (TBANK_TERMINAL and TBANK_PASSWORD) or raw.get('TerminalKey') != TBANK_TERMINAL \
            or not hmac.compare_digest(str(raw.get('Token', '')), tb_token(raw)):
        print(json.dumps({'event': 'tb_bad_token', 'order': raw.get('OrderId')}, ensure_ascii=False))
        return reply_text(403, 'bad token')
    status, order, amount = raw.get('Status'), str(raw.get('OrderId', '')), int(raw.get('Amount') or 0)
    print(json.dumps({'event': 'tb_notification', 'order': order, 'status': status, 'amount': amount}, ensure_ascii=False))
    if status != 'CONFIRMED' and status not in TB_REFUND:
        return reply_text(200, 'OK')
    deadline = started + 8.5                     # банк ждёт ответа 10 секунд
    rows = row = None
    try:
        rows = disk_read(started + 5)
        row = next((r for r in rows[1:] if col(r, 'Номер заказа') == order), None)
    except Exception as e:
        print(json.dumps({'event': 'table_failed', 'error': str(e)}, ensure_ascii=False))
    stamp = _now().strftime('%d.%m %H:%M')
    if status == 'CONFIRMED':
        if row and col(row, 'Оплата').startswith('оплачено'):
            return reply_text(200, 'OK')         # повтор уведомления — уже отмечено
        text = paid_text(row, order, amount, '✅ Оплачено: %s' % rub(amount))
        if row:
            expected = PRICE * 100 * int(col(row, 'Сколько человек') or 0) if col(row, 'Сколько человек').isdigit() else None
            if expected is not None and expected != amount:
                text = text.replace('\n\n', '\n⚠ Сумма не совпадает с заявкой: ждали %s\n\n' % rub(expected), 1)
            set_col(row, 'Оплата', 'оплачено %s · %s' % (stamp, rub(amount)))
    else:
        if row and col(row, 'Оплата').startswith('возврат'):
            return reply_text(200, 'OK')
        text = paid_text(row, order, amount, '↩ Возврат: %s' % rub(amount))
        if row:
            set_col(row, 'Оплата', 'возврат %s · %s' % (stamp, rub(amount)))
    notify(text)
    if row:
        try:
            disk_write(rows, deadline)
        except Exception as e:
            print(json.dumps({'event': 'table_failed', 'error': str(e)}, ensure_ascii=False))
            notify('⚠ Отметка по заказу %s не записалась в таблицу. Причина: %s' % (order, e))
    return reply_text(200, 'OK')


def apply_with_payment(d, chs, origin, started, more=False):
    """Заявка с оплатой: проверить, не оплачено ли уже по этому телефону, создать платёж,
    отправить заявку в чат, записать или обновить строку в таблице, отдать сайту ссылку на оплату.
    По оплаченному телефону новый платёж — только когда гость подтвердил «купить ещё» (more):
    так случайная вторая оплата не пройдёт, а выдумывать номер ради второго билета незачем."""
    phone, n = _phone(d['phone']), int(d['count'])
    rows, mine, notes = None, [], []
    is_paid = lambda r: col(r, 'Оплата').startswith('оплачено')
    try:
        rows = disk_read(started + 6)
        mine = [r for r in rows[1:] if col(r, 'Телефон') == phone and col(r, 'Номер заказа')]
    except Exception as e:
        notes.append('⚠ Таблица не прочиталась, повторную оплату по этому телефону проверить не удалось: %s' % e)
    prev = mine[-1] if mine else None
    replace = marked = None
    if prev and not is_paid(prev):
        order0 = col(prev, 'Номер заказа')
        try:
            pays = tb_api('CheckOrder', started + 9, OrderId=order0).get('Payments') or []
        except Exception as e:
            pays = []
            print(json.dumps({'event': 'tb_check_failed', 'error': str(e)}, ensure_ascii=False))
        done = next((p for p in pays if p.get('Status') in TB_PAID), None)
        if done:
            # оплата прошла, а уведомление банка до нас не дошло — отмечаем сейчас
            amount = int(done.get('Amount') or 0)
            set_col(prev, 'Оплата', 'оплачено %s · %s' % (_now().strftime('%d.%m %H:%M'), rub(amount)))
            notify(paid_text(prev, order0, amount, '✅ Оплачено: %s' % rub(amount)))
            marked = order0
        elif any(p.get('Status') in TB_BUSY for p in pays):
            return reply(409, {'ok': False, 'error': 'busy'}, origin)
        else:
            for p in pays:
                if p.get('Status') in TB_OPEN:
                    try:
                        tb_api('Cancel', started + 10, PaymentId=p['PaymentId'])
                    except Exception as e:
                        print(json.dumps({'event': 'tb_cancel_failed', 'error': str(e)}, ensure_ascii=False))
            replace = prev
            if col(prev, 'Имя').strip().lower() != d['name'].strip().lower():
                notes.append('⚠ Этот телефон уже был в неоплаченной заявке «%s» — заявка заменена этой' % col(prev, 'Имя'))
    paid_n = sum(int(col(r, 'Сколько человек')) for r in mine if is_paid(r) and col(r, 'Сколько человек').isdigit())
    if paid_n and not more:
        if marked:
            try:
                disk_write(rows, started + 15)
            except Exception as e:
                notify('⚠ Отметка по заказу %s не записалась в таблицу. Причина: %s' % (marked, e))
        return reply(409, {'ok': False, 'error': 'paid', 'tickets': paid_n}, origin)
    # номер заказа: телефон и номер попытки; если таблицы не видно — время, чтобы номер не повторился
    nums = [int(m.group(1)) for m in (re.search(r'-(\d+)$', col(r, 'Номер заказа')) for r in mine) if m]
    d['order'] = '%s-%d' % (phone[1:], max(nums, default=0) + 1) if rows is not None else '%s-t%d' % (phone[1:], int(time.time()))
    d['sum'] = '%s (%s × %s)' % (rub(PRICE * 100 * n), n, rub(PRICE * 100)) if n > 1 else rub(PRICE * 100)
    pay_url = None
    try:
        pay_url = tb_init(d, started + 12)
        d['paid'] = 'ждём оплату'
    except Exception as e:
        print(json.dumps({'event': 'tb_init_failed', 'error': str(e)}, ensure_ascii=False))
        notes.append('⚠ Ссылка на оплату не создалась — свяжитесь с гостем. Причина: %s' % e)
        d['paid'] = 'ссылка на оплату не создалась'
    d['title'] = ('🌿 Докупка: по этому телефону уже оплачено %s' % tickets(paid_n) if paid_n
                  else '🌿 Заявка обновлена — новая попытка оплаты' if replace else '🌿 Новая заявка на фестиваль')
    d['notes'] = notes
    where = deliver(d, chs)
    row = table_row(d, where or 'не доставлена')
    try:
        if rows is None:
            disk_append(row, started + 17)
        else:
            if replace:
                rows[rows.index(replace)] = row
            else:
                rows.append(row)
            disk_write(rows, started + 17)
        print(json.dumps({'event': 'table_saved'}, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({'event': 'table_failed', 'error': str(e)}, ensure_ascii=False))
        notify('⚠ Заявка «%s» (заказ %s) не записалась в таблицу на Диске. Причина: %s' % (d['name'], d['order'], e))
    if pay_url:
        return reply(200, {'ok': True, 'pay': pay_url}, origin)
    if where:
        return reply(200, {'ok': True}, origin)
    return reply(502, {'ok': False, 'error': 'не удалось переслать заявку'}, origin)


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


def deliver(d, chs):
    """Отправить заявку в первый канал, который доставит; вернуть его название или None."""
    for n, has, send, _ in chs:
        if not has:
            continue
        try:
            send(d)
            print(json.dumps({'event': 'delivered', 'channel': n}, ensure_ascii=False))
            return n
        except Exception as e:
            print(json.dumps({'event': 'send_failed', 'channel': n, 'error': str(e)}, ensure_ascii=False))
    return None


def notify(text):
    """Служебное сообщение сотрудникам (оплата, сбой): в чат ВК, а если не вышло — на почту."""
    if VK_TOKEN and VK_PEER_ID:
        try:
            vk_api('messages.send', peer_id=VK_PEER_ID, random_id=random.randint(1, 2 ** 31 - 1), dont_parse_links=1, message=text)
            return
        except Exception as e:
            print(json.dumps({'event': 'notice_failed', 'channel': 'ВКонтакте', 'error': str(e)}, ensure_ascii=False))
    if MAIL_USER and MAIL_PASSWORD and MAIL_TO:
        msg = EmailMessage()
        msg['Subject'] = text.split('\n', 1)[0]
        msg['From'] = formataddr(('Сайт фестиваля «От Страха к Любви»', MAIL_USER))
        msg['To'] = ', '.join(MAIL_TO)
        msg.set_content(text)
        try:
            with smtplib.SMTP_SSL(MAIL_HOST, MAIL_PORT, timeout=TIMEOUT, context=ssl.create_default_context()) as s:
                s.login(MAIL_USER, MAIL_PASSWORD)
                s.send_message(msg)
        except Exception as e:
            print(json.dumps({'event': 'notice_failed', 'channel': 'Почта', 'error': _hide(repr(e), MAIL_PASSWORD)}, ensure_ascii=False))


def reply_text(code, text):
    """Ответ банку на уведомление: простой текст, без JSON."""
    return {'statusCode': code, 'headers': {'Content-Type': 'text/plain; charset=utf-8'}, 'body': text}


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
            ('Рассказывать о мероприятиях центра', 'да' if d['news'] else 'нет'), ('Кто пригласил', ref_text(d['ref'])),
            ('К оплате', d.get('sum', '')), ('Заказ', d.get('order', ''))]
    return [(k, v) for k, v in rows if v]


def _stamp():
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3))).strftime('%d.%m.%Y %H:%M')
    return '%s по Москве · с сайта fest.mirra-spb.ru' % now


def as_html(d):
    lines = ['<b>%s</b>' % html.escape(d.get('title', '🌿 Новая заявка на фестиваль')), '']
    lines += ['<b>%s:</b> %s' % (k, html.escape(v)) for k, v in _rows(d)]
    lines += [html.escape(n) for n in d.get('notes', [])]
    return '\n'.join(lines + ['', '<i>%s</i>' % _stamp()])


def as_text(d):
    lines = [d.get('title', '🌿 Новая заявка на фестиваль'), '']
    lines += ['%s: %s' % (k, v) for k, v in _rows(d)]
    lines += d.get('notes', [])
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

    # уведомление Т-Банка о платеже: приходит от банка, без Origin; подлинность — по подписи
    if 'TerminalKey' in raw and 'Token' in raw and 'Status' in raw:
        return tb_notification(raw, time.monotonic())

    if raw.get('check') == 'disk':
        return reply(200, disk_check())
    if raw.get('check') == 'tbank':
        return reply(200, tb_check())

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
         'ref': clean(raw.get('ref'), 40), 'news': raw.get('news') is True}
    digits = re.sub(r'\D', '', d['phone'])
    if not d['name'] or not 10 <= len(digits) <= 15 or raw.get('consent') is not True:
        return reply(400, {'ok': False, 'error': 'проверьте имя, телефон и согласие'}, allowed)

    started = time.monotonic()
    print(json.dumps({'event': 'request', **d}, ensure_ascii=False))
    # оплата — когда её включил сайт (поле pay) и заданы ключи терминала; иначе — просто заявка, как раньше
    if raw.get('pay') is True and TBANK_TERMINAL and TBANK_PASSWORD:
        if d['count'] not in ('1', '2', '3', '4', '5'):
            return reply(400, {'ok': False, 'error': 'число гостей — от 1 до 5'}, allowed)
        return apply_with_payment(d, chs, allowed, started, more=raw.get('more') is True)
    where = deliver(d, chs)

    # Таблица — после отправки и не дольше 15 с от начала: функцию обрывают на 20-й секунде,
    # а заявка уже доставлена, и гость не должен увидеть ошибку из-за Диска; остаток — на сообщение о сбое в ВК.
    if DISK_TOKEN and time.monotonic() - started < 13:
        try:
            disk_append(table_row(d, where or 'не доставлена'), deadline=started + 15)
            print(json.dumps({'event': 'table_saved'}, ensure_ascii=False))
        except Exception as e:
            print(json.dumps({'event': 'table_failed', 'error': str(e)}, ensure_ascii=False))
            # логи в облаке бывают недоступны — о сбое должны узнать сотрудники, заявка в таблицу не попала
            if VK_TOKEN and VK_PEER_ID and time.monotonic() - started < 16:
                try:
                    vk_api('messages.send', peer_id=VK_PEER_ID, random_id=random.randint(1, 2 ** 31 - 1), dont_parse_links=1,
                           message='⚠ Заявка «%s» не записалась в таблицу на Диске. Причина: %s' % (d['name'], e))
                except Exception as e2:
                    print(json.dumps({'event': 'table_notice_failed', 'error': str(e2)}, ensure_ascii=False))

    if where:
        return reply(200, {'ok': True}, allowed)
    return reply(502, {'ok': False, 'error': 'не удалось переслать заявку'}, allowed)
