import telebot
from telebot import types, apihelper
import google.generativeai as genai
import os
import requests
import base64
import threading
import random

apihelper.READ_TIMEOUT = 90
apihelper.CONNECT_TIMEOUT = 90

# ==========================================
# 1. КОНФИГУРАЦИЯ
# ==========================================
BOT_TOKEN = os.getenv('BOT_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
FAL_KEY = os.getenv('FAL_API_KEY')  
CHANNEL_ID = '@Master_Lavk'

bot = telebot.TeleBot(BOT_TOKEN)
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

user_states = {}
user_data = {}
verified_users = set()

(
    STATE_NONE, STATE_PHOTO, STATE_FACTION, STATE_NAME, 
    STATE_CATEGORY, STATE_POWER, STATE_ARMOR, 
    STATE_PROVISION, STATE_FRAME, STATE_ABILITY, STATE_QUOTE
) = range(11)

FACTIONS = {
    "Nilfgaard": (
        "🛡 Нильфгаард", "Нильфгаард", 
        "A highly detailed dark fantasy portrait of a character, wearing heavy, ornate black and gold Nilfgaardian plate armor with a prominent sun emblem. The character exudes a stern, disciplined, and aristocratic aura. Detailed facial features, cinematic lighting, sharp focus on the armor's metallic textures. Background: A sprawling, muddy Nilfgaardian military camp at dusk, featuring imposing black and gold pavilions, flickering campfires, and silhouettes of soldiers with tall pikes in the distance. Color palette: black, gold, dark grey, crimson accents. Masterpiece, digital oil painting, official Gwent card art style, 8k resolution, trending on ArtStation."
    ),
    "Monsters": (
        "👾 Чудовища", "Чудовища", 
        "A highly detailed dark fantasy portrait of a terrifying character, mutated with monstrous features, pale veiny skin, wearing grim bone and chitin armor adorned with skulls. The character looks feral and predatory. Eerie, unnatural lighting casting long shadows. Background: A dense, terrifying, foggy swamp at midnight. Twisted, dead ancient trees reaching like claws, glowing yellow eyes hidden in the thick green mist, bubbling toxic water. Color palette: sickly greens, bone white, blood red, abyssal black. Masterpiece, digital oil painting, official Gwent card art style, macabre, 8k resolution."
    ),
    "Skellige": (
        "⚓ Скеллиге", "Скеллиге", 
        "A highly detailed dark fantasy portrait of a rugged viking warrior character, wearing heavy bear-fur mantles, leather padding, and Skellige clan armor with carved celtic knots and runic symbols. Weathered, sea-salt stained textures. Harsh, dramatic, cold lighting. Background: A stormy nordic oceanic coast. Massive crashing waves against jagged steep cliffs, dark storm clouds, heavy rain, and the prow of a wooden viking longship with a dragon head in the turbulent waters. Color palette: cold icy blues, earthy browns, steel grey, sea foam white. Masterpiece, digital oil painting, official Gwent card art style, 8k resolution."
    ),
    "Northern": (
        "⚜️ Королевства Севера", "Королевства Севера", 
        "A highly detailed dark fantasy portrait of a noble medieval character, wearing gleaming, polished steel plate armor draped with blue and silver heraldic tabards. Heroic and stoic pose. Bright, warm, cinematic sunlight highlighting the metallic reflections. Background: A bustling, war-torn courtyard of a besieged massive stone medieval castle. Thick stone walls, wooden trebuchets, flying arrows in the sky, and fluttering blue and silver royal banners. Color palette: royal blue, bright steel, red accents, warm golden sunlight. Masterpiece, digital oil painting, official Gwent card art style, 8k resolution."
    ),
    "Syndicate": (
        "💰 Синдикат", "Синддикат", 
        "A highly detailed dark fantasy portrait of a cunning and wealthy character, wearing lavish but slightly dirty and frayed medieval merchant clothes, heavy velvet coats, and multiple gold rings on fingers. Shady, conspiratorial look. Warm, glowing lantern light mixed with deep, harsh shadows. Background: A claustrophobic, dirty Novigrad city alleyway at night. Wet cobblestones reflecting the orange glow of wall lanterns, rain puddles, scattered golden coins, and a shadowy figure in the background. Color palette: rich purple, gold, dark murky greens, deep shadows. Masterpiece, digital oil painting, official Gwent card art style, 8k resolution."
    ),
    "Scoiatael": (
        "🐿 Скоя'таэли", "Скоя'таэли", 
        "A highly detailed dark fantasy portrait of an elven guerilla character, wearing intricate forest camouflage, green and brown leather armor decorated with leaves and animal fangs. Agile, alert posture. Soft, dappled forest lighting filtering through leaves. Background: A deep, ancient, magical elven forest. Colossal ancient oak trees covered in moss, crumbling white marble elven ruins, blooming glowing flora, and shafts of golden ethereal sunlight piercing the dense canopy. Color palette: lush forest greens, autumn browns, golden light, earthy tones. Masterpiece, digital oil painting, official Gwent card art style, 8k resolution."
    ),
    "Neutral": (
        "🐺 Нейтральная", "Нейтральная", 
        "A highly detailed dark fantasy portrait of a hardened, weary character like a witcher or mercenary, wearing well-worn, dark leather studded armor, a silver medallion, and the hilts of twin swords strapped to the back. Gritty, realistic textures. Warm, flickering firelight illuminating the face. Background: A cozy but dimly lit medieval tavern interior. Heavy wooden oak tables, flickering wax candles on skulls, massive ale barrels, and blurry silhouettes of tavern patrons drinking in the background. Color palette: dark leather browns, silver, muted greys, warm orange firelight. Masterpiece, digital oil painting, official Gwent card art style, 8k resolution."
    )
}

def check_sub(user_id):
    try:
        status = bot.get_chat_member(CHANNEL_ID, user_id).status
        return status in ['member', 'administrator', 'creator']
    except Exception:
        return False

def get_main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(types.KeyboardButton("🃏 Создать карту"))
    markup.add(types.KeyboardButton("📜 Мои картищи"), types.KeyboardButton("👤 Мой профиль"))
    markup.add(types.KeyboardButton("🔗 Позвать друзей"), types.KeyboardButton("🆘 Поддержка"))
    return markup

def get_cancel_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("❌ Отмена"))
    return markup

