import discord
from discord import app_commands
from discord.ext import commands
import os
from dotenv import load_dotenv

# ===== Load ENV =====
load_dotenv()
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", 0))

# ===== Bot Setup =====
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ===== Guild Check =====
def guild_only_app():
    async def check(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            return True
        if interaction.guild.id != GUILD_ID:
            await interaction.response.send_message(
                "This bot only works in the official server!", ephemeral=True
            )
            return False
        return True
    return app_commands.check(check)

# ===== Slash Command Example =====
@bot.tree.command(name="hello")
@guild_only_app()
async def hello(interaction: discord.Interaction):
    await interaction.response.send_message(f"Hello {interaction.user.mention}! ✅")

# ===== On Ready =====
@bot.event
async def on_ready():
    print(f"{bot.user} is online!")
    guild = discord.Object(id=GUILD_ID)
    try:
        await bot.tree.sync(guild=guild)
        print(f"Slash commands synced to guild {GUILD_ID} ✅")
    except Exception as e:
        print("Failed to sync commands:", e)

bot.run(BOT_TOKEN)
