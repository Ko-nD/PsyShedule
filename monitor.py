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

DATA_FOLDER = "data"
STATE_FILE = os.path.join(DATA_FOLDER, "state.json")

load_dotenv()  # Загружаем переменные из .env (если есть)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DOCTOR_ID = os.environ.get("DOCTOR_ID", "")

CHECK_INTERVAL = 30  # Базовый интервал между запросами (сек)

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
    """Формируем «случайные» заголовки, чтобы не выглядеть как простой скрипт."""
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
    Добавляем базовый интервал + небольшую случайную паузу (1..15 секунд),
    чтобы не стучаться слишком ровно и не выглядеть как «бот».
    """
    extra = random.uniform(1, 15)
    time.sleep(base_interval + extra)

# ---------------------------------------------
# Локализация
# ---------------------------------------------

WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTHS_RU = [
    "",  # индекс 0 пустой, чтобы month (1..12) удобно использовать
    "января", "февраля", "марта", "апреля",
    "мая", "июня", "июля", "августа",
    "сентября", "октября", "ноября", "декабря"
]

def format_date_russian(date_obj: datetime.date) -> str:
    """'2025-01-26' => '26 января (воскресенье)'"""
    day = date_obj.day
    month = date_obj.month
    weekday = date_obj.weekday()  # Пн=0 ... Вс=6
    return f"{day} {MONTHS_RU[month]} ({WEEKDAYS_RU[weekday]})"

def format_datetime_russian(dt: datetime.datetime) -> str:
    """Дата-время в стиле '26 января 2025, 19:30'."""
    day = dt.day
    month = dt.month
    year = dt.year
    hour = dt.hour
    minute = dt.minute
    month_ru = MONTHS_RU[month]
    return f"{day} {month_ru} {year} {hour:02d}:{minute:02d}"

# ---------------------------------------------
# Запрос свободных слотов
# ---------------------------------------------

API_URL = (
    "https://telemed-patient-bff.sberhealth.ru/api/showcase/web/v1/"
    f"providers/62/doctors/{DOCTOR_ID}/specialties/psychologist/slots"
)

DOCTOR_URL = f"https://lk.sberhealth.ru/catalog/onlayn-konsultatsii/psikholog-katalog/doctor/{DOCTOR_ID}"

def fetch_slots() -> Dict[str, Set[str]]:
    """
    Делает GET к API и возвращает словарь формата:
    {
      'YYYY-MM-DD': {'HH:MM', ...},
      ...
    }
    Если запрос не удался — вернётся пустой словарь.
    """
    try:
        headers = get_headers()
        resp = requests.get(API_URL, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        raw_slots = data.get("slots", [])

        result = {}
        for slot_info in raw_slots:
            slot_time_str = slot_info.get("from")
            if not slot_time_str:
                continue

            dt = datetime.datetime.fromisoformat(slot_time_str)  # учтём +03:00
            date_str = dt.date().isoformat()  # "2025-01-26"
            time_str = dt.strftime("%H:%M")   # "19:30"

            if date_str not in result:
                result[date_str] = set()
            result[date_str].add(time_str)

        return result

    except Exception as e:
        print(f"[!] fetch_slots: Ошибка запроса: {e}")
        return {}

# ---------------------------------------------
# Формирование текста
# ---------------------------------------------

def build_schedule_message(
    slots: Dict[str, Set[str]],
    show_new_alert: bool
) -> str:
    """
    Генерируем сообщение (Markdown) со списком слотов.
    """
    lines = []
    if show_new_alert:
        lines.append("🟢 [Появились]({DOCTOR_URL}) новые слоты 🟢\n")

    lines.append("🗓 *Доступные записи:*")

    all_dates = sorted(slots.keys())
    if not all_dates:
        lines.append("_(пока пусто)_")
    else:
        for date_str in all_dates:
            date_obj = datetime.date.fromisoformat(date_str)
            date_human = format_date_russian(date_obj)
            times = sorted(slots[date_str])
            times_str = ", ".join(times)
            lines.append(f"{date_human}: {times_str}")

    return "\n".join(lines)

# ---------------------------------------------
# Телеграм методы
# ---------------------------------------------

def tg_send_message(bot_token: str, chat_id: str, text: str) -> Optional[int]:
    """Отправляет новое сообщение. Возвращает message_id."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data["result"]["message_id"]
    except Exception as e:
        print(f"[!] Ошибка tg_send_message: {e}")
        return None

def tg_delete_message(bot_token: str, chat_id: str, message_id: int):
    """Удаляет сообщение по ID."""
    url = f"https://api.telegram.org/bot{bot_token}/deleteMessage"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"[!] Ошибка tg_delete_message: {e}")

