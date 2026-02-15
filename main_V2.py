import discord
from discord import app_commands
import asyncio
from deep_translator import MyMemoryTranslator, GoogleTranslator
from langdetect import detect, DetectorFactory
from collections import defaultdict, OrderedDict
import time
import logging
import os
import threading
from dotenv import load_dotenv
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

DetectorFactory.seed = 0

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

COOLDOWN_SECONDS = int(os.getenv('COOLDOWN_SECONDS', '30'))
MAX_TRANSLATIONS_PER_HOUR = int(os.getenv('MAX_TRANSLATIONS_PER_HOUR', '30'))

MIN_MESSAGE_LENGTH = 15
MAX_MESSAGE_LENGTH = 1500

CACHE_MAX_SIZE = 2000
CACHE_TTL_SECONDS = 3600

KEEP_ALIVE_PORT = int(os.getenv('PORT', '8080'))

TARGET_LANGUAGES = {
    'en': '🇬🇧 English',
    'fr': '🇫🇷 Français',
    'es': '🇪🇸 Español',
}

LANG_FLAGS = {'en': '🇬🇧', 'fr': '🇫🇷', 'es': '🇪🇸'}

class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain; charset=utf-8')
        self.end_headers()
        h, rem = divmod(int(time.time() - bot_start_time), 3600)
        m, _ = divmod(rem, 60)
        self.wfile.write(
            f"Bot alive | {h}h{m}m | Translations: {stats['translations_total']}\n".encode()
        )

    def log_message(self, *args):
        pass


def start_keep_alive():
    server = HTTPServer(('0.0.0.0', KEEP_ALIVE_PORT), KeepAliveHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info(f"🌐 Keep-alive {KEEP_ALIVE_PORT}")


bot_start_time = time.time()


class TTLCache:
    def __init__(self, max_size: int, ttl_seconds: int):
        self._cache: OrderedDict = OrderedDict()
        self._timestamps: dict = {}
        self.max_size = max_size
        self.ttl = ttl_seconds

    def get(self, key):
        if key in self._cache:
            if time.time() - self._timestamps[key] > self.ttl:
                del self._cache[key]
                del self._timestamps[key]
                return None
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def set(self, key, value):
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = value
        self._timestamps[key] = time.time()
        while len(self._cache) > self.max_size:
            oldest, _ = self._cache.popitem(last=False)
            self._timestamps.pop(oldest, None)

    def __len__(self):
        return len(self._cache)


translation_cache = TTLCache(max_size=CACHE_MAX_SIZE, ttl_seconds=CACHE_TTL_SECONDS)

author_cooldowns: defaultdict = defaultdict(float)
author_translation_count: defaultdict = defaultdict(list)

stats = {
    'translations_total': 0,
    'cache_hits': 0,
    'rate_limit_blocks': 0,
    'errors': 0,
    'api_calls': 0,
}


def check_rate_limit(author_id: int) -> tuple:
    now = time.time()
    if now - author_cooldowns[author_id] < COOLDOWN_SECONDS:
        stats['rate_limit_blocks'] += 1
        return False, "cooldown"
    hour_ago = now - 3600
    author_translation_count[author_id] = [
        ts for ts in author_translation_count[author_id] if ts > hour_ago
    ]
    if len(author_translation_count[author_id]) >= MAX_TRANSLATIONS_PER_HOUR:
        stats['rate_limit_blocks'] += 1
        return False, "hour limit"
    return True, ""


def update_rate_limit(author_id: int):
    now = time.time()
    author_cooldowns[author_id] = now
    author_translation_count[author_id].append(now)


def normalize_lang(lang: str) -> str:
    mapping = {'zh-cn': 'zh', 'zh-tw': 'zh', 'pt-br': 'pt', 'iw': 'he'}
    return mapping.get(lang.lower(), lang.lower())


async def translate_text(text: str, source_lang: str, target_lang: str) -> str:
    """MyMemory → Google fallback, cash."""
    cache_key = (text.strip(), source_lang, target_lang)

    cached = translation_cache.get(cache_key)
    if cached:
        stats['cache_hits'] += 1
        return cached

    try:
        result = await asyncio.to_thread(
            MyMemoryTranslator(source=source_lang, target=target_lang).translate, text
        )
        stats['api_calls'] += 1
        translation_cache.set(cache_key, result)
        return result
    except Exception as e:
        logger.warning(f"MyMemory ({source_lang}→{target_lang}): {e}")

    try:
        result = await asyncio.to_thread(
            GoogleTranslator(source=source_lang, target=target_lang).translate, text
        )
        stats['api_calls'] += 1
        translation_cache.set(cache_key, result)
        return result
    except Exception as e:
        logger.error(f"api dead ({source_lang}→{target_lang}): {e}")
        stats['errors'] += 1
        raise


def should_translate(message: discord.Message) -> bool:
    if message.author.bot:
        return False
    text = message.content.strip()
    if len(text) < MIN_MESSAGE_LENGTH or len(text) > MAX_MESSAGE_LENGTH:
        return False
    if text.startswith(('!', '/', 'http://', 'https://', '<@', '<#', '<:', '```')):
        return False
    if all(not c.isalpha() for c in text.replace(' ', '')):
        return False
    return True


@client.event
async def on_ready():
    logger.info(f"🤖 Bot {client.user} started!")
    logger.info(f"📊 Servers: {len(client.guilds)}")
    logger.info(f"🌐 Language: {', '.join(TARGET_LANGUAGES.keys())}")

    try:
        test = await translate_text("Bonjour le monde", "fr", "en")
        logger.info(f"✅ Translate worked: Bonjour le monde → {test}")
    except Exception as e:
        logger.error(f"❌ Translate not allowed: {e}")

    if os.getenv('SYNC_COMMANDS', '1') == '1':
        try:
            synced = await tree.sync()
            logger.info(f"✅ Synch {len(synced)} command")
        except Exception as e:
            logger.error(f"❌ Synch: {e}")

    await client.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="EN / FR / ES | /help")
    )


