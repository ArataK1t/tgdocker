from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, CallbackContext
from subprocess import check_output, CalledProcessError
import docker
import psutil
from datetime import datetime, timezone, timedelta

# Инициализация Docker клиента
client = docker.from_env()

# Глобальные переменные для отслеживания состояния
active_jobs = {}
notification_history = []
notification_messages = []
notified_containers = set()
container_states = {}

# Функция для получения системных метрик
def get_system_metrics():
    cpu_usage = psutil.cpu_percent(interval=1)
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage('/')
    return (f"CPU Usage: {cpu_usage}%\n"
            f"Memory Usage: {memory.percent}% ({memory.used / 1024 / 1024 / 1024:.2f} GB / {memory.total / 1024 / 1024 / 1024:.2f} GB)\n"
            f"Swap Usage: {swap.percent}% ({swap.used / 1024 / 1024 / 1024:.2f} GB / {swap.total / 1024 / 1024 / 1024:.2f} GB)\n"
            f"Disk Usage: {disk.percent}% ({disk.used / 1024 / 1024 / 1024:.2f} GB / {disk.total / 1024 / 1024 / 1024:.2f} GB)")

# Функция для получения статуса контейнеров
def get_container_status():
    containers = client.containers.list(all=True)
    status = "\n".join([f"{c.name}: {c.status}" for c in containers])
    return status

# Функция для получения последних строк из screen-сессий
def get_screen_logs(session_name, lines=20):
    try:
        check_output(["screen", "-S", session_name, "-X", "hardcopy", "/tmp/screenlog.txt"])
        with open("/tmp/screenlog.txt", "r") as log_file:
            logs = log_file.readlines()[-lines:]
        return f"Logs for session {session_name}:\n" + "".join(logs)
    except Exception as e:
        return f"Error: {e}"

# Функция для добавления уведомления в историю
def add_notification_to_history(message):
    now = datetime.now(timezone.utc).astimezone(tz=timezone(timedelta(hours=3)))
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
    notification_history.append(f"[{timestamp}] {message}")
    if len(notification_history) > 50:
        notification_history.pop(0)

# Главное меню
def start(update: Update, context: CallbackContext):
    # Если пришел запрос через callback_query (нажатие на кнопку), используем его
    if update.callback_query:
        query = update.callback_query
        query.answer()  # Подтверждаем запрос callback

        keyboard = [
            [InlineKeyboardButton("\ud83d\udcca Метрики", callback_data='metrics')],
            [InlineKeyboardButton("\ud83d\udce6 Статус контейнеров", callback_data='container_status')],
            [InlineKeyboardButton("\ud83d\udd0d Логи контейнера", callback_data='container_logs')],
            [InlineKeyboardButton("\ud83d\udd0e История уведомлений", callback_data='notification_history')],
            [InlineKeyboardButton("\u2753 Помощь", callback_data='help')]
        ]
        
        # Редактируем сообщение с кнопками
        query.edit_message_text("Главное меню", reply_markup=InlineKeyboardMarkup(keyboard))

    # Если это не callback_query, а обычный старт (например, первый запуск бота)
    elif update.message:
        keyboard = [
            [InlineKeyboardButton("\ud83d\udcca Метрики", callback_data='metrics')],
            [InlineKeyboardButton("\ud83d\udce6 Статус контейнеров", callback_data='container_status')],
            [InlineKeyboardButton("\ud83d\udd0d Логи контейнера", callback_data='container_logs')],
            [InlineKeyboardButton("\ud83d\udd0e История уведомлений", callback_data='notification_history')],
            [InlineKeyboardButton("\u2753 Помощь", callback_data='help')]
        ]
        
        # Отправляем сообщение с кнопками, если это первый запуск
        update.message.reply_text("Главное меню", reply_markup=InlineKeyboardMarkup(keyboard))

# Универсальная кнопка "Назад" для возврата в меню
def back_button():
    return [
        [InlineKeyboardButton("\u2b05 Назад", callback_data='back_to_menu')]
    ]

