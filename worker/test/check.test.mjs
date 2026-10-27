// Сценарии бота на подменённых SberHealth и Телеграме; D1 — настоящий SQLite (node:sqlite).
// Подменный Телеграм ведёт себя как настоящий: правка с тем же видимым текстом -> «message is not modified»
// (в Markdown `**10:00**` выглядит как `10:00`), сообщение старше 48 часов не удаляется.
// Запуск: npm test (из папки worker)
import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";

const { check, formatDateRussian, formatDatetimeRussian } =
  await import(process.env.WORKER_SRC || "../src/index.js");

const H = 3600 * 1000;
const MIN = 60 * 1000;
const T0 = Date.parse("2026-10-27T00:00:00+03:00");

// ---------- подменная D1 ----------

function fakeD1() {
  const db = new DatabaseSync(":memory:");
  let stateWrites = 0;
  return {
    get stateWrites() { return stateWrites; },
    state() {
      try { return JSON.parse(db.prepare("SELECT v FROM state WHERE k = 'state'").get()?.v ?? "null"); }
      catch { return null; }
    },
    seed(json) {
      db.exec("CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT NOT NULL)");
      db.prepare("INSERT INTO state (k, v) VALUES ('state', ?)").run(json);
    },
    prepare(sql) {
      let args = [];
      return {
        bind(...a) { args = a; return this; },
        async run() {
          const r = db.prepare(sql).run(...args);
          if (sql.includes("VALUES ('state'")) stateWrites++;
          return { meta: { changes: Number(r.changes) } };
        },
        async first() { return db.prepare(sql).get(...args) ?? null; },
      };
    },
  };
}

// ---------- подменные SberHealth и Телеграм ----------

let w; // «мир» текущего теста
const rendered = (t) => t.replaceAll("**", "");
const reply = (status, body) => new Response(JSON.stringify(body), { status });
const bad = (description) => reply(400, { ok: false, description });

globalThis.fetch = async (url, opts = {}) => {
  url = String(url);
  if (url.includes("sberhealth")) {
    if (w.sberFail) throw new Error("connection reset");
    if (w.gates.length) await w.gates.shift(); // «долгий» запрос — держим, пока тест не отпустит
    await new Promise((r) => setTimeout(r, 1)); // запросы не мгновенные — даём запускам перемешаться
    return reply(200, { slots: w.sber.map(([d, t]) => ({ from: `${d}T${t}:00+03:00` })) });
  }
  const method = url.split("/").pop();
  const b = JSON.parse(opts.body);
  await new Promise((r) => setTimeout(r, 1)); // и Телеграм отвечает не мгновенно
  w.calls.push({ method, id: b.message_id, silent: b.disable_notification, first: (b.text || "").split("\n")[0] });
  if (w.tgDown) throw new Error("network down");
  const m = w.chat.get(b.message_id);
  if (method === "sendMessage") {
    const id = w.nextId++;
    w.chat.set(id, { text: rendered(b.text), sentAt: w.clock });
    return reply(200, { ok: true, result: { message_id: id } });
  }
  if (method === "editMessageText") {
    if (w.editGlitch) return reply(500, { ok: false, description: "Internal Server Error" });
    if (!m) return bad("Bad Request: message to edit not found");
    if (m.text === rendered(b.text)) return bad("Bad Request: message is not modified: specified new message content and reply markup are exactly the same as a current content and reply markup of the message");
    m.text = rendered(b.text);
    return reply(200, { ok: true, result: { message_id: b.message_id } });
  }
  if (method === "deleteMessage") {
    if (!m) return bad("Bad Request: message to delete not found");
    if (w.clock - m.sentAt >= 48 * H) return bad("Bad Request: message can't be deleted for everyone");
    w.chat.delete(b.message_id);
    return reply(200, { ok: true, result: true });
  }
  throw new Error("unexpected " + method);
};

beforeEach(() => {
  w = { clock: 0, sber: [], sberFail: false, tgDown: false, editGlitch: false, nextId: 100, chat: new Map(), calls: [], gates: [] };
  w.db = fakeD1();
  w.env = { DB: w.db, DOCTOR_ID: "1234567", TELEGRAM_BOT_TOKEN: "x", TELEGRAM_CHAT_ID: "1" };
});

/** Один запуск cron в момент `at`; возвращает вызовы Телеграма [метод, id, тихо, первая строка]. */
async function run(at) {
  w.clock = at;
  const from = w.calls.length;
  await check(w.env, at);
  return w.calls.slice(from).map((c) => [c.method, c.id ?? "", c.silent ?? "", c.first]);
}
const sends = () => w.calls.filter((c) => c.method === "sendMessage");
const chatTexts = () => [...w.chat.values()].map((m) => m.text.replaceAll("\n", " / ")).join("\n");
const onlyMessage = () => {
  assert.equal(w.chat.size, 1, "в чате должно быть одно сообщение бота:\n" + chatTexts());
  return [...w.chat.values()][0].text;
};
const URL_LINE = "🗓 **[Доступные записи](https://lk.sberhealth.ru/telemed/speciality/47/doctor/1234567):**";

