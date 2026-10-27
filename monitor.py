import os
import time
import random
import requests
import datetime
import json
from typing import Dict, Set, Optional
from dotenv import load_dotenv

# ---------------------------------------------
# Папка для хранения состояния
# ---------------------------------------------

load_dotenv()  # Загружаем переменные из .env (если есть)

DATA_FOLDER = os.environ.get("DATA_FOLDER", "/data")
STATE_FILE = os.path.join(DATA_FOLDER, "state.json")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DOCTOR_ID = os.environ.get("DOCTOR_ID", "")
# Если ID неизвестен — ищем врача по ФИО в общем списке психологов
DOCTOR_NAME = os.environ.get("DOCTOR_NAME", "")

CHECK_INTERVAL = 30  # Базовый интервал (сек) между запросами

# ---------------------------------------------
# «Антифрод»: случайные заголовки + задержка
# ---------------------------------------------

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) "
    "Gecko/20100101 Firefox/109.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_1) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.1 Safari/605.1.15"
]

def get_headers() -> dict:
    """Формируем «случайные» заголовки, чтобы не выглядеть как бот."""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;"
                  "q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1"
    }

def random_sleep(base_interval: int):
    """
    Делает паузу base_interval + random(1..15), чтобы не выглядело слишком роботизированно.
    """
    extra = random.uniform(1, 15)
    time.sleep(base_interval + extra)

# ---------------------------------------------
# Локализация
# ---------------------------------------------

WEEKDAYS_RU = [
    "понедельник", "вторник", "среда",
    "четверг", "пятница", "суббота", "воскресенье"
]

MONTHS_RU = [
    "",  # индекс 0 пуст
    "января", "февраля", "марта", "апреля",
    "мая", "июня", "июля", "августа",
    "сентября", "октября", "ноября", "декабря"
]

def format_date_russian(date_obj: datetime.date) -> str:
    """2025-01-26 => '26 января (воскресенье)'."""
    day = date_obj.day
    month = date_obj.month
    weekday = date_obj.weekday()  # Пн=0..Вс=6
    return f"{day} {MONTHS_RU[month]} ({WEEKDAYS_RU[weekday]})"

def format_datetime_russian(dt: datetime.datetime) -> str:
    """2025-01-26 19:30 => '26 января в 19:30'."""
    day = dt.day
    month = dt.month
    hour = dt.hour
    minute = dt.minute
    return f"{day} {MONTHS_RU[month]} в {hour:02d}:{minute:02d}"

# ---------------------------------------------
# Запрос слотов (API)
# ---------------------------------------------

API_BASE = "https://telemed-patient-bff.sberhealth.ru/api/showcase/web/v1"
SPECIALITY_ID = 47  # «Психолог» на SberHealth

def doctor_url(doctor_id: str) -> str:
    return f"https://lk.sberhealth.ru/telemed/speciality/{SPECIALITY_ID}/doctor/{doctor_id}"

def normalize_name(name: str) -> list:
    return name.lower().replace("ё", "е").split()