@client.event
async def on_message(message: discord.Message):
    if not should_translate(message):
        return

    can, _ = check_rate_limit(message.author.id)
    if not can:
        return

    text = message.content.strip()

    try:
        source_lang = normalize_lang(detect(text))
    except Exception:
        return

    langs_to_translate = [lang for lang in TARGET_LANGUAGES if lang != source_lang]

    if source_lang not in TARGET_LANGUAGES:
        langs_to_translate = list(TARGET_LANGUAGES.keys())

    if not langs_to_translate:
        return

    translations = {}
    for lang in langs_to_translate:
        try:
            translations[lang] = await translate_text(text, source_lang, lang)
        except Exception:
            continue

    if not translations:
        return

    embed = discord.Embed(color=discord.Color.blue())

    for lang, translated in translations.items():
        flag = LANG_FLAGS[lang]
        name = TARGET_LANGUAGES[lang]
        if len(translated) > 1000:
            translated = translated[:997] + "..."
        embed.add_field(name=f"{flag} {name}", value=translated, inline=False)

    try:
        await message.reply(embed=embed, mention_author=False)
        update_rate_limit(message.author.id)
        stats['translations_total'] += len(translations)
        logger.info(
            f"✅ [{message.author.name}] {source_lang} → {list(translations.keys())}"
        )
    except discord.Forbidden:
        logger.warning(f"Dont have perms. #{message.channel.name}")
    except discord.HTTPException as e:
        logger.error(f"Discord: {e}")
        stats['errors'] += 1

@tree.command(name="stats", description="Bot statistics")
async def stats_command(interaction: discord.Interaction):
    h, rem = divmod(int(time.time() - bot_start_time), 3600)
    m, _ = divmod(rem, 60)
    total = max(stats['translations_total'], 1)
    api_total = stats['cache_hits'] + stats['api_calls'] or 1
    cache_rate = (stats['cache_hits'] / api_total) * 100

    embed = discord.Embed(title="📊 Stats", color=discord.Color.blue())
    embed.add_field(name="📨 Translate", value=f"`{stats['translations_total']}`", inline=True)
    embed.add_field(name="💾 Cash", value=f"`{stats['cache_hits']}`", inline=True)
    embed.add_field(name="🌐 API", value=f"`{stats['api_calls']}`", inline=True)
    embed.add_field(name="📈 Cash %", value=f"`{cache_rate:.1f}%`", inline=True)
    embed.add_field(name="❌ Error", value=f"`{stats['errors']}`", inline=True)
    embed.add_field(name="⏱ Uptime", value=f"`{h}ч {m}м`", inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="help", description="Help about bot")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 Translation Bot",
        description=(
        ),
        color=discord.Color.blue()
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


def main():
    token = os.getenv('DISCORD_TOKEN')
    if not token:
        logger.error(
            "❌ DISCORD_TOKEN not found!\n"
            "Replit: add Secrets\n"
        )
        return

    start_keep_alive()
    logger.info("🚀 Start...")

    try:
        client.run(token, log_handler=None)
    except discord.LoginFailure:
        logger.error("❌ Wrong token!")
    except Exception as e:
        logger.error(f"❌ Error: {e}")


if __name__ == "__main__":
    main()