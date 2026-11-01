const root = document.querySelector('#analytics-root');
const stylesheet = document.createElement('link');
stylesheet.rel = 'stylesheet';
stylesheet.href = 'analytics-summary.css?v=summary1';
document.head.append(stylesheet);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
const formatTime = seconds => seconds == null ? '—' : seconds < 60 ?
  seconds.toLocaleString('ru-RU', {maximumFractionDigits:1}) + ' с' : seconds < 3600 ?
  (seconds / 60).toLocaleString('ru-RU', {maximumFractionDigits:1}) + ' мин' :
  (seconds / 3600).toLocaleString('ru-RU', {maximumFractionDigits:1}) + ' ч';
const money = value => value == null ? '—' : '$' + Number(value).toLocaleString('ru-RU', {minimumFractionDigits:2, maximumFractionDigits:4});
const date = value => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('ru-RU', {
  timeZone:'Europe/Moscow', day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit'
}) + ' МСК' : null;

function card(label, value, note, primary = false) {
  return `<article class="metric ${primary ? 'metric-primary' : ''}"><span>${esc(label)}</span><strong>${esc(value)}</strong><p>${esc(note)}</p></article>`;
}

function render(raw) {
  const metrics = raw.production_summary;
  if (!metrics || !Number.isInteger(metrics.ready_video_count) || metrics.ready_video_count < 0) throw Error('Нет данных о готовых роликах.');
  const count = metrics.ready_video_count, timed = metrics.timed_video_count;
  const seconds = number(metrics.average_generation_seconds) ? metrics.average_generation_seconds : null;
  const cost = metrics.average_video_cost || {}, amount = cost.amount?.usd;
  const price = amount != null && Number.isFinite(Number(amount)) ? amount : null;
  const timeNote = seconds == null ? 'Пока нет замеров полного запуска.' : `Факт · замеры для ${timed} из ${count} роликов`;
  const costNote = price == null ? (count ? 'Пока нет данных о стоимости.' : 'Появится после готового ролика.') :
    `${cost.label || 'Нет данных'}${cost.complete ? '' : ' · частично'} · по ${cost.priced_videos} из ${count} роликов`;
  const updated = date(raw.sync?.last_attempt_at_utc || raw.sync?.last_success_at_utc);
  root.innerHTML = `<div class="analytics-toolbar simple-toolbar"><div class="a-heading"><h2>Главное о роликах</h2><p>За всё время</p></div><button id="a-refresh">Обновить</button></div>
    <div class="metrics simple-metrics">
      ${card('Среднее время генерации', formatTime(seconds), timeNote)}
      ${card('Средняя стоимость ролика', money(price), costNote)}
      ${card('Готовых роликов', String(count), 'Факт · готовые MP4', true)}
    </div>
    <p class="simple-footer">Время — от запуска кода до готового ролика. Стоимость — по учтённым расходам, с пометкой «Факт» или «Оценка».</p>
    ${updated ? `<p class="simple-updated">Данные обновлены ${esc(updated)}</p>` : ''}`;
  root.querySelector('#a-refresh').onclick = load;
}

async function load() {
  const button = root.querySelector('#a-refresh');
  if (button) { button.disabled = true; button.textContent = 'Обновляю…'; }
  try {
    const response = await fetch('billing-data.json', {cache:'no-store'});
    if (!response.ok) throw Error('Не удалось загрузить показатели.');
    render(await response.json());
  } catch (error) {
    const previous = root.querySelector('.a-notice');
    if (previous) previous.remove();
    root.insertAdjacentHTML('beforeend', `<p class="a-notice a-error" role="status">${esc(error.message)}</p>`);
    if (button) { button.disabled = false; button.textContent = 'Обновить'; }
    else root.insertAdjacentHTML('beforeend', '<button id="a-refresh">Повторить</button>');
    root.querySelector('#a-refresh').onclick = load;
  }
}
load();