// ---------- сценарии ----------

test("даты по-русски и по Москве", () => {
  assert.equal(formatDateRussian("2026-10-25"), "25 октября (воскресенье)");
  assert.equal(formatDatetimeRussian(Date.parse("2026-10-23T09:51:35.015757+03:00")), "23 октября в 09:51");
});

test("полный цикл: нет слотов -> появились -> меняются -> пропали", async () => {
  assert.deepEqual(await run(T0), [["sendMessage", "", false, "🔴 *Слотов нет* 🔴"]]);

  w.sber = [["2026-10-28", "10:00"], ["2026-10-28", "11:00"]];
  assert.deepEqual(await run(T0 + MIN), [
    ["deleteMessage", 100, "", ""],
    ["sendMessage", "", false, "🟢 *Появились слоты* 🟢"], // с нуля — громко
  ]);

  w.sber = [["2026-10-28", "10:00"]];
  assert.deepEqual(await run(T0 + 2 * MIN), [["editMessageText", 101, "", "🟢 *Появились слоты* 🟢"]]);

  w.sber = [["2026-10-28", "10:00"], ["2026-10-28", "12:00"]];
  assert.deepEqual(await run(T0 + 3 * MIN), [
    ["deleteMessage", 101, "", ""],
    ["sendMessage", "", true, "🟢 *Появились слоты* 🟢"], // добавилось к имеющимся — тихо
  ]);

  assert.deepEqual(await run(T0 + 70 * MIN), [["editMessageText", 102, "", URL_LINE]]); // через час баннер снят
  const writes = w.db.stateWrites;
  assert.deepEqual(await run(T0 + 71 * MIN), []);
  assert.equal(w.db.stateWrites, writes, "без изменений состояние не пишется");

  w.sber = [];
  assert.deepEqual(await run(T0 + 72 * MIN), [
    ["deleteMessage", 102, "", ""],
    ["sendMessage", "", false, "🔴 *Слотов нет* 🔴"],
  ]);
  assert.match(onlyMessage(), /Появлялись 27 октября в 00:03/);
});

test("сбой SberHealth — тишина и без записи состояния", async () => {
  await run(T0);
  const writes = w.db.stateWrites;
  w.sberFail = true;
  assert.deepEqual(await run(T0 + MIN), []);
  assert.equal(w.db.stateWrites, writes);
});

test("снятие выделения нового времени не плодит дубли (жалоба 27.10)", async () => {
  w.sber = [["2026-10-27", "09:00"], ["2026-11-09", "09:00"]];
  await run(T0);
  w.sber = [["2026-10-30", "10:00"], ["2026-11-09", "09:00"]];
  await run(T0 + MIN); // 30.10 10:00 выделено
  await run(T0 + 2 * MIN); // выделение снимается — Телеграм: «не изменилось»
  await run(T0 + 3 * MIN);
  onlyMessage();
  assert.equal(sends().length, 2, "снятие выделения — правка, а не новое сообщение");
});

test("«Слотов нет» висит 3 дня — при появлении слотов исчезает", async () => {
  for (let t = T0; t < T0 + 72 * H; t += 30 * MIN) await run(t);
  w.sber = [["2026-11-02", "12:00"]];
  await run(T0 + 72 * H);
  assert.match(onlyMessage(), /Появились слоты/);
  const refreshes = sends().filter((s) => s.silent === true && s.first.includes("Слотов нет"));
  assert.ok(refreshes.length >= 1, "старое «Слотов нет» перевыкладывается тихо до 48 часов");
});

test("расписание висит 3 дня — при пропаже слотов исчезает", async () => {
  w.sber = [["2026-11-02", "12:00"]];
  for (let t = T0; t < T0 + 72 * H; t += 30 * MIN) await run(t);
  w.sber = [];
  await run(T0 + 72 * H);
  assert.match(onlyMessage(), /Слотов нет/);
});

test("разовый сбой правки (500) — старое сообщение не остаётся дублем", async () => {
  w.sber = [["2026-11-10", "10:00"], ["2026-11-10", "11:00"]];
  await run(T0);
  w.sber = [["2026-11-10", "10:00"]];
  w.editGlitch = true;
  await run(T0 + MIN);
  w.editGlitch = false;
  await run(T0 + 2 * MIN);
  assert.match(onlyMessage(), /10 ноября \(вторник\): 10:00$/);
});

