import discord
from discord.ext import commands
from discord import app_commands
import os
from flask import Flask
import threading

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = 123456789012345678  # replace with your server ID
OWNER_ID = 1319292111325106296  # you only

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------- Flask Keep Alive ---------------- #
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive!"

def run():
    app.run(host="0.0.0.0", port=10000)

def keep_alive():
    t = threading.Thread(target=run)
    t.start()
# -------------------------------------------------- #

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    try:
        await bot.tree.sync()
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        print("✅ Slash commands synced globally + guild")
    except Exception as e:
        print(f"⚠️ Sync failed: {e}")


# ---------------- Refresh Command ---------------- #
@bot.tree.command(name="refresh", description="Refresh slash commands (Owner only)")
async def refresh(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("❌ You are not allowed to use this.", ephemeral=True)
        return
    try:
        await bot.tree.sync()
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        await interaction.response.send_message("✅ Commands refreshed successfully.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"⚠️ Refresh failed: {e}", ephemeral=True)
# ------------------------------------------------- #


# ---------------- Modmail Command ---------------- #
cooldowns = {}

@bot.tree.command(name="modmail", description="Send a message to staff (creates a ticket).")
async def modmail(interaction: discord.Interaction, message: str):
    user_id = interaction.user.id
    now = discord.utils.utcnow()

    # cooldown check
    if user_id in cooldowns:
        diff = (now - cooldowns[user_id]).total_seconds()
        if diff < 300:  # 5 minutes = 300 seconds
            remaining = int(300 - diff)
            await interaction.response.send_message(
                f"⏳ You must wait {remaining} seconds before sending another modmail.",
                ephemeral=True
            )
            return

    cooldowns[user_id] = now  # set cooldown

    guild = bot.get_guild(GUILD_ID)
    category = discord.utils.get(guild.categories, name="Modmail")
    if category is None:
        category = await guild.create_category("Modmail")

    channel = await category.create_text_channel(f"ticket-{interaction.user.name}")
    await channel.send(f"📩 New modmail from {interaction.user.mention}:\n>>> {message}")

    await interaction.response.send_message(
        "✅ Your modmail has been sent! Staff will reach out soon.",
        ephemeral=True
    )
# ------------------------------------------------- #

keep_alive()
bot.run(TOKEN)