# Режим обновления метрик
def show_metrics(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()

    # Остановка старой задачи
    stop_job_for_chat(query.message.chat_id)

    job_context = {
        'chat_id': query.message.chat_id,
        'message_id': query.message.message_id,
        'callback': get_system_metrics,
        'reply_markup': back_button()
    }
    # Запускаем задачу, которая будет обновлять сообщение с метриками
    job = context.job_queue.run_repeating(
        lambda context: query.edit_message_text(get_system_metrics(), reply_markup=InlineKeyboardMarkup(back_button())), 
        interval=5, first=0
    )
    active_jobs[query.message.chat_id] = job

    # Начальное обновление сообщения с метриками
    query.edit_message_text(get_system_metrics(), reply_markup=InlineKeyboardMarkup(back_button()))

# Режим обновления статуса контейнеров
def show_container_status(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()

    containers = client.containers.list(all=True)
    keyboard = []

    # Для каждого контейнера создаем кнопку
    for container in containers:
        # Определяем статус: Зеленый или Красный кружок
        if container.status == "running":
            status_icon = "🟢"
        else:
            status_icon = "🔴"

        keyboard.append([InlineKeyboardButton(f"{status_icon} {container.name}", callback_data=f'container_{container.name}')])

    # Добавляем кнопку "Назад"
    keyboard.append([InlineKeyboardButton("\u2b05 Назад", callback_data="back_to_menu")])

    # Обновляем сообщение с кнопками
    query.edit_message_text(
        "Выберите контейнер для управления:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

def show_container_control_buttons(query, container_name, context):
    try:
        container = client.containers.get(container_name)
        
        # Создаем кнопки для управления контейнером
        keyboard = [
            [InlineKeyboardButton("⏯ Запустить", callback_data=f"start_{container_name}")],
            [InlineKeyboardButton("⏹ Остановить", callback_data=f"stop_{container_name}")],
            [InlineKeyboardButton("🔄 Перезапустить", callback_data=f"restart_{container_name}")],
            [InlineKeyboardButton("ℹ️ Информация", callback_data=f"info_{container_name}")],
            [InlineKeyboardButton("\u2b05 Назад", callback_data="back_to_menu")]
        ]

        # Обновляем сообщение с кнопками для управления контейнером
        query.edit_message_text(
            f"Контейнер {container_name} ({container.status})",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    except docker.errors.NotFound:
        query.edit_message_text(
            f"Контейнер {container_name} не найден.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\u2b05 Назад", callback_data="back_to_menu")]])
        )

def show_confirmation_buttons(query, container_name, context, action):
    if action == "start":
        action_text = "запустить"
    elif action == "stop":
        action_text = "остановить"
    elif action == "restart":
        action_text = "перезапустить"

    keyboard = [
        [InlineKeyboardButton("Да", callback_data=f"confirm_{action}_{container_name}")],
        [InlineKeyboardButton("Нет", callback_data=f"container_{container_name}")],  # Кнопка для возврата
        [InlineKeyboardButton("\u2b05 Назад", callback_data=f"back_to_container_{container_name}")]
    ]

    query.edit_message_text(
        f"Вы уверены, что хотите {action_text} контейнер {container_name}?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
  
def start_container(container_name, query, context: CallbackContext):
    try:
        container = client.containers.get(container_name)
        container.start()
        message = f"Контейнер {container_name} успешно запущен."

        # Отправляем уведомление и обновляем интерфейс
        context.bot.send_message(chat_id=query.message.chat_id, text=message)
        show_container_control_buttons(query, container_name, context)

    except docker.errors.NotFound:
        message = f"Контейнер {container_name} не найден."
        context.bot.send_message(chat_id=query.message.chat_id, text=message)

def stop_container(container_name, query, context: CallbackContext):
    try:
        container = client.containers.get(container_name)
        container.stop()
        message = f"Контейнер {container_name} успешно остановлен."

        # Отправляем уведомление и обновляем интерфейс
        context.bot.send_message(chat_id=query.message.chat_id, text=message)
        show_container_control_buttons(query, container_name, context)

    except docker.errors.NotFound:
        message = f"Контейнер {container_name} не найден."
        context.bot.send_message(chat_id=query.message.chat_id, text=message)

def restart_container(container_name, query, context: CallbackContext):
    try:
        container = client.containers.get(container_name)
        container.restart()
        message = f"Контейнер {container_name} успешно перезапущен."

        # Отправляем уведомление и обновляем интерфейс
        context.bot.send_message(chat_id=query.message.chat_id, text=message)
        show_container_control_buttons(query, container_name, context)

    except docker.errors.NotFound:
        message = f"Контейнер {container_name} не найден."
        context.bot.send_message(chat_id=query.message.chat_id, text=message)

def show_container_info(container_name, query, context: CallbackContext):
    try:
        container = client.containers.get(container_name)
        
        # Собираем информацию о контейнере
        info = f"Информация о контейнере {container_name}:\n" \
               f"ID: {container.id}\n" \
               f"Статус: {container.status}\n" \
               f"Имя: {container.name}\n" \
               f"Время запуска: {container.attrs['State']['StartedAt']}\n" \
               f"Используемые порты: {container.attrs['NetworkSettings']['Ports']}\n"

        # Кнопка "Назад" с возвратом в управление контейнером
        keyboard = [
            [InlineKeyboardButton("\u2b05 Назад", callback_data=f"back_to_container_{container_name}")]
        ]

        # Обновляем сообщение с информацией о контейнере и кнопкой "Назад"
        query.edit_message_text(
            f"{info}",  # Вставляем информацию о контейнере в текст
            reply_markup=InlineKeyboardMarkup(keyboard)  # Добавляем только кнопку "Назад"
        )
    except docker.errors.NotFound:
        message = f"Контейнер {container_name} не найден."
        query.edit_message_text(
            f"{message}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\u2b05 Назад", callback_data="back_to_menu")]])
        )

