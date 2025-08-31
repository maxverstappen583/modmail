import discord
from discord.ext import commands
from discord import ui, ButtonStyle, app_commands
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
GUILD_ID = int(os.getenv("GUILD_ID", 0))
OTHER_GUILD_RESPONSE = os.getenv("OTHER_GUILD_RESPONSE", "Sorry, this bot is only available in the official server!")
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

# ===== GUILD CHECK DECORATOR =====
def guild_only():
    async def predicate(ctx):
        if ctx.guild is None:
            return True
        if ctx.guild.id != GUILD_ID:
            await ctx.send(OTHER_GUILD_RESPONSE, ephemeral=True)
            return False
        return True
    return commands.check(predicate)

def guild_only_app():
    async def check(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            return True
        if interaction.guild.id != GUILD_ID:
            await interaction.response.send_message(OTHER_GUILD_RESPONSE, ephemeral=True)
            return False
        return True
    return app_commands.check(check)

# ===== EMBED HELPER =====
def get_embed_color(member: discord.Member):
    for role in reversed(member.roles):
        if role.color.value != 0:
            return role.color
    return discord.Color.greyple()

def create_embed(user: discord.User, content: str, attachments=None, member_obj=None):
    color = get_embed_color(member_obj) if member_obj else discord.Color.greyple()
    embed = discord.Embed(description=content, color=color)
    embed.set_author(name=str(user), icon_url=user.display_avatar.url)
    embed.set_footer(text="@u4_straight1", icon_url="https://i.postimg.cc/rp5b7Jkn/IMG-6152.jpg")
    if attachments:
        embed.set_image(url=attachments[0].url)
    return embed

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
    embed.set_footer(text="@u4_straight1", icon_url="https://i.postimg.cc/rp5b7Jkn/IMG-6152.jpg")
    await channel.send(embed=embed, view=CloseButton(channel))

# ===== VIEWS =====
class CloseButton(ui.View):
    def __init__(self, channel):
        super().__init__(timeout=None)
        self.channel = channel

    @ui.button(label="Close Ticket", style=ButtonStyle.red)
    async def close_ticket(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        settings = load_settings()
        staff_role = interaction.guild.get_role(int(settings.get("staff_role", 0)))
        if staff_role.id not in [role.id for role in interaction.user.roles]:
            await interaction.followup.send("Only staff can close tickets.", ephemeral=True)
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
        await interaction.response.defer(ephemeral=True)
        settings = load_settings()
        staff_role = interaction.guild.get_role(int(settings.get("staff_role", 0)))
        if staff_role.id not in [role.id for role in interaction.user.roles]:
            await interaction.followup.send("Only staff can mark this as solved.", ephemeral=True)
            return
        await mark_ticket_solved(self.channel, self.user)
        await interaction.message.edit(view=None)
        await interaction.followup.send("Ticket locked and ready to be closed.", ephemeral=True)

# ===== DM ↔ TICKET SYNC =====
@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if message.guild and message.guild.id != GUILD_ID:
        await message.channel.send(OTHER_GUILD_RESPONSE)
        return

    settings = load_settings()
    guild = bot.get_guild(GUILD_ID)
    active_tickets = settings.get("active_tickets", {})

    # User DM → Ticket
    if isinstance(message.channel, discord.DMChannel):
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
            embed.set_footer(text="@u4_straight1", icon_url="https://i.postimg.cc/rp5b7Jkn/IMG-6152.jpg")
            await channel.send(embed=embed, view=SolvedButton(channel, message.author))
        embed = create_embed(message.author, message.content, message.attachments, member_obj=message.author)
        await channel.send(embed=embed)

    # Staff → User DM
    elif message.channel.name.startswith("ticket-") and not isinstance(message.channel, discord.DMChannel):
        for user_id, ch_id in active_tickets.items():
            if ch_id == message.channel.id:
                user = bot.get_user(int(user_id))
                break
        else:
            return
        if user:
            content = f"**Staff Reply:** {message.content}" if message.content else ""
            if message.attachments:
                files = [await a.to_file() for a in message.attachments]
                await user.send(content, files=files)
            else:
                await user.send(content)

    await bot.process_commands(message)

# ===== Typing indicator =====
@bot.event
async def on_typing(channel, user, when):
    if user.bot:
        return
    if isinstance(channel, discord.DMChannel):
        settings = load_settings()
        active_tickets = settings.get("active_tickets", {})
        if str(user.id) in active_tickets:
            ticket_channel = bot.get_guild(GUILD_ID).get_channel(active_tickets[str(user.id)])
            if ticket_channel:
                async with ticket_channel.typing():
                    await asyncio.sleep(2)

# ===== COMMANDS =====
@bot.command()
@guild_only()
async def refresh(ctx):
    await ctx.send("Panel refreshed ✅")

@bot.tree.command(name="send_panel")
@guild_only_app()
async def send_panel(interaction: discord.Interaction):
    await interaction.response.send_message("Panel sent ✅")

@bot.tree.command(name="settings")
@guild_only_app()
async def settings_cmd(interaction: discord.Interaction):
    settings = load_settings()
    desc = (
        f"**Staff Role:** <@&{settings.get('staff_role', 'Not set')}>\n"
        f"**Log Channel:** <#{settings.get('log_channel', 'Not set')}>\n"
        f"**Ticket Category:** {settings.get('ticket_category', 'Not set')}\n"
        f"**Cooldown:** {settings.get('cooldown', 60)} seconds\n"
        f"**Active Tickets:** {len(settings.get('active_tickets', {}))}"
    )
    embed = discord.Embed(title="ModMail Settings", description=desc, color=discord.Color.blurple())
    embed.set_footer(text="@u4_straight1", icon_url="https://i.postimg.cc/rp5b7Jkn/IMG-6152.jpg")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="set_staff_role")
@guild_only_app()
@app_commands.describe(role="Role for staff")
async def set_staff_role(interaction: discord.Interaction, role: discord.Role):
    settings = load_settings()
    settings["staff_role"] = role.id
    save_settings(settings)
    await interaction.response.send_message(f"Staff role set to {role.mention} ✅", ephemeral=True)

@bot.tree.command(name="set_log_channel")
@guild_only_app()
@app_commands.describe(channel="Channel for logs")
async def set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    settings = load_settings()
    settings["log_channel"] = channel.id
    save_settings(settings)
    await interaction.response.send_message(f"Log channel set to {channel.mention} ✅", ephemeral=True)

@bot.tree.command(name="set_category")
@guild_only_app()
@app_commands.describe(category="Category for tickets")
async def set_category(interaction: discord.Interaction, category: discord.CategoryChannel):
    settings = load_settings()
    settings["ticket_category"] = category.id
    save_settings(settings)
    await interaction.response.send_message(f"Ticket category set to {category.name} ✅", ephemeral=True)

@bot.tree.command(name="set_cooldown")
@guild_only_app()
@app_commands.describe(seconds="Cooldown in seconds")
async def set_cooldown(interaction: discord.Interaction, seconds: int):
    settings = load_settings()
    settings["cooldown"] = seconds
    save_settings(settings)
    await interaction.response.send_message(f"Cooldown set to {seconds} seconds ✅", ephemeral=True)

bot.run(BOT_TOKEN)