def find_doctor_id(name: str) -> Optional[str]:
    """
    Ищем врача по ФИО в списке психологов.
    В списке только врачи, у которых сейчас есть слоты, поэтому
    None — это не ошибка, а «пока слотов нет».
    """
    wanted = normalize_name(name)
    page = 1
    while True:
        resp = requests.get(
            f"{API_BASE}/specialities/{SPECIALITY_ID}/doctors",
            params={"timezone": "Europe/Moscow", "perPage": 100, "page": page},
            headers=get_headers(), timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        for doc in data.get("doctors", []):
            full_name = normalize_name(doc.get("fullName", ""))
            if all(part in full_name for part in wanted):
                print(f"[i] Нашёл врача «{doc.get('fullName')}»: DOCTOR_ID={doc['id']}")
                return str(doc["id"])
        if page >= data.get("pagination", {}).get("countPage", 1):
            return None
        page += 1

def fetch_slots(doctor_id: str) -> Dict[str, Set[str]]:
    """
    Возвращает словарь:
      {
        'YYYY-MM-DD': {'HH:MM', 'HH:MM'...},
        ...
      }
    или пустой словарь, если слотов нет. При ошибке запроса — исключение,
    чтобы сбой сети не выглядел как «слотов нет».
    """
    h = get_headers()
    resp = requests.get(
        f"{API_BASE}/providers/62/doctors/{doctor_id}/specialties/psychologist/slots",
        headers=h, timeout=15
    )
    resp.raise_for_status()
    data = resp.json()
    raw_slots = data.get("slots", [])

    result = {}
    for slot in raw_slots:
        slot_time = slot.get("from")
        if not slot_time:
            continue
        dt = datetime.datetime.fromisoformat(slot_time)
        date_str = dt.date().isoformat()
        t_str = dt.strftime("%H:%M")
        if date_str not in result:
            result[date_str] = set()
        result[date_str].add(t_str)

    return result

# ---------------------------------------------
# Формирование текста
# ---------------------------------------------

def build_schedule_message(
    doctor_id: str,
    slots: Dict[str, Set[str]],
    show_new_alert: bool,
    highlight: Optional[Dict[str, Set[str]]] = None
) -> str:
    """
    Генерируем текст (Markdown):
      - Если show_new_alert=True, добавим фразу "Появились слоты"
      - "highlight" - те слоты (дата->время), которые сейчас "добавились" (выделить жирным).
    """
    lines = []
    if show_new_alert:
        lines.append("🟢 *Появились слоты* 🟢\n")

    # Делаем ссылку на "Доступные записи"
    lines.append(f"🗓 **[Доступные записи]({doctor_url(doctor_id)}):**")
    
    if highlight is None:
        highlight = {}  # пустое, ничего не выделяем

    all_dates = sorted(slots.keys())
    if not all_dates:
        lines.append("_(пока пусто)_")
    else:
        for date_str in all_dates:
            date_obj = datetime.date.fromisoformat(date_str)
            date_human = format_date_russian(date_obj)
            highlight_times = highlight.get(date_str, set())

            all_times = sorted(slots[date_str])
            times_styled = []
            for t in all_times:
                if t in highlight_times:
                    times_styled.append(f"**{t}**")
                else:
                    times_styled.append(t)

            times_str = ", ".join(times_styled)
            lines.append(f"{date_human}: {times_str}")

    return "\n".join(lines)

# ---------------------------------------------
# Телеграм (с поддержкой disable_notification)
# ---------------------------------------------

def tg_send_message(bot_token: str, chat_id: str, text: str, silent: bool = False) -> Optional[int]:
    """
    Если silent=True, то ставим disable_notification=True (тихое уведомление).
    """
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_notification": silent
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data["result"]["message_id"]
    except Exception as e:
        print("[!] Ошибка tg_send_message:", e)
        return None

def tg_delete_message(bot_token: str, chat_id: str, msg_id: int):
    url = f"https://api.telegram.org/bot{bot_token}/deleteMessage"
    payload = {
        "chat_id": chat_id,
        "message_id": msg_id
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print("[!] Ошибка tg_delete_message:", e)

def tg_edit_message(bot_token: str, chat_id: str, msg_id: int, new_text: str) -> bool:
    """
    Редактируем существующее сообщение (нет опции disable_notification в editMessageText).
    False — не вышло (например, сообщение удалили в чате), тогда шлём новое.
    """
    url = f"https://api.telegram.org/bot{bot_token}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": msg_id,
        "text": new_text,
        "parse_mode": "Markdown"
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except requests.exceptions.HTTPError:
        # В Markdown `**10:00**` выглядит как `10:00`: сообщение уже показывает нужный текст
        if "message is not modified" in r.text:
            return True
        print("[!] tg_edit_message HTTPError:", r.status_code, r.text)
        return False
    except Exception as e:
        print("[!] tg_edit_message Exception:", e)
        return False

# ---------------------------------------------
# find_added_slots + find_removed_slots
# ---------------------------------------------

def find_added_slots(old: Dict[str, Set[str]], new: Dict[str, Set[str]]) -> Dict[str, Set[str]]:
    added = {}
    for d, new_times in new.items():
        old_times = old.get(d, set())
        diff = new_times - old_times
        if diff:
            added[d] = diff
    return added

def find_removed_slots(old: Dict[str, Set[str]], new: Dict[str, Set[str]]) -> Dict[str, Set[str]]:
    removed = {}
    for d, old_times in old.items():
        new_times = new.get(d, set())
        diff = old_times - new_times
        if diff:
            removed[d] = diff
    return removed

# ---------------------------------------------
# Баннер
# ---------------------------------------------

def still_show_banner(slots: Dict[str, Set[str]], t_new: Optional[datetime.datetime]) -> bool:
    """
    Если прошло >1 часа с момента появления или слоты обнулились, выключаем баннер.
    """
    if not t_new:
        return False
    # ВСЕГДА offset-aware (таймзона +3)
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))
    diff_sec = (now - t_new).total_seconds()
    if diff_sec > 3600:
        return False
    total = sum(len(ts) for ts in slots.values())
    if total == 0:
        return False
    return True

