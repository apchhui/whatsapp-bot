from dotenv import load_dotenv
import os
import aiosqlite
import asyncio
import json
import httpx
from fastapi import FastAPI, Request
import re
from contextlib import asynccontextmanager
import pandas as pd
from urllib.parse import urlencode
from datetime import datetime, timedelta
import telebot
import sqlite3
import threading
import requests
from requests.exceptions import ReadTimeout, ConnectionError
import time
import logging

ALLOWED_USERS = [] # тут chatId юзеров которым будет рассылка в телеграме и доступна адм панель

app_state = {}

bot = telebot.TeleBot("tk")

@bot.message_handler(commands=['start'])
def start(message):
    chat_id = message.chat.id
    if chat_id not in ALLOWED_USERS:
        bot.send_message(chat_id, "Вам нельзя пользоваться этим ботом!")
    else:
        bot.send_message(chat_id, "Добро пожаловать! Рассылка /send")

@bot.message_handler(commands=['send'])
def send(message):
    chat_id = message.chat.id
    if chat_id not in ALLOWED_USERS:
        bot.send_message(chat_id, "Вам нельзя пользоваться этим ботом!")
        return

    bot.send_message(chat_id, "Введи сообщение:")
    bot.register_next_step_handler(message, receive_message)

def receive_message(message):
    chat_id = message.chat.id
    app_state[chat_id] = {"message": message.text}
    bot.send_message(chat_id, "Введи города через запятую:")
    bot.register_next_step_handler(message, receive_cities)

def receive_cities(message):
    chat_id = message.chat.id
    cities = [c.strip().capitalize() for c in message.text.split(",") if c.strip()]
    app_state[chat_id]["cities"] = cities
    bot.send_message(chat_id, f"Рассылка будет по городам: {', '.join(cities)}")
    print("Текст:", app_state[chat_id]["message"])
    with sqlite3.connect("users.db") as conn:
        cursor = conn.cursor()
        for city in cities:
            cursor.execute("SELECT number FROM users WHERE city = ?", (city,))
            rows = cursor.fetchall()
            for row in rows:
                number = row[0]
                print(f"Отправляем {number[:-4]}")
                send_message_sync(app_state[chat_id]['message'], number)

@asynccontextmanager
async def lifespan(app: FastAPI):
    db = await prepare_database_tables()
    cities = await load_cities()

    app.state.cities = cities
    def run_bot():
        bot.infinity_polling(timeout=10, long_polling_timeout=5)


    threading.Thread(target=run_bot, daemon=True).start()
    yield

app = FastAPI(lifespan=lifespan)

app.state.task_cache = {}

