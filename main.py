import discord
from discord.ext import commands
from discord import ui, ButtonStyle
import openai
import os
import json
from flask import Flask
from threading import Thread
from dotenv import load_dotenv
import asyncio

# ===== LOAD ENV =====
load_dotenv()
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

# ===== FLASK =====
app = Flask("")

@app.route('/')
def home():
    return "Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

Thread(target=run_flask).start()

# ===== SETTINGS STORAGE =====
SETTINGS_FILE = "modmail_settings.json"
if not os.path.exists(SETTINGS_FILE):
    with open(SETTINGS_FILE, "w") as f:
        json.dump({"staff_role": None, "log_channel": None, "ticket_category": None, "cooldown": 60, "active_tickets": {}}, f)

def load_settings():
    with open(SETTINGS_FILE, "r") as f:
        return json.load(f)

def save_settings(settings):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=4)

# ===== BOT =====
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"{bot.user} is online and ready!")
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="Mod Mail"))

# ===== HELPER FUNCTIONS =====
async def summarize_problem(messages):
    text = "\n".join(messages[-20:])
    prompt = f"Summarize the following messages as the user's problem in one concise paragraph:\n{text}"
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print("OpenAI API error:", e)
        return "Problem could not be automatically detected."

def get_embed_color(member: discord.Member):
    for role in reversed(member.roles):
        if role.color.value != 0:
            return role.color
    return discord.Color.greyple()

async def create_ticket_channel(guild: discord.Guild, user: discord.User, settings):
    category_id = int(settings.get("ticket_category", 0))
    category = discord.utils.get(guild.categories, id=category_id)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.get_role(int(settings.get("staff_role", 0))): discord.PermissionOverwrite(read_messages=True, send_messages=True)
    }

    channel = await guild.create_text_channel(
        name=f"ticket-{user.name}",
        category=category,
        overwrites=overwrites
    )
    return channel

async def mark_ticket_solved(channel: discord.TextChannel, user: discord.User):
    overwrites = channel.overwrites
    overwrites[user] = discord.PermissionOverwrite(read_messages=True, send_messages=False)
    await channel.edit(overwrites=overwrites)

    messages = await channel.history(limit=50, oldest_first=True).flatten()
    user_messages = [msg.content for msg in messages if msg.author.id == user.id]
    problem_text = await summarize_problem(user_messages) if user_messages else "No messages detected."

    embed = discord.Embed(
        title="Ticket Resolved",
        description=f"**Problem detected automatically:**\n{problem_text}",
        color=discord.Color.red()
    )
    await channel.send(embed=embed, view=CloseButton(channel))

# ===== VIEWS =====
class CloseButton(ui.View):
    def __init__(self, channel):
        super().__init__(timeout=None)
        self.channel = channel

    @ui.button(label="Close Ticket", style=ButtonStyle.red)
    async def close_ticket(self, interaction: discord.Interaction, button: ui.Button):
        settings = load_settings()
        staff_role = interaction.guild.get_role(int(settings.get("staff_role", 0)))
        if staff_role.id not in [role.id for role in interaction.user.roles]:
            await interaction.response.send_message("Only staff can close tickets.", ephemeral=True)
            return
        active_tickets = settings.get("active_tickets", {})
        for user_id, ch_id in list(active_tickets.items()):
            if ch_id == self.channel.id:
                del active_tickets[user_id]
        settings["active_tickets"] = active_tickets
        save_settings(settings)
        await self.channel.delete()

class SolvedButton(ui.View):
    def __init__(self, channel, user):
        super().__init__(timeout=None)
        self.channel = channel
        self.user = user

    @ui.button(label="Problem Solved", style=ButtonStyle.green)
    async def problem_solved(self, interaction: discord.Interaction, button: ui.Button):
        settings = load_settings()
        staff_role = interaction.guild.get_role(int(settings.get("staff_role", 0)))
        if staff_role.id not in [role.id for role in interaction.user.roles]:
            await interaction.response.send_message("Only staff can mark this as solved.", ephemeral=True)
            return
        await mark_ticket_solved(self.channel, self.user)
        await interaction.message.edit(view=None)
        await interaction.response.send_message("Ticket locked and ready to be closed.", ephemeral=True)