# Выбор screen-сессии для логов
def select_screen_session(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()

    try:
        sessions = check_output(["screen", "-ls"]).decode('utf-8')
        session_names = [line.split()[0] for line in sessions.splitlines() if "Detached" in line or "Attached" in line]
        keyboard = [[InlineKeyboardButton(name, callback_data=f'screen_logs_{name}')]
                    for name in session_names]
    except CalledProcessError:
        keyboard = []

    # Добавляем кнопку "Назад"
    keyboard.append([InlineKeyboardButton("\u2b05 Назад", callback_data='back_to_menu')])

    # Обновляем сообщение с выбором screen-сессии
    query.edit_message_text("Выберите screen-сессию для просмотра логов:", reply_markup=InlineKeyboardMarkup(keyboard))

# Режим обновления логов screen-сессии
def show_screen_logs(update: Update, context: CallbackContext):
    query = update.callback_query
    session_name = query.data.split('_', 2)[2]
    query.answer()

    # Остановка старой задачи
    stop_job_for_chat(query.message.chat_id)

    def logs_callback():
        return get_screen_logs(session_name)

    job_context = {
        'chat_id': query.message.chat_id,
        'message_id': query.message.message_id,
        'callback': logs_callback,
        'reply_markup': back_button()
    }
   # Используем lambda для обновления сообщения с логами через определенный интервал
    job = context.job_queue.run_repeating(
        lambda context: query.edit_message_text(logs_callback(), reply_markup=InlineKeyboardMarkup(back_button())), 
        interval=10, first=0
    )
    active_jobs[query.message.chat_id] = job
    
    # Начальное обновление сообщения с логами
    query.edit_message_text(get_screen_logs(session_name), reply_markup=InlineKeyboardMarkup(back_button()))

# История уведомлений
def show_notification_history(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()

    history_text = "\n".join(notification_history) if notification_history else "История пуста."
    keyboard = [
        [InlineKeyboardButton("\ud83d\uddd1\ufe0f Очистить историю", callback_data='clear_notification_history')],
        [InlineKeyboardButton("\u2b05 Назад", callback_data='back_to_menu')]
    ]
    
    # Обновляем сообщение с историей уведомлений
    query.edit_message_text(f"История уведомлений:\n{history_text}", reply_markup=InlineKeyboardMarkup(keyboard))

# Остановка задач для чата
def stop_job_for_chat(chat_id):
    if chat_id in active_jobs:
        active_jobs[chat_id].schedule_removal()
        del active_jobs[chat_id]

# Обработчик кнопок
def button(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()

    if query.data == 'metrics':
        show_metrics(update, context)
    
    elif query.data == 'container_status':
        show_container_status(update, context)

    elif query.data == 'container_logs':
        select_screen_session(update, context)

    elif query.data.startswith('screen_logs_'):
        show_screen_logs(update, context)

    elif query.data == 'notification_history':
        show_notification_history(update, context)

    elif query.data == 'clear_notification_history':
        clear_notification_history(update, context)

    elif query.data == 'back_to_menu':
        back_to_menu(update, context)

    elif query.data == 'help':
        query.edit_message_text(
            "Помощь:\n1. Метрики - показывает загрузку CPU и памяти.\n2. Статус контейнеров - текущий статус ваших контейнеров.\n3. Логи контейнера - выберите screen-сессию для просмотра последних строк логов.\n4. История уведомлений - последние уведомления о состоянии контейнеров.",
            reply_markup=InlineKeyboardMarkup(back_button())
        )

    # Если выбрали контейнер для управления
    elif query.data.startswith('container_'):
        container_name = query.data.split('_')[1]
        # Показываем кнопки для управления контейнером
        show_container_control_buttons(query, container_name, context)

    # Если выбрали действие (запуск, остановка, перезапуск)
    elif query.data.startswith('start_'):
        container_name = query.data.split('_')[1]
        show_confirmation_buttons(query, container_name, context, action="start")
    
    elif query.data.startswith('stop_'):
        container_name = query.data.split('_')[1]
        show_confirmation_buttons(query, container_name, context, action="stop")

    elif query.data.startswith('restart_'):
        container_name = query.data.split('_')[1]
        show_confirmation_buttons(query, container_name, context, action="restart")

    elif query.data.startswith('info_'):
        container_name = query.data.split('_')[1]
        show_container_info(container_name, query, context)

    elif query.data.startswith('back_to_container_'):
        container_name = query.data.split('_')[2]
        show_container_control_buttons(query, container_name, context)

    # Подтверждение действия (Да / Нет)
    elif query.data.startswith('confirm_'):
        action, container_name = query.data.split('_')[1], query.data.split('_')[2]
        if action == "start":
            start_container(container_name, query, context)
        elif action == "stop":
            stop_container(container_name, query, context)
        elif action == "restart":
            restart_container(container_name, query, context)

# Функция для проверки статусов контейнеров и отправки уведомлений
def check_container_health_and_notify(context: CallbackContext):
    global container_states, notification_messages

    containers = client.containers.list(all=True)

    for container in containers:
        container_name = container.name
        current_status = container.status

        if container_name not in container_states or container_states[container_name] != current_status:
            container_states[container_name] = current_status

            if current_status in ['exited', 'stopped', 'unhealthy']:
                message = f"\u2757 Контейнер {container_name} в состоянии {current_status}."
                add_notification_to_history(message)

                sent_message = context.bot.send_message(
                    chat_id=context.job.context['chat_id'],
                    text=message,
                    disable_notification=False
                )
                notification_messages.append(sent_message.message_id)

        elif current_status == 'running' and container_name in notified_containers:
            notified_containers.remove(container_name)
          
# Функция для очистки истории уведомлений
def clear_notification_history(update: Update, context: CallbackContext):
    global notification_history, notification_messages
    query = update.callback_query
    query.answer()

    # Очищаем историю уведомлений
    notification_history = []

    # Удаляем все сообщения с уведомлениями
    for message_id in notification_messages:
        try:
            context.bot.delete_message(chat_id=query.message.chat_id, message_id=message_id)
        except Exception as e:
            query.edit_message_text(f"Ошибка при удалении сообщений: {e}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\u2b05 Назад", callback_data='back_to_menu')]]))

    # Очищаем список сообщений
    notification_messages.clear()

    # Возвращаем обновленный текст истории
    query.edit_message_text(
        "История уведомлений очищена.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\u2b05 Назад", callback_data='back_to_menu')]])
    )

# Остановка задач для чата
def stop_job_for_chat(chat_id):
    if chat_id in active_jobs:
        active_jobs[chat_id].schedule_removal()
        del active_jobs[chat_id]

# Возврат в главное меню
def back_to_menu(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()

    # Остановка старой задачи
    stop_job_for_chat(query.message.chat_id)

    # Отправляем главное меню
    start(update, context)

def main():
    global container_states

    # Получаем статус контейнеров при запуске
    containers = client.containers.list(all=True)
    container_states = {container.name: container.status for container in containers}

    # Создаем объект Updater и Dispatcher
    updater = Updater("your_telegram_bot_token", use_context=True)
    dispatcher = updater.dispatcher

    # Добавляем обработчики команд и кнопок
    dispatcher.add_handler(CommandHandler('start', start))
    dispatcher.add_handler(CallbackQueryHandler(button))

    # Запускаем задачу для проверки состояния контейнеров каждые 60 секунд
    job_queue = updater.job_queue
    job_queue.run_repeating(check_container_health_and_notify, interval=60, first=0, context={'chat_id': 'your_chat_id'})

    # Запуск бота
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
