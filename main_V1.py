import discord
from deep_translator import GoogleTranslator
from langdetect import detect, DetectorFactory

DetectorFactory.seed = 0  # Pour rendre la détection stable

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"🤖 Connecté en tant que {client.user}")


@client.event
async def on_message(message):
    if message.author == client.user:
        return

    texte = message.content

    try:
        langue = detect(texte)
    except:
        langue = "unknown"

    if langue == "fr":
        traduction = GoogleTranslator(source='fr',
                                      target='en').translate(texte)
        await message.channel.send(f"🇬🇧 Traduction : {traduction}")

    elif langue == "en":
        traduction = GoogleTranslator(source='en',
                                      target='fr').translate(texte)
        await message.channel.send(f"🇫🇷 Traduction : {traduction}")

    else:
        # Si langue non détectée ou autre, on ne fait rien
        pass


TOKEN = ""
client.run(TOKEN)
