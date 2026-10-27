// Cloudflare Worker: раз в минуту проверяет слоты врача на SberHealth и пишет в Телеграм.
// Логика та же, что в monitor.py; состояние — одна строка JSON в базе D1.
// Секреты (wrangler secret put): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DOCTOR_ID.

// ---------------------------------------------
// Запрос слотов (API)
// ---------------------------------------------

const API_BASE = "https://telemed-patient-bff.sberhealth.ru/api/showcase/web/v1";
const SPECIALITY_ID = 47; // «Психолог» на SberHealth
const MSK_OFFSET_MS = 3 * 3600 * 1000;

const USER_AGENTS = [
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
    "(KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) " +
    "Gecko/20100101 Firefox/109.0",
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_1) AppleWebKit/605.1.15 " +
    "(KHTML, like Gecko) Version/16.1 Safari/605.1.15",
];

function getHeaders() {
  return {
    "User-Agent": USER_AGENTS[Math.floor(Math.random() * USER_AGENTS.length)],
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
  };
}

export function doctorUrl(doctorId) {
  return `https://lk.sberhealth.ru/telemed/speciality/${SPECIALITY_ID}/doctor/${doctorId}`;
}

/**
 * Возвращает { 'YYYY-MM-DD': Set('HH:MM', ...) } или {}, если слотов нет.
 * При ошибке запроса — исключение, чтобы сбой сети не выглядел как «слотов нет».
 */
export async function fetchSlots(doctorId) {
  const resp = await fetch(
    `${API_BASE}/providers/62/doctors/${doctorId}/specialties/psychologist/slots`,
    // Таймауты держат запуск короче минуты, чтобы запуски cron не накладывались
    { headers: getHeaders(), signal: AbortSignal.timeout(15000) }
  );
  if (!resp.ok) throw new Error(`SberHealth HTTP ${resp.status}`);
  const data = await resp.json();

  const result = {};
  for (const slot of data.slots || []) {
    if (!slot.from) continue;
    // "2026-10-23T10:00:00+03:00": дата и время уже по Москве
    const date = slot.from.slice(0, 10);
    const time = slot.from.slice(11, 16);
    (result[date] ||= new Set()).add(time);
  }
  return result;
}

// ---------------------------------------------
// Локализация
// ---------------------------------------------

const WEEKDAYS_RU = [
  "понедельник", "вторник", "среда",
  "четверг", "пятница", "суббота", "воскресенье",
];

const MONTHS_RU = [
  "", // индекс 0 пуст
  "января", "февраля", "марта", "апреля",
  "мая", "июня", "июля", "августа",
  "сентября", "октября", "ноября", "декабря",
];

/** '2025-01-26' => '26 января (воскресенье)'. */
export function formatDateRussian(dateStr) {
  const d = new Date(`${dateStr}T00:00:00Z`);
  const weekday = (d.getUTCDay() + 6) % 7; // Пн=0..Вс=6
  return `${d.getUTCDate()} ${MONTHS_RU[d.getUTCMonth() + 1]} (${WEEKDAYS_RU[weekday]})`;
}