# ---------------------------------------------
# Хранилище state.json
# ---------------------------------------------

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print("[!] Ошибка загрузки state.json:", e)
        return {}

def save_state(st: dict):
    os.makedirs(DATA_FOLDER, exist_ok=True)
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[!] Ошибка сохранения state.json:", e)

def state_to_dict(
    message_id_schedule: Optional[int],
    old_schedule_text: Optional[str],
    message_id_no_slots: Optional[int],
    old_no_slots_text: Optional[str],
    time_of_new_slots: Optional[datetime.datetime],
    last_time_slots_found: Optional[datetime.datetime],
    current_slots: Dict[str, Set[str]],
    doctor_id: Optional[str]
) -> dict:
    slots_serial = {}
    for d, tset in current_slots.items():
        slots_serial[d] = sorted(list(tset))

    # Сохраняем offset-aware datetime в isoformat
    t_new_str = time_of_new_slots.isoformat() if time_of_new_slots else None
    last_str = last_time_slots_found.isoformat() if last_time_slots_found else None

    return {
        "message_id_schedule": message_id_schedule,
        "old_schedule_text": old_schedule_text,
        "message_id_no_slots": message_id_no_slots,
        "old_no_slots_text": old_no_slots_text,
        "time_of_new_slots": t_new_str,
        "last_time_slots_found": last_str,
        "current_slots": slots_serial,
        "doctor_id": doctor_id
    }

def dict_to_state(d: dict):
    msg_id_sched = d.get("message_id_schedule")
    old_sched_txt = d.get("old_schedule_text")
    msg_id_noslots = d.get("message_id_no_slots")
    old_noslots_txt = d.get("old_no_slots_text")

    # Берём isoformat, создаём offset-aware datetime
    time_new_str = d.get("time_of_new_slots")
    if time_new_str:
        time_of_new = datetime.datetime.fromisoformat(time_new_str)
    else:
        time_of_new = None

    last_time_str = d.get("last_time_slots_found")
    if last_time_str:
        last_time_found = datetime.datetime.fromisoformat(last_time_str)
    else:
        last_time_found = None

    curr = {}
    for day, tlist in d.get("current_slots", {}).items():
        curr[day] = set(tlist)

    return (
        msg_id_sched,
        old_sched_txt,
        msg_id_noslots,
        old_noslots_txt,
        time_of_new,
        last_time_found,
        curr
    )

# ---------------------------------------------
# Основной цикл
# ---------------------------------------------

