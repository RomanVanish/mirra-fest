// Посредник для Telegram на Cloudflare Workers.
// Из российских дата-центров (Яндекс Облако) api.telegram.org недоступен, поэтому функция заявок
// шлёт запросы сюда, а Cloudflare пересылает их в Telegram. Адрес посредника — в переменной TG_API функции.
//
// Пропускает только запросы нашего бота (номер — часть токена до двоеточия, он не секретный),
// чтобы посредником не воспользовался кто-то ещё. Сам токен здесь не хранится — его присылает функция.
const BOT_ID = '901939185';

export default {
  async fetch(request) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith('/bot' + BOT_ID + ':')) {
      return new Response('not found', { status: 404 });
    }
    return fetch('https://api.telegram.org' + url.pathname + url.search, {
      method: request.method,
      headers: { 'Content-Type': 'application/json' },
      body: request.method === 'POST' ? await request.text() : undefined,
    });
  },
};