@app.post("/webhook")
async def receive_webhook(req: Request):
    data = await req.json()
    if data.get("typeWebhook") != "incomingMessageReceived":
        return {"status": "ignored"}

    sender_data = data.get("senderData", {})
    message_data = data.get("messageData", {})
    chat_id = sender_data.get("chatId")
    print(chat_id)
    text = message_data.get("textMessageData", {}).get("textMessage", "").strip().lower()

    async with aiosqlite.connect("users.db") as db:
        await db.execute("INSERT OR IGNORE INTO users (number) VALUES (?)", (chat_id,))
        async with db.execute("SELECT state, city, name FROM users WHERE number = ?", (chat_id,)) as cursor:
            row = await cursor.fetchone()
            state = row[0] if row else "start"
            user_city = row[1] if row else None
            user_name = row[2] if row else None

        if state == "registered" and (not user_name or not user_city):
            if not user_name:
                await send_message("Пожалуйста, укажите ваше имя для завершения регистрации:", chat_id)
                await db.execute("UPDATE users SET state = 'awaiting_name' WHERE number = ?", (chat_id,))
            elif not user_city:
                await send_message("Пожалуйста, укажите ваш город для завершения регистрации:", chat_id)
                await db.execute("UPDATE users SET state = 'awaiting_city' WHERE number = ?", (chat_id,))
        elif state == "start":
            if text == "регистрация":
                await send_message("Как вас зовут?", chat_id)
                await db.execute("UPDATE users SET state = 'awaiting_name' WHERE number = ?", (chat_id,))
            else:
                await send_message("Добро пожаловать! Есть ли у вас опыт работы Тайным Покупателем? (Да / Нет)", chat_id)
                await db.execute("UPDATE users SET state = 'asked_experience' WHERE number = ?", (chat_id,))

        elif state == "awaiting_name":
            name = text.strip().capitalize()
            if not name:
                await send_message("Пожалуйста, введите корректное имя.", chat_id)
            else:
                await db.execute("UPDATE users SET name = ?, state = 'awaiting_city' WHERE number = ?", (name, chat_id))
                await send_message("Спасибо! Укажите, пожалуйста, ваш город:", chat_id)
        #редачите любой текст в send_message()
        elif state == "awaiting_city":
            cities = req.app.state.cities
            city_name = text.strip().capitalize()
            if city_name in cities:
                if not user_name:
                    await send_message("Сначала введите имя перед выбором города. Напишите своё имя:", chat_id)
                    await db.execute("UPDATE users SET state = 'awaiting_name' WHERE number = ?", (chat_id,))
                else:
                    await db.execute("UPDATE users SET city = ?, state = 'registered' WHERE number = ?", (city_name, chat_id))
                    await send_message(
                        f"Ваш город '{city_name}' сохранён. Теперь вы зарегистрированы, {user_name}!\nВы можете:\n- Получить задания\n- Открыть FAQ\n- Пройти обучение\n\nНапишите одну из команд: Задания / FAQ / Обучение",
                        chat_id
                    )
            else:
                await send_message("Город не найден. Убедитесь, что вы ввели его правильно. Попробуйте снова:", chat_id)

        elif state == "asked_experience":
            if text == "да":
                await send_message("Пожалуйста, подтвердите согласие на обработку персональных данных. (Да / Нет)", chat_id)
                await db.execute("UPDATE users SET state = 'awaiting_consent' WHERE number = ?", (chat_id,))
            elif text == "нет":
                await db.execute("UPDATE users SET state = 'training_in_progress' WHERE number = ?", (chat_id,))
                await send_message(
                    "Обучение:\n"
                    "1. Что такое работа тайным покупателем\n"
                    "Тайный покупатель приходит в магазин, кафе, офис продаж под видом обычного клиента, получает консультацию по товару, делает покупку или заказывает услугу. Как правило, визит занимает 15-20 минут.\n"
                    "Задача тайного покупателя — проверить качество обслуживания, знание продавцами товара или услуги, внешний вид и другие параметры.\n"
                    "Для каждой проверки у вас будет сценарий проведения, где описано как проводить данную проверку и на какие параметры следует обращать внимание. В большинстве случаев надо сделать аудиозапись визита и фотографии товара, вывесок, входных групп. Аудиозапись и фото делается на телефон.\n"
                    "После проведения проверки необходимо заполнить отчет в личном кабинете https://qwertykmv.tainpo.ru/. Вас будет курировать координатор.\n\n"

                    "2. Сколько можно зарабатывать в месяц\n"
                    "Чем больше проверок в месяц вы сделаете, тем больше заработаете. Опытные тайные покупатели зарабатывают до 30 000 рублей в месяц.\n\n"

                    "3. Как часто нужно делать проверки?\n"
                    "В зависимости от ваших возможностей и количества заданий в вашем городе. Одну и ту же точку можно посещать не чаще одного раза в 6 месяцев.\n\n"

                    "4. Как происходят выплаты\n"
                    "• Оплата на карту через 2 месяца после проверки (за февраль — в апреле), с 25 по 31 число. Реквизиты запрашиваются на почту.\n"
                    "• Самозанятым: проверки с 1 по 15 число — оплата 30/31 числа, с 16 по 30/31 — 15 числа следующего месяца. Через Рокет Ворк. Реквизиты запрашиваются на почту.\n\n"

                    "5. Как начать работу Тайным покупателем\n"
                    "Зарегистрируйтесь в системе https://qwertykmv.tainpo.ru/. Выберите проверку, ознакомьтесь с заданием и сценарием, совершите визит.\n\n"

                    "6. Зачем регистрироваться в системе\n"
                    "Чтобы получать предложения и иметь возможность самостоятельно брать задания. Это позволяет работать на постоянной основе и формировать свой заработок.\n\n"

                    "7. Как проводить проверки\n"
                    "• Сценарий: будет инструкция с ролью, легендой, вопросами и деталями проверки.\n"
                    "• Время: посещать точки с 1 часа после открытия и до 1 часа до закрытия.\n"
                    "• Аудиозапись: держите телефон в кармане микрофоном вверх. Включите режим полета. Начинайте и заканчивайте запись с датой, временем и адресом.\n"
                    "• Фото: как правило, нужны фото товара, чеков, фасада и т.д. Фасад — только после выхода.\n"
                    "• Отчет: после визита заполняйте отчет на сайте и прикрепляйте материалы.\n"
                    "• Оплата: как указано выше, на карту или через Рокет Ворк, в зависимости от статуса.\n\n"

                    "Вы всё поняли? (Да / Нет)", chat_id
                )
            else:
                await send_message("Ответьте, пожалуйста: есть ли у вас опыт работы Тайным Покупателем? (Да / Нет)", chat_id)

        elif state == "training_in_progress":
            if text == "да":
                await send_message("Пожалуйста, подтвердите согласие на обработку персональных данных. (Да / Нет)", chat_id)
                await db.execute("UPDATE users SET state = 'awaiting_consent' WHERE number = ?", (chat_id,))
            elif text == "нет":
                await send_message(
                    "Обучение:\n"
                    "1. Что такое работа тайным покупателем\n"
                    "Тайный покупатель приходит в магазин, кафе, офис продаж под видом обычного клиента, получает консультацию по товару, делает покупку или заказывает услугу. Как правило, визит занимает 15-20 минут.\n"
                    "Задача тайного покупателя — проверить качество обслуживания, знание продавцами товара или услуги, внешний вид и другие параметры.\n"
                    "Для каждой проверки у вас будет сценарий проведения, где описано как проводить данную проверку и на какие параметры следует обращать внимание. В большинстве случаев надо сделать аудиозапись визита и фотографии товара, вывесок, входных групп. Аудиозапись и фото делается на телефон.\n"
                    "После проведения проверки необходимо заполнить отчет в личном кабинете https://qwertykmv.tainpo.ru/. Вас будет курировать координатор.\n\n"

                    "2. Сколько можно зарабатывать в месяц\n"
                    "Чем больше проверок в месяц вы сделаете, тем больше заработаете. Опытные тайные покупатели зарабатывают до 30 000 рублей в месяц.\n\n"

                    "3. Как часто нужно делать проверки?\n"
                    "В зависимости от ваших возможностей и количества заданий в вашем городе. Одну и ту же точку можно посещать не чаще одного раза в 6 месяцев.\n\n"

                    "4. Как происходят выплаты\n"
                    "• Оплата на карту через 2 месяца после проверки (за февраль — в апреле), с 25 по 31 число. Реквизиты запрашиваются на почту.\n"
                    "• Самозанятым: проверки с 1 по 15 число — оплата 30/31 числа, с 16 по 30/31 — 15 числа следующего месяца. Через Рокет Ворк. Реквизиты запрашиваются на почту.\n\n"

                    "5. Как начать работу Тайным покупателем\n"
                    "Зарегистрируйтесь в системе https://qwertykmv.tainpo.ru/. Выберите проверку, ознакомьтесь с заданием и сценарием, совершите визит.\n\n"

                    "6. Зачем регистрироваться в системе\n"
                    "Чтобы получать предложения и иметь возможность самостоятельно брать задания. Это позволяет работать на постоянной основе и формировать свой заработок.\n\n"

                    "7. Как проводить проверки\n"
                    "• Сценарий: будет инструкция с ролью, легендой, вопросами и деталями проверки.\n"
                    "• Время: посещать точки с 1 часа после открытия и до 1 часа до закрытия.\n"
                    "• Аудиозапись: держите телефон в кармане микрофоном вверх. Включите режим полета. Начинайте и заканчивайте запись с датой, временем и адресом.\n"
                    "• Фото: как правило, нужны фото товара, чеков, фасада и т.д. Фасад — только после выхода.\n"
                    "• Отчет: после визита заполняйте отчет на сайте и прикрепляйте материалы.\n"
                    "• Оплата: как указано выше, на карту или через Рокет Ворк, в зависимости от статуса.\n\n"

                    "Вы всё поняли? (Да / Нет)", chat_id
                )

            else:
                await send_message("Пожалуйста, ответьте: вы всё поняли после обучения? (Да / Нет)", chat_id)

        elif state == "awaiting_consent":
            if text == "да":
                if not user_name:
                    await send_message("Пожалуйста, укажите имя для завершения регистрации.", chat_id)
                    await db.execute("UPDATE users SET state = 'awaiting_name' WHERE number = ?", (chat_id,))
                elif not user_city:
                    await send_message("Пожалуйста, укажите город для завершения регистрации.", chat_id)
                    await db.execute("UPDATE users SET state = 'awaiting_city' WHERE number = ?", (chat_id,))
                else:
                    await send_message("Спасибо! Теперь вы можете:\n- Получить задания\n- Открыть FAQ\nНапишите 'сменить город' для дальнейшего взаимодействия", chat_id)
                    await db.execute("UPDATE users SET state = 'registered' WHERE number = ?", (chat_id,))
            elif text == "нет":
                await send_message("Вы не можете продолжить без согласия на обработку данных.", chat_id)
            else:
                await send_message("Пожалуйста, подтвердите согласие на обработку данных. (Да / Нет)", chat_id)

        elif state == "registered":
            if text == "регистрация":
                await send_message("Вы уже зарегистрированы. Чтобы изменить город, напишите 'сменить город'", chat_id)
            elif text == "сменить город":
                await send_message("Укажите новый город:", chat_id)
                await db.execute("UPDATE users SET state = 'awaiting_city' WHERE number = ?", (chat_id,))
            elif text == "задания":
                cities = req.app.state.cities
                if user_city and user_city in cities:
                    city_id = cities[user_city]
                    params = {
                        'key': 'myKey',
                        'username': 'apibot',
                        'password': 'psw',
                        'action': 'select',
                        'entity_id': 22,
                        'limit': 0,
                        'select_fields': '167,170,272,514',
                        'filters[167]': city_id
                    }
                    encoded = urlencode(params)
                    async with httpx.AsyncClient() as client:
                        response = await client.post("http://qwertykmv.ru/zrm/api/rest.php", data=encoded, headers={"Content-Type": "application/x-www-form-urlencoded"})
                        try:
                            response_data = response.json()
                            if "data" in response_data:
                                results = response_data["data"]
                                if results:
                                    app.state.task_cache[chat_id] = {
                                        "expires": datetime.now() + timedelta(minutes=5),
                                        "tasks": results
                                    }
                                    message = "Ваши задания:\n\n"
                                    for i, task in enumerate(results, 1):
                                        message += f"{i}. Город: {task['167']}\nАдрес: {task['170']}\nПроект: {task['514']}\nОплата: {task['272']}\n"
                                    message += "\nНапишите номер задания, чтобы выбрать его.\nНапишите только ОДИН номер"
                                    await db.execute("UPDATE users SET state = 'awaiting_task_number' WHERE number = ?", (chat_id,))
                                else:
                                    message = "Нет доступных заданий для вашего города."
                            else:
                                message = f"Ошибка получения заданий: {response_data}"
                        except Exception as e:
                            message = f"Ошибка при обработке ответа сервера: {str(e)}"

                    await send_message(message, chat_id)
                else:
                    await send_message("Ваш город не указан или не найден. Напишите 'сменить город' для обновления.", chat_id)

            elif text == "faq":
                await send_message("FAQ:\n"
                    "• Оплата на карту через 2 месяца после проверки (за февраль — в апреле), с 25 по 31 число. Реквизиты запрашиваются на почту.\n"
                    "• Самозанятым: проверки с 1 по 15 число — оплата 30/31 числа, с 16 по 30/31 — 15 числа следующего месяца. Через Рокет Ворк. Реквизиты запрашиваются на почту.\n\n"
                    "Зарегистрируйтесь в системе https://qwertykmv.tainpo.ru/. Выберите проверку, ознакомьтесь с заданием и сценарием, совершите визит.\n\n", 
                    chat_id)
            elif text == "обучение":
                await send_message(
                    "Обучение:\n"
                    "1. Что такое работа тайным покупателем\n"
                    "Тайный покупатель приходит в магазин, кафе, офис продаж под видом обычного клиента, получает консультацию по товару, делает покупку или заказывает услугу. Как правило, визит занимает 15-20 минут.\n"
                    "Задача тайного покупателя — проверить качество обслуживания, знание продавцами товара или услуги, внешний вид и другие параметры.\n"
                    "Для каждой проверки у вас будет сценарий проведения, где описано как проводить данную проверку и на какие параметры следует обращать внимание. В большинстве случаев надо сделать аудиозапись визита и фотографии товара, вывесок, входных групп. Аудиозапись и фото делается на телефон.\n"
                    "После проведения проверки необходимо заполнить отчет в личном кабинете https://qwertykmv.tainpo.ru/. Вас будет курировать координатор.\n\n"

                    "2. Сколько можно зарабатывать в месяц\n"
                    "Чем больше проверок в месяц вы сделаете, тем больше заработаете. Опытные тайные покупатели зарабатывают до 30 000 рублей в месяц.\n\n"

                    "3. Как часто нужно делать проверки?\n"
                    "В зависимости от ваших возможностей и количества заданий в вашем городе. Одну и ту же точку можно посещать не чаще одного раза в 6 месяцев.\n\n"

                    "4. Как происходят выплаты\n"
                    "• Оплата на карту через 2 месяца после проверки (за февраль — в апреле), с 25 по 31 число. Реквизиты запрашиваются на почту.\n"
                    "• Самозанятым: проверки с 1 по 15 число — оплата 30/31 числа, с 16 по 30/31 — 15 числа следующего месяца. Через Рокет Ворк. Реквизиты запрашиваются на почту.\n\n"

                    "5. Как начать работу Тайным покупателем\n"
                    "Зарегистрируйтесь в системе https://qwertykmv.tainpo.ru/. Выберите проверку, ознакомьтесь с заданием и сценарием, совершите визит.\n\n"

                    "6. Зачем регистрироваться в системе\n"
                    "Чтобы получать предложения и иметь возможность самостоятельно брать задания. Это позволяет работать на постоянной основе и формировать свой заработок.\n\n"

                    "7. Как проводить проверки\n"
                    "• Сценарий: будет инструкция с ролью, легендой, вопросами и деталями проверки.\n"
                    "• Время: посещать точки с 1 часа после открытия и до 1 часа до закрытия.\n"
                    "• Аудиозапись: держите телефон в кармане микрофоном вверх. Включите режим полета. Начинайте и заканчивайте запись с датой, временем и адресом.\n"
                    "• Фото: как правило, нужны фото товара, чеков, фасада и т.д. Фасад — только после выхода.\n"
                    "• Отчет: после визита заполняйте отчет на сайте и прикрепляйте материалы.\n"
                    "• Оплата: как указано выше, на карту или через Рокет Ворк, в зависимости от статуса.\n\n", chat_id
                )

            else:
                await send_message("Напишите одну из команд: Задания / FAQ / Обучение / Сменить город", chat_id)

        elif state == "awaiting_task_number":
            task_data = app.state.task_cache.get(chat_id)
            if not task_data or task_data["expires"] < datetime.now():
                await send_message("Время выбора задания истекло. Напишите 'задания' чтобы получить их снова.", chat_id)
                await db.execute("UPDATE users SET state = 'registered' WHERE number = ?", (chat_id,))
            else:
                try:
                    selected_index = int(text) - 1
                    task = task_data["tasks"][selected_index]
                    task_id = task["id"]
                except (ValueError, IndexError):
                    await send_message("Некорректный номер задания. Попробуйте снова.", chat_id)
                else:
                    task_data.setdefault("attempts", 0)
                    task_data["attempts"] += 1

                    params = {
                        'key': 'myKey',
                        'username': 'apibot',
                        'password': 'psw',
                        'action': 'select',
                        'entity_id': 22,
                        'limit': 0,
                        'select_fields': '459',
                        'filters[id]': task_id
                    }
                    encoded = urlencode(params)
                    async with httpx.AsyncClient() as client:
                        response = await client.post("http://qwertykmv.ru/zrm/api/rest.php", data=encoded, headers={"Content-Type": "application/x-www-form-urlencoded"})
                        try:
                            response_data = response.json()
                            for row in response_data.get("data", []):
                                raw = row.get("459", "")
                                coordinator_number = ''.join(filter(lambda c: c.isdigit() or c == '+', raw))
                                if coordinator_number:
                                    break
                            if coordinator_number:
                                msg = (
                                    f"ТП оставил отклик на локацию. Свяжитесь с ним для назначения проверки:\n"
                                    f"Имя: {user_name or 'Неизвестно'}\n"
                                    f"Город: {user_city}\n"
                                    f"Номер: {chat_id[:-4]}\n\n"
                                    f"Проект: {task['514']}\nАдрес: {task['170']}\nОплата: {task['272']}"
                                )
                                await send_message(msg, f"{coordinator_number[1:]}@c.us")
                                await send_message("Вы выбрали задание. Координатор получил информацию и скоро свяжется с вами.", chat_id)
                                await db.execute("UPDATE users SET state = 'registered' WHERE number = ?", (chat_id,))
                                app.state.task_cache.pop(chat_id, None)
                            else:
                                if task_data["attempts"] >= 2:
                                    await send_message("Не удалось найти координатора после нескольких попыток. Попробуйте позже или свяжитесь с поддержкой.", chat_id)
                                    await db.execute("UPDATE users SET state = 'registered' WHERE number = ?", (chat_id,))
                                    app.state.task_cache.pop(chat_id, None)
                                else:
                                    await send_message("Координатор не найден. Повторите номер задания.", chat_id)
                        except Exception as e:
                            await send_message(f"Ошибка получения координатора: {str(e)}", chat_id)

        else:
            if text == "обучение":
                await send_message("Обучение: [здесь будет ваш материал]", chat_id)
            elif text in {"да", "нет"}:
                pass
            else:
                await send_message("Для начала регистрации напишите 'Регистрация'.", chat_id)

        await db.commit()

    return {"status": "ok"}