def tg_edit_message(bot_token: str, chat_id: str, message_id: int, new_text: str):
    """Редактирует сообщение (заменяет текст)."""
    url = f"https://api.telegram.org/bot{bot_token}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": new_text,
        "parse_mode": "Markdown"
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
    except requests.exceptions.HTTPError:
        print("[!] tg_edit_message HTTPError:", resp.status_code, resp.text)
        raise
    except Exception as e:
        print("[!] tg_edit_message Exception:", e)
        raise

# ---------------------------------------------
# Работа со слотами
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

def still_show_banner(slots: Dict[str, Set[str]], time_new: Optional[datetime.datetime]) -> bool:
    """
    Баннер «Появились новые слоты» показываем не дольше часа и пока слоты не кончились.
    """
    if not time_new:
        return False
    now = datetime.datetime.now()
    diff = (now - time_new).total_seconds()
    if diff > 3600:
        return False
    total_slots = sum(len(s) for s in slots.values())
    if total_slots == 0:
        return False
    return True

# ---------------------------------------------
# Хранилище состояния (файл state.json)
# ---------------------------------------------

def load_state() -> dict:
    """Загружаем состояние из файла, если он есть. Иначе возвращаем пустой dict."""
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except Exception as e:
        print("[!] Ошибка загрузки state.json:", e)
        return {}

def save_state(state: dict):
    """Сохраняем состояние в файл state.json (JSON)."""
    # Убедимся, что папка data/ существует:
    os.makedirs(DATA_FOLDER, exist_ok=True)
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[!] Ошибка сохранения state.json:", e)


def state_to_dict(
    message_id_schedule: Optional[int],
    old_schedule_text: Optional[str],
    message_id_no_slots: Optional[int],
    old_no_slots_text: Optional[str],
    time_of_new_slots: Optional[datetime.datetime],
    last_time_slots_found: Optional[datetime.datetime],
    current_slots: Dict[str, Set[str]]
) -> dict:
    """
    Превращаем переменные состояния в сериализуемый словарь.
    Добавили old_no_slots_text для хранения текста «нет слотов».
    """
    # current_slots: set -> list
    slots_serializable = {}
    for date_str, times_set in current_slots.items():
        slots_serializable[date_str] = sorted(list(times_set))

    # Даты -> isoformat
    time_new_str = time_of_new_slots.isoformat() if time_of_new_slots else None
    last_slots_str = last_time_slots_found.isoformat() if last_time_slots_found else None

    return {
        "message_id_schedule": message_id_schedule,
        "old_schedule_text": old_schedule_text,
        "message_id_no_slots": message_id_no_slots,
        "old_no_slots_text": old_no_slots_text,
        "time_of_new_slots": time_new_str,
        "last_time_slots_found": last_slots_str,
        "current_slots": slots_serializable
    }


def dict_to_state(data: dict):
    """
    Обратная операция: восстанавливаем переменные состояния из словаря.
    """
    message_id_schedule = data.get("message_id_schedule")
    old_schedule_text = data.get("old_schedule_text")
    message_id_no_slots = data.get("message_id_no_slots")
    old_no_slots_text = data.get("old_no_slots_text")

    # time_of_new_slots
    time_of_new_slots_str = data.get("time_of_new_slots")
    if time_of_new_slots_str:
        time_of_new_slots = datetime.datetime.fromisoformat(time_of_new_slots_str)
    else:
        time_of_new_slots = None

    # last_time_slots_found
    last_slots_str = data.get("last_time_slots_found")
    if last_slots_str:
        last_time_slots_found = datetime.datetime.fromisoformat(last_slots_str)
    else:
        last_time_slots_found = None

    # current_slots
    current_slots_data = data.get("current_slots", {})
    current_slots: Dict[str, Set[str]] = {}
    for date_str, times_list in current_slots_data.items():
        current_slots[date_str] = set(times_list)

    return (
        message_id_schedule,
        old_schedule_text,
        message_id_no_slots,
        old_no_slots_text,
        time_of_new_slots,
        last_time_slots_found,
        current_slots
    )