@bot.message_handler(commands=['start'])
def send_welcome(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if chat_id not in verified_users:
        if not check_sub(user_id):
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("📢 Пойти в пещеру (Подписаться)", url=f"https://t.me/{CHANNEL_ID[1:]}"))
            markup.add(types.InlineKeyboardButton("🔄 Я подписался!", callback_data="check_sub_btn"))
            bot.send_message(chat_id, "🛑 **СТОЯТЬ, ЧЕЛОВЕЧЕ!**\nЛавк не пускает чужаков. Сначала зайди в мою пещеру (подпишись на канал), а потом будем делать картищи!", parse_mode="Markdown", reply_markup=markup)
            return
        else:
            verified_users.add(chat_id)
    show_welcome(chat_id)

@bot.callback_query_handler(func=lambda call: call.data == "check_sub_btn")
def verify_sub_callback(call):
    if check_sub(call.from_user.id):
        verified_users.add(call.message.chat.id)
        bot.answer_callback_query(call.id, "Лавк рад! Заходи!")
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        show_welcome(call.message.chat.id)
    else:
        bot.answer_callback_query(call.id, "Ууу, Лавк не видит подписки! Ты обманываешь!", show_alert=True)

def show_welcome(chat_id):
    user_states[chat_id] = STATE_NONE
    welcome_text = "👋 **Ууу, здорово, человече!** Я — Лавк, твой верный проводник в мире легендарного Гвинта.\n\nЗдесь пахнет краской, старым пергаментом и магией! 🎨✨\n\nГотов увековечить себя на великой картище? Выбирай дело по душе, и погнали творить искусство!"
    try:
        with open('1794190872103.jpg', 'rb') as photo:
            bot.send_photo(chat_id, photo, caption=welcome_text, reply_markup=get_main_menu(), parse_mode="Markdown")
    except FileNotFoundError:
        bot.send_message(chat_id, welcome_text, reply_markup=get_main_menu(), parse_mode="Markdown")

