# Собирает index.html (полная страница для хостинга) из page.html (фрагмент, он же превью-артефакт).
# Запуск: python3 build.py
import re, pathlib
here = pathlib.Path(__file__).parent
p = (here/'page.html').read_text(encoding='utf-8')
parts = re.findall(r'(<title>.*?</title>|<meta name="claude[^>]*>|<link[^>]*>|<style>.*?</style>)', p, re.S)
body = p
for h in parts: body = body.replace(h, '', 1)
head = '\n'.join(h for h in parts if 'claude:' not in h)
doc = f'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="description" content="Фестиваль психологических практик «От Страха к Любви» · 21 ноября 2026 · психологический центр «МирРа», Санкт-Петербург, Лиговский пр., 44В">
<meta property="og:title" content="От Страха к Любви · фестиваль психологических практик">
<meta property="og:description" content="21 ноября 2026 · шесть мастер-классов о том, как страх превращается в любовь — через доверие к миру, к себе, к телу и к роду">
<meta property="og:type" content="website">
<meta name="theme-color" content="#2f3850">
{head}
</head>
<body>
{body.strip()}
</body>
</html>
'''
(here/'index.html').write_text(doc, encoding='utf-8')
print('index.html', len(doc))