# ---------------------------------------------
# Основной цикл мониторинга
# ---------------------------------------------
def run_monitor():
    """
    - Загружаем состояние (если есть).
    - В бесконечном цикле fetch_slots(), анализируем, редактируем/отправляем сообщения.
    - Сохраняем состояние после изменений.
    """

    # 1) Загружаем из файла
    data = load_state()
    if data:
        (
            message_id_schedule,
            old_schedule_text,
            message_id_no_slots,
            old_no_slots_text,
            time_of_new_slots,
            last_time_slots_found,
            current_slots
        ) = dict_to_state(data)
    else:
        # Начальное состояние
        message_id_schedule = None
        old_schedule_text = None
        message_id_no_slots = None
        old_no_slots_text = None
        time_of_new_slots = None
        last_time_slots_found = None
        current_slots = {}

    while True:
        new_slots = fetch_slots()

        # --- (1) Нет слотов
        if not new_slots:
            # Удаляем сообщение с расписанием, если было
            if message_id_schedule is not None:
                tg_delete_message(BOT_TOKEN, CHAT_ID, message_id_schedule)
                message_id_schedule = None
                old_schedule_text = None

            # Формируем текст «нет слотов»
            if last_time_slots_found:
                last_str = format_datetime_russian(last_time_slots_found)
                new_no_slots_text = (
                    f"🔴 Свободных слотов [нет]({DOCTOR_URL}) 🔴\n\n"
                    "Как только появятся новые — сразу напишу 🙏🏻\n"
                    f"_(Последнее появление: {last_str})_"
                )
            else:
                new_no_slots_text = (
                    f"🔴 Свободных слотов [нет]({DOCTOR_URL}) 🔴\n\n"
                    "Как только появятся новые — сразу напишу 🙏🏻"
                )

            # Если у нас уже есть сообщение «нет слотов»
            if message_id_no_slots:
                # Сравним текст
                if old_no_slots_text != new_no_slots_text:
                    # Текст поменялся => редактируем
                    tg_edit_message(BOT_TOKEN, CHAT_ID, message_id_no_slots, new_no_slots_text)
                    old_no_slots_text = new_no_slots_text
                else:
                    # Текст не поменялся => ничего не делаем
                    pass
            else:
                # Сообщения «нет слотов» ещё нет => создаём
                msg_id = tg_send_message(BOT_TOKEN, CHAT_ID, new_no_slots_text)
                message_id_no_slots = msg_id
                old_no_slots_text = new_no_slots_text

            # current_slots = {} (пусто)
            current_slots = {}

            # Сохраняем
            state_dict = state_to_dict(
                message_id_schedule,
                old_schedule_text,
                message_id_no_slots,
                old_no_slots_text,
                time_of_new_slots,
                last_time_slots_found,
                current_slots
            )
            save_state(state_dict)

            random_sleep(CHECK_INTERVAL)
            continue

        # --- (2) Есть слоты
        # Если раньше было пусто, значит появились
        if not current_slots:
            last_time_slots_found = datetime.datetime.now()

        # Удаляем «нет слотов», если оно есть
        if message_id_no_slots:
            tg_delete_message(BOT_TOKEN, CHAT_ID, message_id_no_slots)
            message_id_no_slots = None
            old_no_slots_text = None

        # Смотрим, что появилось / пропало
        added = find_added_slots(current_slots, new_slots)
        removed = find_removed_slots(current_slots, new_slots)

        if added:
            # Удаляем старое сообщение (если было)
            if message_id_schedule:
                tg_delete_message(BOT_TOKEN, CHAT_ID, message_id_schedule)
                message_id_schedule = None
                old_schedule_text = None

            time_of_new_slots = datetime.datetime.now()

            # Формируем сообщение с баннером
            show_banner = True
            new_text = build_schedule_message(new_slots, show_new_alert=show_banner)

            msg_id = tg_send_message(BOT_TOKEN, CHAT_ID, new_text)
            message_id_schedule = msg_id
            old_schedule_text = new_text

            last_time_slots_found = datetime.datetime.now()

        else:
            # Новых слотов нет, но что-то могло измениться (пропасть),
            # или баннер мог «истечь»
            show_banner = still_show_banner(new_slots, time_of_new_slots)
            new_text = build_schedule_message(new_slots, show_new_alert=show_banner)

            if message_id_schedule:
                if old_schedule_text != new_text:
                    tg_edit_message(BOT_TOKEN, CHAT_ID, message_id_schedule, new_text)
                    old_schedule_text = new_text
            else:
                # Если нет сообщения с расписанием — создаём
                msg_id = tg_send_message(BOT_TOKEN, CHAT_ID, new_text)
                message_id_schedule = msg_id
                old_schedule_text = new_text

        current_slots = new_slots

        # Сохраняем
        state_dict = state_to_dict(
            message_id_schedule,
            old_schedule_text,
            message_id_no_slots,
            old_no_slots_text,
            time_of_new_slots,
            last_time_slots_found,
            current_slots
        )
        save_state(state_dict)

        random_sleep(CHECK_INTERVAL)


# ---------------------------------------------
# Запуск
# ---------------------------------------------
if __name__ == "__main__":
    os.makedirs(DATA_FOLDER, exist_ok=True)
    run_monitor()