test("связь пропала, когда слоты исчезли — старое убирается позже, «Слотов нет» громко", async () => {
  w.sber = [["2026-11-10", "10:00"]];
  await run(T0);
  w.sber = [];
  w.tgDown = true;
  await run(T0 + MIN);
  w.tgDown = false;
  await run(T0 + 2 * MIN);
  await run(T0 + 3 * MIN);
  assert.match(onlyMessage(), /Слотов нет/);
  assert.equal(sends().at(-1).silent, false, "о пропаже слотов — громко, даже если первая отправка сорвалась");
  assert.deepEqual(w.db.state().retire_later, []);
});

test("сорванная перевыкладка «Слотов нет» восстанавливается тихо", async () => {
  await run(T0);
  w.tgDown = true;
  await run(T0 + 47 * H); // пора перевыкладывать, но связи нет
  w.tgDown = false;
  await run(T0 + 47 * H + MIN);
  onlyMessage();
  assert.equal(sends().at(-1).silent, true);
});

test("Телеграм не принял громкое «Появились слоты» — повтор тоже громкий", async () => {
  await run(T0);
  w.sber = [["2026-10-28", "10:00"]];
  w.tgDown = true;
  await run(T0 + MIN);
  w.tgDown = false;
  await run(T0 + 2 * MIN);
  assert.deepEqual([sends().at(-1).first, sends().at(-1).silent], ["🟢 *Появились слоты* 🟢", false]);
  assert.match(onlyMessage(), /Появились слоты/);
});

test("подхват состояния Python-бота: одна тихая перевыкладка, дальше тишина", async () => {
  w.chat.set(1329, { text: "старое расписание", sentAt: T0 - 9 * H });
  w.db.seed(JSON.stringify({
    message_id_schedule: 1329,
    old_schedule_text: "🟢 *Появились слоты* 🟢\n\n" + URL_LINE + "\n9 ноября (понедельник): 09:00",
    message_id_no_slots: null, old_no_slots_text: null,
    time_of_new_slots: "2026-10-27T00:00:03.441628+03:00",
    last_time_slots_found: "2026-10-27T00:00:04.137089+03:00",
    current_slots: { "2026-11-09": ["09:00"] }, doctor_id: "1234567",
  }));
  w.sber = [["2026-11-09", "09:00"]];
  assert.deepEqual(await run(T0 + 10 * MIN), [
    ["deleteMessage", 1329, "", ""],
    ["sendMessage", "", true, "🟢 *Появились слоты* 🟢"],
  ]);
  assert.deepEqual(await run(T0 + 11 * MIN), []);
});

test("два запуска одновременно — одно сообщение, а не дубль", async () => {
  await run(T0);
  w.sber = [["2026-10-28", "10:00"]];
  w.clock = T0 + MIN;
  await Promise.all([check(w.env, T0 + MIN), check(w.env, T0 + MIN)]);
  assert.match(onlyMessage(), /Появились слоты/);
  assert.equal(sends().length, 2);
});

test("долгий запуск не снимает чужой замок", async () => {
  await run(T0);
  let openA, openB;
  w.gates = [new Promise((r) => (openA = r)), new Promise((r) => (openB = r))];
  const tick = () => new Promise((r) => setTimeout(r, 5));
  const a = check(w.env, T0 + MIN); // A завис на запросе к SberHealth
  await tick();
  const b = check(w.env, T0 + 7 * MIN); // замок A истёк — B его перехватил
  await tick();
  openA();
  await a; // A закончил и снимает только свой замок
  w.sber = [["2026-10-28", "10:00"]];
  assert.deepEqual(await run(T0 + 7 * MIN), [], "B ещё работает — третий запуск пропускает ход");
  openB();
  await b;
});

test("за один запуск убирается не больше трёх старых сообщений", async () => {
  for (const id of [1, 2, 3, 4, 5]) w.chat.set(id, { text: "старое", sentAt: T0 });
  w.db.seed(JSON.stringify({ current_slots: {}, retire_later: [1, 2, 3, 4, 5] }));
  await run(T0 + MIN);
  assert.deepEqual([...w.chat.keys()].filter((id) => id <= 5), [4, 5]);
  assert.deepEqual(w.db.state().retire_later, [4, 5]);
  await run(T0 + 2 * MIN);
  assert.deepEqual([...w.chat.keys()].filter((id) => id <= 5), []);
});

test("замок от упавшего запуска не вечный", async () => {
  await run(T0);
  await w.env.DB.prepare("UPDATE state SET v = ?1 WHERE k = 'lock'").bind(String(T0 + MIN + 5 * MIN)).run();
  w.sber = [["2026-10-28", "10:00"]];
  assert.deepEqual(await run(T0 + MIN), [], "пока замок свежий — ход пропускается");
  assert.equal((await run(T0 + 7 * MIN)).at(-1)[3], "🟢 *Появились слоты* 🟢", "после срока замка — работает");
});