/** Момент времени (мс) => '26 января в 19:30' по Москве. */
export function formatDatetimeRussian(ms) {
  const d = new Date(ms + MSK_OFFSET_MS);
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${d.getUTCDate()} ${MONTHS_RU[d.getUTCMonth() + 1]} в ${hh}:${mm}`;
}

// ---------------------------------------------
// Формирование текста
// ---------------------------------------------

/**
 * Текст (Markdown): showNewAlert — фраза «Появились слоты»,
 * highlight — слоты (дата -> Set времени), которые только что добавились (жирным).
 */
export function buildScheduleMessage(doctorId, slots, showNewAlert, highlight = {}) {
  const lines = [];
  if (showNewAlert) lines.push("🟢 *Появились слоты* 🟢\n");
  lines.push(`🗓 **[Доступные записи](${doctorUrl(doctorId)}):**`);

  const dates = Object.keys(slots).sort();
  if (!dates.length) lines.push("_(пока пусто)_");
  for (const date of dates) {
    const hl = highlight[date] || new Set();
    const times = [...slots[date]].sort().map((t) => (hl.has(t) ? `**${t}**` : t));
    lines.push(`${formatDateRussian(date)}: ${times.join(", ")}`);
  }
  return lines.join("\n");
}

// ---------------------------------------------
// Телеграм
// ---------------------------------------------

/** okDescriptions — ответы-ошибки Телеграма, которые для нас означают успех. */
async function tg(env, method, payload, okDescriptions = []) {
  try {
    const resp = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, ...payload }),
      signal: AbortSignal.timeout(10000),
    });
    const data = await resp.json().catch(() => ({}));
    if (okDescriptions.some((d) => (data.description || "").includes(d))) return true;
    if (!resp.ok || !data.ok) {
      console.log(`[!] Telegram ${method}:`, resp.status, JSON.stringify(data));
      return null;
    }
    return data.result;
  } catch (e) {
    console.log(`[!] Telegram ${method}:`, e.message);
    return null;
  }
}

/** Если silent — тихое уведомление. Возвращает message_id или null. */
async function tgSendMessage(env, text, silent) {
  const res = await tg(env, "sendMessage", {
    text, parse_mode: "Markdown", disable_notification: silent,
  });
  return res ? res.message_id : null;
}

/**
 * false — не вышло (например, сообщение удалили в чате), тогда шлём новое.
 * «message is not modified» — успех: в Markdown `**10:00**` выглядит как `10:00`,
 * и сообщение уже показывает нужный текст.
 */
async function tgEditMessage(env, msgId, text) {
  const res = await tg(
    env, "editMessageText", { message_id: msgId, text, parse_mode: "Markdown" }, ["message is not modified"]
  );
  return res !== null;
}

// Телеграм даёт боту удалить своё сообщение только в первые 48 часов
const REFRESH_AFTER_MS = 46 * 3600 * 1000;

/**
 * Убираем своё сообщение; если удалить уже нельзя — стираем текст, чтобы не висели старые слоты.
 * true — сообщения больше нет или в нём уже нет старых слотов.
 */
async function retireMessage(env, msgId) {
  if (await tg(env, "deleteMessage", { message_id: msgId }, ["message to delete not found"])) return true;
  const res = await tg(
    env, "editMessageText", { message_id: msgId, text: "_(устарело)_", parse_mode: "Markdown" },
    ["message is not modified", "message to edit not found"]
  );
  return res !== null;
}

// ---------------------------------------------
// Сравнение слотов и баннер
// ---------------------------------------------

const isEmpty = (slots) => Object.keys(slots).length === 0;

/** Слоты из a, которых нет в b. */
function diffSlots(a, b) {
  const diff = {};
  for (const [date, times] of Object.entries(a)) {
    const other = b[date] || new Set();
    const extra = new Set([...times].filter((t) => !other.has(t)));
    if (extra.size) diff[date] = extra;
  }
  return diff;
}

/** Если прошло >1 часа с момента появления или слоты обнулились, выключаем баннер. */
function stillShowBanner(slots, timeOfNew, now) {
  if (!timeOfNew) return false;
  if (now - timeOfNew > 3600 * 1000) return false;
  return !isEmpty(slots);
}

// ---------------------------------------------
// Хранилище (D1, формат как у state.json)
// ---------------------------------------------

// Дольше любого запуска: все запросы наружу с таймаутами
const LOCK_MS = 5 * 60 * 1000;

/**
 * Один запуск за раз: пока предыдущий не закончил (например, Телеграм тормозит),
 * следующий cron пропускает ход — иначе оба прочитают одно состояние и пришлют дубль.
 */
/** Возвращает метку своего замка или null, если занят. */
async function acquireLock(env, now) {
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT NOT NULL)").run();
  const mark = String(now + LOCK_MS);
  const res = await env.DB.prepare(
    "INSERT INTO state (k, v) VALUES ('lock', ?1) ON CONFLICT(k) DO UPDATE SET v = excluded.v " +
      "WHERE CAST(state.v AS INTEGER) < ?2"
  ).bind(mark, now).run();
  return res.meta.changes === 1 ? mark : null;
}

/** Снимаем только свой замок: если наш истёк и его взял следующий запуск, не трогаем. */
async function releaseLock(env, mark) {
  await env.DB.prepare("UPDATE state SET v = '0' WHERE k = 'lock' AND v = ?1").bind(mark).run();
}

async function loadState(env) {
  const row = await env.DB.prepare("SELECT v FROM state WHERE k = 'state'").first();
  return row ? row.v : null;
}

async function saveState(env, json) {
  await env.DB.prepare(
    "INSERT INTO state (k, v) VALUES ('state', ?1) ON CONFLICT(k) DO UPDATE SET v = excluded.v"
  ).bind(json).run();
}

// ---------------------------------------------
// Одна проверка (тело цикла monitor.py)
// ---------------------------------------------

export async function check(env, now = Date.now()) {
  const doctorId = env.DOCTOR_ID;
  if (!doctorId || !env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) {
    console.log("[!] Не заданы секреты TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID или DOCTOR_ID");
    return;
  }
  const lock = await acquireLock(env, now);
  if (!lock) {
    console.log("[i] Предыдущий запуск ещё идёт — пропускаю ход");
    return;
  }
  try {
    await runCheck(env, doctorId, now);
  } finally {
    await releaseLock(env, lock);
  }
}

async function runCheck(env, doctorId, now) {
  let newSlots;
  try {
    newSlots = await fetchSlots(doctorId);
  } catch (e) {
    console.log("[!] Ошибка запроса к SberHealth:", e.message);
    return;
  }

  const raw = await loadState(env);
  const st = raw ? JSON.parse(raw) : {};
  let msgSchedule = st.message_id_schedule ?? null;
  let oldScheduleText = st.old_schedule_text ?? null;
  let msgNoSlots = st.message_id_no_slots ?? null;
  let oldNoSlotsText = st.old_no_slots_text ?? null;
  let timeOfNew = st.time_of_new_slots ? Date.parse(st.time_of_new_slots) : null;
  let lastFound = st.last_time_slots_found ? Date.parse(st.last_time_slots_found) : null;
  // Время отправки неизвестно (старое состояние) — считаем сообщение старым
  let scheduleSentAt = st.schedule_sent_at ? Date.parse(st.schedule_sent_at) : null;
  let noSlotsSentAt = st.no_slots_sent_at ? Date.parse(st.no_slots_sent_at) : null;
  let current = {};
  for (const [date, times] of Object.entries(st.current_slots || {})) current[date] = new Set(times);
  // Состояние другого врача не продолжаем
  if (st.doctor_id && String(st.doctor_id) !== String(doctorId)) current = {};

  // Не убранное (сбой связи) не теряем: пробуем снова в следующих запусках
  const retireLater = [];
  const retire = async (msgId) => {
    if (!(await retireMessage(env, msgId))) retireLater.push(msgId);
  };
  // Не больше трёх за запуск, чтобы запуск оставался коротким; остальные — в следующие
  const pending = st.retire_later || [];
  for (const msgId of pending.slice(0, 3)) await retire(msgId);
  retireLater.push(...pending.slice(3));

  if (isEmpty(newSlots)) {
    // 1) Нет слотов: удаляем сообщение с расписанием, если было
    if (msgSchedule !== null) {
      await retire(msgSchedule);
      msgSchedule = null;
      oldScheduleText = null;
    }

    let text = "🔴 *Слотов нет* 🔴\n\nНе волнуйтесь — как только освободится окошко, сразу напишу 🙏🏻";
    if (lastFound) text += `\n\n_(Появлялись ${formatDatetimeRussian(lastFound)})_`;

    if (msgNoSlots && oldNoSlotsText !== text) {
      if (await tgEditMessage(env, msgNoSlots, text)) oldNoSlotsText = text;
      else {
        await retire(msgNoSlots); // правка не вышла — убираем старое и пришлём заново
        msgNoSlots = null;
      }
    }
    if (!msgNoSlots) {
      // Громко — только когда слоты пропали; восстановление после сбоя — тихо
      msgNoSlots = await tgSendMessage(env, text, oldNoSlotsText !== null);
      if (msgNoSlots) oldNoSlotsText = text; // не отправилось — повторим так же громко
      noSlotsSentAt = now;
    }
    current = {};
  } else {
    // 2) Есть слоты: удаляем «нет слотов», если оно есть
    if (msgNoSlots) {
      await retire(msgNoSlots);
      msgNoSlots = null;
      oldNoSlotsText = null;
    }

    // Если раньше было пусто, а теперь появились => громкое уведомление
    const noToYes = isEmpty(current);
    const added = diffSlots(newSlots, current);

    if (!isEmpty(added)) {
      if (msgSchedule) {
        await retire(msgSchedule);
        msgSchedule = null;
        oldScheduleText = null;
      }
      timeOfNew = now;
      const text = buildScheduleMessage(doctorId, newSlots, true, noToYes ? {} : added);
      msgSchedule = await tgSendMessage(env, text, !noToYes);
      oldScheduleText = text;
      scheduleSentAt = now;
      lastFound = now;
      // Не отправилось — не запоминаем слоты, чтобы в следующий раз снова прислать громко
      if (msgSchedule !== null) current = newSlots;
    } else {
      // Нет новых слотов, но могли пропасть
      const text = buildScheduleMessage(doctorId, newSlots, stillShowBanner(newSlots, timeOfNew, now));
      if (msgSchedule && oldScheduleText !== text) {
        if (await tgEditMessage(env, msgSchedule, text)) oldScheduleText = text;
        else {
          await retire(msgSchedule); // правка не вышла — убираем старое и пришлём заново
          msgSchedule = null;
        }
      }
      if (!msgSchedule) {
        // Создаем заново (тихое, так как не "с нуля" появилось)
        msgSchedule = await tgSendMessage(env, text, true);
        oldScheduleText = text;
        scheduleSentAt = now;
      }
      current = newSlots;
    }
  }

  // Перевыкладываем (тихо) сообщение, пока его ещё можно удалить:
  // иначе при смене слотов оно останется в чате со старыми данными
  if (msgSchedule && (scheduleSentAt === null || now - scheduleSentAt >= REFRESH_AFTER_MS)) {
    await retire(msgSchedule);
    msgSchedule = await tgSendMessage(env, oldScheduleText, true);
    scheduleSentAt = now;
  }
  if (msgNoSlots && (noSlotsSentAt === null || now - noSlotsSentAt >= REFRESH_AFTER_MS)) {
    await retire(msgNoSlots);
    msgNoSlots = await tgSendMessage(env, oldNoSlotsText, true);
    noSlotsSentAt = now;
  }

  const slotsSerial = {};
  for (const date of Object.keys(current).sort()) slotsSerial[date] = [...current[date]].sort();
  const json = JSON.stringify({
    message_id_schedule: msgSchedule,
    old_schedule_text: oldScheduleText,
    message_id_no_slots: msgNoSlots,
    old_no_slots_text: oldNoSlotsText,
    time_of_new_slots: timeOfNew ? new Date(timeOfNew).toISOString() : null,
    last_time_slots_found: lastFound ? new Date(lastFound).toISOString() : null,
    current_slots: slotsSerial,
    doctor_id: String(doctorId),
    schedule_sent_at: msgSchedule && scheduleSentAt ? new Date(scheduleSentAt).toISOString() : null,
    no_slots_sent_at: msgNoSlots && noSlotsSentAt ? new Date(noSlotsSentAt).toISOString() : null,
    retire_later: retireLater,
  });
  // Пишем только изменения: у бесплатного D1 лимит на записи в сутки
  if (json !== raw) await saveState(env, json);
}

export default {
  async scheduled(event, env, ctx) {
    await check(env);
  },
};