def run_monitor():
    st = load_state()
    if st:
        (
            message_id_schedule,
            old_schedule_text,
            message_id_no_slots,
            old_no_slots_text,
            time_of_new_slots,
            last_time_slots_found,
            current_slots
        ) = dict_to_state(st)
    else:
        message_id_schedule = None
        old_schedule_text = None
        message_id_no_slots = None
        old_no_slots_text = None
        time_of_new_slots = None
        last_time_slots_found = None
        current_slots = {}

    # ID из .env важнее; иначе — тот, что уже нашли по ФИО раньше
    doctor_id = DOCTOR_ID or st.get("doctor_id")

    while True:
        try:
            if not doctor_id:
                doctor_id = find_doctor_id(DOCTOR_NAME)
            new_slots = fetch_slots(doctor_id) if doctor_id else {}
        except Exception as e:
            print("[!] Ошибка запроса к SberHealth:", e)
            random_sleep(CHECK_INTERVAL)
            continue

        # 1) Нет слотов
        if not new_slots:
            # Удаляем сообщение с расписанием, если было
            if message_id_schedule is not None:
                tg_delete_message(BOT_TOKEN, CHAT_ID, message_id_schedule)
                message_id_schedule = None
                old_schedule_text = None

            if last_time_slots_found:
                # Выводим offset-aware время
                last_str = format_datetime_russian(last_time_slots_found.astimezone(datetime.timezone(datetime.timedelta(hours=3))))
                new_no_slots_text = (
                    f"🔴 *Слотов нет* 🔴\n\n"
                    f"Не волнуйтесь — как только освободится окошко, сразу напишу 🙏🏻\n\n"
                    f"_(Появлялись {last_str})_"
                )
            else:
                new_no_slots_text = (
                    "🔴 *Слотов нет* 🔴\n\n"
                    "Не волнуйтесь — как только освободится окошко, сразу напишу 🙏🏻"
                )

            if message_id_no_slots and old_no_slots_text != new_no_slots_text:
                # Редактируем "нет слотов"
                if tg_edit_message(BOT_TOKEN, CHAT_ID, message_id_no_slots, new_no_slots_text):
                    old_no_slots_text = new_no_slots_text
                else:
                    message_id_no_slots = None  # сообщение пропало — пришлём заново
            if not message_id_no_slots:
                msg_id = tg_send_message(BOT_TOKEN, CHAT_ID, new_no_slots_text, silent=False)
                message_id_no_slots = msg_id
                old_no_slots_text = new_no_slots_text

            current_slots = {}

            st_dict = state_to_dict(
                message_id_schedule, old_schedule_text,
                message_id_no_slots, old_no_slots_text,
                time_of_new_slots, last_time_slots_found,
                current_slots, doctor_id
            )
            save_state(st_dict)

            random_sleep(CHECK_INTERVAL)
            continue

        # 2) Есть слоты
        # Удаляем "нет слотов" если оно есть
        if message_id_no_slots:
            tg_delete_message(BOT_TOKEN, CHAT_ID, message_id_no_slots)
            message_id_no_slots = None
            old_no_slots_text = None

        # Если раньше было пусто, а теперь появились => громкое уведомление
        no_to_yes = (not current_slots)

        # Если было пусто => ставим last_time_slots_found в +3
        if not current_slots:
            last_time_slots_found = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))

        added = find_added_slots(current_slots, new_slots)
        removed = find_removed_slots(current_slots, new_slots)

        if added:
            # Удаляем старое сообщение (если было)
            if message_id_schedule:
                tg_delete_message(BOT_TOKEN, CHAT_ID, message_id_schedule)
                message_id_schedule = None
                old_schedule_text = None

            time_of_new_slots = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))

            highlight_times = added if current_slots else None

            new_text = build_schedule_message(
                doctor_id,
                new_slots,
                show_new_alert=True,
                highlight=highlight_times
            )

            is_silent = (not no_to_yes)
            msg_id = tg_send_message(BOT_TOKEN, CHAT_ID, new_text, silent=is_silent)
            message_id_schedule = msg_id
            old_schedule_text = new_text

            last_time_slots_found = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))

            # Не отправилось — не запоминаем слоты, чтобы в следующий раз снова прислать громко
            if msg_id is not None:
                current_slots = new_slots

        else:
            # Нет новых слотов, но могли пропасть
            show_banner = still_show_banner(new_slots, time_of_new_slots)
            highlight_times = None
            new_text = build_schedule_message(doctor_id, new_slots, show_banner, highlight_times)

            if message_id_schedule and old_schedule_text != new_text:
                if tg_edit_message(BOT_TOKEN, CHAT_ID, message_id_schedule, new_text):
                    old_schedule_text = new_text
                else:
                    message_id_schedule = None  # сообщение пропало — пришлём заново
            if not message_id_schedule:
                # Создаем заново (тихое, так как не "с нуля" появилось)
                msg_id = tg_send_message(BOT_TOKEN, CHAT_ID, new_text, silent=True)
                message_id_schedule = msg_id
                old_schedule_text = new_text

            current_slots = new_slots

        # Сохраняем состояние
        st_dict = state_to_dict(
            message_id_schedule, old_schedule_text,
            message_id_no_slots, old_no_slots_text,
            time_of_new_slots, last_time_slots_found,
            current_slots, doctor_id
        )
        save_state(st_dict)

        random_sleep(CHECK_INTERVAL)


# ---------------------------------------------
# Запуск
# ---------------------------------------------
if __name__ == "__main__":
    if not DOCTOR_ID and not DOCTOR_NAME:
        raise SystemExit("Укажите в .env DOCTOR_ID или DOCTOR_NAME")
    os.makedirs(DATA_FOLDER, exist_ok=True)
    run_monitor()