@bot.message_handler(func=lambda msg: msg.text == "❌ Отмена")
def cancel_creation(message):
    user_states[message.chat.id] = STATE_NONE
    bot.send_message(message.chat.id, "Эх, Лавк убирает холст. Если захочешь новую картищу — жми кнопку!", reply_markup=get_main_menu())

@bot.message_handler(func=lambda msg: msg.text in ["📜 Мои картищи", "👤 Мой профиль", "🔗 Позвать друзей", "🆘 Поддержка"])
def menu_stubs(message):
    bot.send_message(message.chat.id, "🛠 *Эта кнопка пока в разработке. Скоро Лавк её доделает!*", parse_mode="Markdown")

@bot.message_handler(func=lambda msg: msg.text == "🃏 Создать карту")
def start_creation(message):
    chat_id = message.chat.id
    user_data[chat_id] = {}
    user_states[chat_id] = STATE_PHOTO
    text = "📸 **Сначала давай лицо!**\n\nЧеловече давать хорошую картинку. Без теней, без темных стекол на глазах и чтобы ничего не закрывало! Духам нужно видеть твои черты, чтобы красиво перенести на холст. Плохое лицо — плохая картища!"
    bot.send_message(chat_id, text, reply_markup=get_cancel_menu(), parse_mode="Markdown")

@bot.message_handler(content_types=['photo', 'document'], func=lambda msg: user_states.get(msg.chat.id) == STATE_PHOTO)
def process_photo(message):
    chat_id = message.chat.id
    if message.photo:
        user_data[chat_id]['photo_id'] = message.photo[-1].file_id
    else:
        bot.send_message(chat_id, "⚠️ Лавк просил картинку! Пришли фото, человече.")
        return
    user_states[chat_id] = STATE_FACTION
    markup = types.InlineKeyboardMarkup(row_width=2)
    for key, val in FACTIONS.items():
        markup.add(types.InlineKeyboardButton(val[0], callback_data=f"faction_{key}"))
    bot.send_message(chat_id, "🏴 **За кого воевать будешь?**\n\nВыбирай знамя, человече!", reply_markup=markup, parse_mode="Markdown")

def ask_gemini(prompt, fallbacks):
    try:
        model = genai.GenerativeModel('gemini-1.5-pro')
        response = model.generate_content(prompt, generation_config=genai.types.GenerationConfig(temperature=1.2))
        return response.text.strip().replace('"', '').replace('*', '')
    except Exception as e:
        return random.choice(fallbacks)

def gen_ai_markup(step_name):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🎲 Пусть духи придумают", callback_data=f"ai_{step_name}"))
    return markup

@bot.callback_query_handler(func=lambda call: call.data.startswith("faction_"))
def process_faction(call):
    chat_id = call.message.chat.id
    if user_states.get(chat_id) != STATE_FACTION:
        return
    faction_key = call.data.split("_")[1]
    faction_name = FACTIONS[faction_key][1]
    user_data[chat_id]['faction'] = faction_name
    user_data[chat_id]['faction_prompt'] = FACTIONS[faction_key][2]
    user_states[chat_id] = STATE_NAME
    try:
        bot.answer_callback_query(call.id)
    except:
        pass
    bot.edit_message_text(f"✅ Фракция: **{FACTIONS[faction_key][0]}**", chat_id, call.message.message_id, parse_mode="Markdown")
    bot.send_message(chat_id, "📜 **Как тебя звать-величать?**", reply_markup=gen_ai_markup("name"), parse_mode="Markdown")

@bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) == STATE_NAME)
def process_name(message):
    chat_id = message.chat.id
    name = message.text.strip()
    if len(name) > 35:
        bot.send_message(chat_id, "⚠️ Лавк сказал коротко! Слишком много букв.")
        return
    user_data[chat_id]['name'] = name
    user_states[chat_id] = STATE_CATEGORY
    bot.send_message(chat_id, "🧬 **Ты вообще кто такой? (Категория)**", reply_markup=gen_ai_markup("category"), parse_mode="Markdown")

@bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) == STATE_CATEGORY)
def process_category(message):
    chat_id = message.chat.id
    category = message.text.strip()
    if len(category.split()) > 3 or len(category) > 30:
        bot.send_message(chat_id, "⚠️ Слишком длинно!")
        return
    user_data[chat_id]['category'] = category
    user_states[chat_id] = STATE_POWER
    bot.send_message(chat_id, "⚔️ **Насколько больно бьешь? (Сила 1-99)**", reply_markup=gen_ai_markup("power"), parse_mode="Markdown")

@bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) == STATE_POWER)
def process_power(message):
    chat_id = message.chat.id
    text = message.text.strip()
    if not text.isdigit() or not (1 <= int(text) <= 99):
        bot.send_message(chat_id, "⚠️ Только цифры от 1 до 99!")
        return
    user_data[chat_id]['power'] = text
    user_states[chat_id] = STATE_ARMOR
    bot.send_message(chat_id, "🛡 **Щит есть? (Броня 0-99)**", reply_markup=gen_ai_markup("armor"), parse_mode="Markdown")

@bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) == STATE_ARMOR)
def process_armor(message):
    chat_id = message.chat.id
    text = message.text.strip()
    if not text.isdigit() or not (0 <= int(text) <= 99):
        bot.send_message(chat_id, "⚠️ Только цифры от 0 до 99!")
        return
    user_data[chat_id]['armor'] = text
    user_states[chat_id] = STATE_PROVISION
    bot.send_message(chat_id, "🍖 **Сколько еды жрешь в походе? (Стоимость найма 4-99)**", reply_markup=gen_ai_markup("provision"), parse_mode="Markdown")

@bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) == STATE_PROVISION)
def process_provision(message):
    chat_id = message.chat.id
    text = message.text.strip()
    if not text.isdigit() or not (4 <= int(text) <= 99):
        bot.send_message(chat_id, "⚠️ От 4 до 99!")
        return
    user_data[chat_id]['provision'] = text
    user_states[chat_id] = STATE_FRAME
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("🟡 Золотая", callback_data="frame_Золотая"),
        types.InlineKeyboardButton("🟤 Бронзовая", callback_data="frame_Бронзовая")
    )
    bot.send_message(chat_id, "🖼 **Какой редкости будет твоя карта?**", reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith("frame_"))
def process_frame(call):
    chat_id = call.message.chat.id
    if user_states.get(chat_id) != STATE_FRAME:
        return
    frame = call.data.split("_")[1]
    user_data[chat_id]['frame'] = frame
    user_states[chat_id] = STATE_ABILITY
    try:
        bot.answer_callback_query(call.id)
    except:
        pass
    bot.edit_message_text(f"✅ Редкость: **{frame}**", chat_id, call.message.message_id, parse_mode="Markdown")
    bot.send_message(chat_id, "✨ **Что делать умеешь на столе? (Способность)**", reply_markup=gen_ai_markup("ability"), parse_mode="Markdown")

@bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) == STATE_ABILITY)
def process_ability(message):
    chat_id = message.chat.id
    text = message.text.strip()
    if len(text) > 150:
        bot.send_message(chat_id, f"⚠️ Слишком длинно ({len(text)}/150)!")
        return
    user_data[chat_id]['ability'] = text
    user_states[chat_id] = STATE_QUOTE
    bot.send_message(chat_id, "💬 **Умная мысль для самого низа (Цитата)**", reply_markup=gen_ai_markup("quote"), parse_mode="Markdown")

@bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) == STATE_QUOTE)
def process_quote(message):
    chat_id = message.chat.id
    user_data[chat_id]['quote'] = message.text.strip()
    ask_for_confirmation(chat_id)