load_dotenv()

API_URL = "https://1103.api.green-api.com"
MEDIA_URL = "https://1103.media.green-api.com"

def prepare_env_variables():
    GREEN_API_TOKEN = os.getenv("GREEN_API_TOKEN")
    INSTANCE = os.getenv("INSTANCE")
    return GREEN_API_TOKEN, INSTANCE

GREEN_API_TOKEN, INSTANCE = prepare_env_variables()

async def load_cities():
    df = pd.read_excel("Города api.xlsx")
    city_ids = dict(zip(df["Наименование города"], df["ID"]))
    return city_ids

async def prepare_database_tables():
    db = await aiosqlite.connect("users.db")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            city TEXT,
            number TEXT UNIQUE,
            state TEXT DEFAULT 'start',
            name TEXT
        )
    """)
    await db.commit()
    return db

async def send_message(message: str, chatId: str):
    url = f"https://1103.api.green-api.com/waInstance{INSTANCE}/sendMessage/{GREEN_API_TOKEN}"
    payload = {
        "chatId": chatId,
        "message": message,
    }
    headers = {
        'Content-Type': 'application/json'
    }
    async with httpx.AsyncClient() as client:
        await client.post(url, json=payload, headers=headers)

def send_message_sync(message: str, chatId: str):
    url = f"https://1103.api.green-api.com/waInstance{INSTANCE}/sendMessage/{GREEN_API_TOKEN}"
    payload = {
        "chatId": chatId,
        "message": message,
    }
    headers = {
        'Content-Type': 'application/json'
    }
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"Ошибка при отправке сообщения на {chatId}: {e}")