# ===== DM CONFIRMATION =====
class TicketConfirm(ui.View):
    def __init__(self, user, ctx):
        super().__init__(timeout=60)
        self.user = user
        self.ctx = ctx
        self.value = None

    @ui.button(label="✅ Confirm", style=ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user != self.user:
            await interaction.response.send_message("This is not for you.", ephemeral=True)
            return
        self.value = True
        self.stop()
        await interaction.response.send_message("Ticket confirmed! Staff will now be notified.", ephemeral=True)

    @ui.button(label="❎ Cancel", style=ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user != self.user:
            await interaction.response.send_message("This is not for you.", ephemeral=True)
            return
        self.value = False
        self.stop()
        await interaction.response.send_message("Ticket creation canceled.", ephemeral=True)

# ===== DM ↔ TICKET SYNC WITH MEDIA =====
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # ===== User DMs =====
    if isinstance(message.channel, discord.DMChannel):
        settings = load_settings()
        guild = bot.guilds[0]  # Assumes bot in one server
        active_tickets = settings.get("active_tickets", {})

        # Get or create ticket channel
        if str(message.author.id) in active_tickets:
            channel = guild.get_channel(active_tickets[str(message.author.id)])
        else:
            channel = await create_ticket_channel(guild, message.author, settings)
            active_tickets[str(message.author.id)] = channel.id
            settings["active_tickets"] = active_tickets
            save_settings(settings)

            staff_role = guild.get_role(int(settings.get("staff_role", 0)))
            embed = discord.Embed(
                title="New Ticket",
                description=f"{message.author.mention} created a ticket.\n{staff_role.mention}, please assist!",
                color=discord.Color.red()
            )
            await channel.send(embed=embed, view=SolvedButton(channel, message.author))

        # Send user message + attachments to ticket channel as embed
        embed = discord.Embed(description=message.content, color=get_embed_color(message.author))
        embed.set_author(name=message.author, icon_url=message.author.display_avatar.url)
        if message.attachments:
            embed.set_image(url=message.attachments[0].url)  # Show first attachment
        await channel.send(embed=embed)

    # ===== Staff messages in ticket channel =====
    elif message.channel.name.startswith("ticket-") and not isinstance(message.channel, discord.DMChannel):
        settings = load_settings()
        active_tickets = settings.get("active_tickets", {})
        for user_id, ch_id in active_tickets.items():
            if ch_id == message.channel.id:
                user = bot.get_user(int(user_id))
                break
        else:
            return

        if user:
            content = f"**Staff Reply:** {message.content}" if message.content else ""
            if message.attachments:
                files = [await attachment.to_file() for attachment in message.attachments]
                await user.send(content, files=files)
            else:
                await user.send(content)

    await bot.process_commands(message)

# ===== Typing indicator in staff ticket channel =====
@bot.event
async def on_typing(channel, user, when):
    if user.bot:
        return
    if isinstance(channel, discord.DMChannel):
        settings = load_settings()
        active_tickets = settings.get("active_tickets", {})
        guild = bot.guilds[0]
        if str(user.id) in active_tickets:
            ticket_channel = guild.get_channel(active_tickets[str(user.id)])
            if ticket_channel:
                async with ticket_channel.typing():
                    await asyncio.sleep(2)

# ===== Admin/staff settings, panel, refresh, etc. remain same as previous version =====
# You can keep /set_staff_role, /set_log_channel, /set_category, /set_cooldown, /send_panel, /refresh commands
# along with /settings command, solved/close workflow

bot.run(BOT_TOKEN)