def run_gemini_and_transition(chat_id, step, faction, message_id):
    try:
        bot.edit_message_text("*(Духи шепчут...)*", chat_id=chat_id, message_id=message_id, parse_mode="Markdown")
    except:
        pass

    if step == "name":
        res = ask_gemini(f"Сгенерируй случайное, уникальное имя для персонажа Гвинта (фракция {faction}). 1-3 слова. Только имя.", ["Грозный Воин", "Старый Волк"])
        user_data[chat_id]['name'] = res
        user_states[chat_id] = STATE_CATEGORY
        bot.send_message(chat_id, "✨ Имя: **" + res + "**

🧬 **Ты вообще кто такой? (Категория)**", reply_markup=gen_ai_markup("category"), parse_mode="Markdown")
    
    elif step == "category":
        res = ask_gemini(f"Сгенерируй случайные теги/категорию для Гвинта, фракция {faction}. 1-2 слова.", ["Человек, Воин", "Чудовище"])
        user_data[chat_id]['category'] = res
        user_states[chat_id] = STATE_POWER
        bot.send_message(chat_id, "✨ Категория: **" + res + "**

⚔️ **Насколько больно бьешь? (Сила 1-99)**", reply_markup=gen_ai_markup("power"), parse_mode="Markdown")
    
    elif step == "power":
        res = ask_gemini("Выдай случайное число от 2 до 12. Только число.", [str(random.randint(2, 12))])
        user_data[chat_id]['power'] = res
        user_states[chat_id] = STATE_ARMOR
        bot.send_message(chat_id, "✨ Сила: **" + res + "**

🛡 **Щит есть? (Броня 0-99)**", reply_markup=gen_ai_markup("armor"), parse_mode="Markdown")
        
    elif step == "armor":
        res = ask_gemini("Выдай случайное число от 0 до 4. Только число.", [str(random.randint(0, 4))])
        user_data[chat_id]['armor'] = res
        user_states[chat_id] = STATE_PROVISION
        bot.send_message(chat_id, "✨ Броня: **" + res + "**

🍖 **Сколько еды жрешь в походе? (Наём 4-99)**", reply_markup=gen_ai_markup("provision"), parse_mode="Markdown")
        
    elif step == "provision":
        res = ask_gemini("Выдай случайное число от 4 до 14. Только число.", [str(random.randint(4, 14))])
        user_data[chat_id]['provision'] = res
        user_states[chat_id] = STATE_FRAME
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("🟡 Золотая", callback_data="frame_Золотая"),
            types.InlineKeyboardButton("🟤 Бронзовая", callback_data="frame_Бронзовая")
        )
        bot.send_message(chat_id, "✨ Наём: **" + res + "**

🖼 **Какой редкости будет твоя карта?**", reply_markup=markup, parse_mode="Markdown")
        
    elif step == "ability":
        res = ask_gemini(f"Сгенерируй новую способность карты для игры Гвинт (фракция {faction}). Максимум 25 слов. Без кавычек.", ["РАЗМЕЩЕНИЕ: Нанесите 2 единицы урона."])
        user_data[chat_id]['ability'] = res
        user_states[chat_id] = STATE_QUOTE
        bot.send_message(chat_id, "✨ Способность:
_" + res + "_

💬 **Умная мысль для самого низа (Цитата)**", reply_markup=gen_ai_markup("quote"), parse_mode="Markdown")
        
    elif step == "quote":
        res = ask_gemini(f"Сгенерируй случайную мрачную цитату для карты Гвинт (фракция {faction}). Максимум 20 слов. Без кавычек.", ["Смерть — лишь начало."])
        user_data[chat_id]['quote'] = res
        ask_for_confirmation(chat_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("ai_"))
def process_ai_button(call):
    chat_id = call.message.chat.id
    step = call.data.split("_")[1]
    faction = user_data.get(chat_id, {}).get("faction", "Нейтральная")
    try:
        bot.answer_callback_query(call.id, "Духи думают...")
    except:
        pass
    threading.Thread(target=run_gemini_and_transition, args=(chat_id, step, faction, call.message.message_id)).start()

def ask_for_confirmation(chat_id):
    user_states[chat_id] = STATE_NONE 
    data = user_data[chat_id]
    
    summary = (
        "🎉 **Лавк собрал твои бумажки! ПРОВЕРЯЙ:**

"
        "🏴 Фракция: " + str(data.get('faction')) + "
"
        "📜 Имя: " + str(data.get('name')) + "
"
        "🧬 Категория: " + str(data.get('category')) + "
"
        "⚔️ Сила: " + str(data.get('power')) + " | 🛡 Броня: " + str(data.get('armor')) + " | 🍖 Наём: " + str(data.get('provision')) + "
"
        "🖼 Редкость: " + str(data.get('frame')) + "
"
        "✨ Способность: " + str(data.get('ability')) + "
"
        "💬 Цитата: _" + str(data.get('quote')) + "_

"
        "**Всё верно, человече? Запускаем нейромагию?**"
    )
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ ДА, РИСУЙ!", callback_data="confirm_yes"),
        types.InlineKeyboardButton("❌ ОТМЕНА", callback_data="confirm_no")
    )
    bot.send_message(chat_id, summary, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data in ["confirm_yes", "confirm_no"])
def process_confirmation(call):
    chat_id = call.message.chat.id
    try:
        bot.answer_callback_query(call.id)
    except:
        pass
        
    try:
        if call.data == "confirm_no":
            bot.edit_message_text("Эх, порвали холст! Начинай заново, когда будешь готов (нажми /start).", chat_id=chat_id, message_id=call.message.message_id)
            user_data.pop(chat_id, None)
            return
            
        bot.edit_message_text("✅ Данные подтверждены! Лавк пошел за красками...", chat_id=chat_id, message_id=call.message.message_id)
        threading.Thread(target=start_generation, args=(chat_id,)).start()
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Ошибка в кнопке: {str(e)}")

def start_generation(chat_id):
    data = user_data.get(chat_id)
    if not data:
        return
        
    status_msg = bot.send_message(chat_id, "🎨 *Лавк творит истинную магию (вживляем твое лицо в арт)... Это займет около 30-40 секунд!*", parse_mode="Markdown")
    
    if not FAL_KEY:
        bot.edit_message_text("❌ ОШИБКА: FAL_KEY пустой!", chat_id, status_msg.message_id)
        return

    try:
        file_info = bot.get_file(data['photo_id'])
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
        downloaded_file = requests.get(file_url, timeout=90).content 
        
        headers_upload = {
            "Authorization": f"Key {FAL_KEY}",
            "Content-Type": "image/jpeg"
        }
        upload_resp = requests.post(
            "https://fal.run/fal-ai/storage/upload", 
            headers=headers_upload, 
            data=downloaded_file, 
            timeout=90
        )
        fal_image_url = upload_resp.json().get("url")
        
        faction_prompt = data.get('faction_prompt', 'dark medieval fantasy')
        
        payload = {
            "prompt": faction_prompt, 
            "reference_image_url": fal_image_url,
            "image_size": "portrait_4_3", 
            "num_inference_steps": 28,
            "guidance_scale": 4.0, 
            "true_cfg": 1.0,
            "id_weight": 0.85 
        }
        headers = {
            "Authorization": f"Key {FAL_KEY}",
            "Content-Type": "application/json"
        }
        
        response = requests.post("https://fal.run/fal-ai/flux-pulid", json=payload, headers=headers, timeout=120)
        
        if response.status_code == 200:
            result_data = response.json()
            image_url = result_data['images'][0]['url']
            
            img_data = requests.get(image_url, timeout=90).content
            
            try:
                bot.delete_message(chat_id, status_msg.message_id)
            except:
                pass
                
            bot.send_photo(
                chat_id, 
                img_data, 
                caption="🔥 **ТВОЯ ИСТИННАЯ КАРТИЩА ГОТОВА!**

*(Только твое лицо, остальное — чистый лор!)*", 
                reply_markup=get_main_menu(), 
                parse_mode="Markdown"
            )
        else:
            bot.edit_message_text(f"⚠️ Ошибка от Fal.ai:
Код: {response.status_code}
Ответ: {response.text}", chat_id, status_msg.message_id, parse_mode="Markdown")
            
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Системная ошибка связи: {str(e)}", reply_markup=get_main_menu())

if __name__ == '__main__':
    print("Bot is listening (Clean Syntax Version)...")
    bot.infinity_polling(timeout=90, long_polling_timeout=60)