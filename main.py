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

async def mark_ticket_solved(channel: discord.TextChannel, user: discord.Member):
    overwrites = channel.overwrites
    overwrites[user] = discord.PermissionOverwrite(read_messages=True, send_messages=False)
    await channel.edit(overwrites=overwrites)

    messages = await channel.history(limit=50, oldest_first=True).flatten()
    user_messages = [msg.content for msg in messages if msg.author == user]
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
        await interaction.response.send_message("Ticket confirmed! Creating your ticket...", ephemeral=True)

    @ui.button(label="❎ Cancel", style=ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user != self.user:
            await interaction.response.send_message("This is not for you.", ephemeral=True)
            return
        self.value = False
        self.stop()
        await interaction.response.send_message("Ticket creation canceled.", ephemeral=True)

# ===== SLASH COMMANDS =====
@bot.slash_command(description="Open a support ticket")
async def ticket(ctx: discord.ApplicationContext):
    settings = load_settings()
    active_tickets = settings.get("active_tickets", {})

    if str(ctx.user.id) in active_tickets:
        await ctx.respond("You already have an open ticket.", ephemeral=True)
        return

    try:
        view = TicketConfirm(ctx.user, ctx)
        dm = await ctx.user.send("Do you want to open a support ticket? Click ✅ to confirm or ❎ to cancel.", view=view)
        await view.wait()

        if view.value is None:
            await ctx.user.send("Ticket creation timed out. Please try again.")
            await ctx.respond("Ticket creation timed out. Check your DMs.", ephemeral=True)
            return
        elif view.value is False:
            await ctx.respond("Ticket creation canceled.", ephemeral=True)
            return

    except discord.Forbidden:
        await ctx.respond("I cannot DM you! Please enable DMs from server members.", ephemeral=True)
        return

    # Create ticket
    category_id = int(settings.get("ticket_category", 0))
    category = discord.utils.get(ctx.guild.categories, id=category_id)
    if category is None:
        await ctx.respond("Ticket category not set or category not found.", ephemeral=True)
        return

    overwrites = {
        ctx.guild.default_role: discord.PermissionOverwrite(read_messages=False),
        ctx.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        ctx.guild.get_role(int(settings.get("staff_role", 0))): discord.PermissionOverwrite(read_messages=True, send_messages=True)
    }

    channel = await ctx.guild.create_text_channel(
        name=f"ticket-{ctx.user.name}",
        category=category,
        overwrites=overwrites
    )

    active_tickets[str(ctx.user.id)] = channel.id
    settings["active_tickets"] = active_tickets
    save_settings(settings)

    staff_role = ctx.guild.get_role(int(settings.get("staff_role", 0)))
    embed = discord.Embed(
        title="New Ticket",
        description=f"{ctx.user.mention} created a ticket.\n{staff_role.mention}, please assist!\nThe bot will automatically detect the problem from recent chat.",
        color=discord.Color.red()
    )
    await channel.send(embed=embed, view=SolvedButton(channel, ctx.user))
    await ctx.respond(f"Your ticket has been created: {channel.mention}", ephemeral=True)

# ===== SETTINGS COMMANDS =====
@bot.slash_command(description="Show current mod mail settings (admin/staff only)")
async def settings(ctx: discord.ApplicationContext):
    settings_data = load_settings()
    embed = discord.Embed(title="Mod Mail Settings", color=discord.Color.blue())
    staff_role = settings_data.get("staff_role")
    log_channel = settings_data.get("log_channel")
    ticket_category = settings_data.get("ticket_category")
    cooldown = settings_data.get("cooldown")
    embed.add_field(name="Staff Role", value=f"<@&{staff_role}>" if staff_role else "Not set", inline=False)
    embed.add_field(name="Log Channel", value=f"<#{log_channel}>" if log_channel else "Not set", inline=False)
    embed.add_field(name="Ticket Category", value=f"<#{ticket_category}>" if ticket_category else "Not set", inline=False)
    embed.add_field(name="Cooldown", value=f"{cooldown} seconds", inline=False)
    await ctx.respond(embed=embed, ephemeral=True)

@bot.slash_command(description="Set the staff role (admin only)")
async def set_staff_role(ctx: discord.ApplicationContext, role: discord.Role):
    if not ctx.user.guild_permissions.administrator:
        await ctx.respond("Only admins can use this.", ephemeral=True)
        return
    settings_data = load_settings()
    settings_data["staff_role"] = role.id
    save_settings(settings_data)
    await ctx.respond(f"Staff role set to {role.mention}", ephemeral=True)

@bot.slash_command(description="Set the log channel (admin only)")
async def set_log_channel(ctx: discord.ApplicationContext, channel: discord.TextChannel):
    if not ctx.user.guild_permissions.administrator:
        await ctx.respond("Only admins can use this.", ephemeral=True)
        return
    settings_data = load_settings()
    settings_data["log_channel"] = channel.id
    save_settings(settings_data)
    await ctx.respond(f"Log channel set to {channel.mention}", ephemeral=True)

@bot.slash_command(description="Set ticket category (admin only)")
async def set_category(ctx: discord.ApplicationContext, category: discord.CategoryChannel):
    if not ctx.user.guild_permissions.administrator:
        await ctx.respond("Only admins can use this.", ephemeral=True)
        return
    settings_data = load_settings()
    settings_data["ticket_category"] = category.id
    save_settings(settings_data)
    await ctx.respond(f"Ticket category set to {category.name}", ephemeral=True)

@bot.slash_command(description="Set ticket cooldown in seconds (admin only)")
async def set_cooldown(ctx: discord.ApplicationContext, seconds: int):
    if not ctx.user.guild_permissions.administrator:
        await ctx.respond("Only admins can use this.", ephemeral=True)
        return
    settings_data = load_settings()
    settings_data["cooldown"] = seconds
    save_settings(settings_data)
    await ctx.respond(f"Ticket cooldown set to {seconds} seconds.", ephemeral=True)

# ===== PANEL COMMANDS =====
@bot.slash_command(description="Send mod mail panel (admin/staff only)")
async def send_panel(ctx: discord.ApplicationContext):
    settings = load_settings()
    staff_role = ctx.guild.get_role(int(settings.get("staff_role", 0)))
    embed = discord.Embed(title="Mod Mail Panel", description=f"Click below to open a ticket.\nStaff: {staff_role.mention}", color=discord.Color.green())
    view = ui.View()
    view.add_item(ui.Button(label="Open Ticket", style=ButtonStyle.blurple, custom_id="open_ticket_button"))
    await ctx.send(embed=embed, view=view)
    await ctx.respond("Panel sent.", ephemeral=True)

@bot.slash_command(description="Refresh the mod mail panel (admin/staff only)")
async def refresh(ctx: discord.ApplicationContext):
    settings = load_settings()
    staff_role = ctx.guild.get_role(int(settings.get("staff_role", 0)))
    async for message in ctx.channel.history(limit=100):
        if message.author == bot.user and message.embeds:
            embed = message.embeds[0]
            if embed.title == "Mod Mail Panel":
                await message.delete()
    embed = discord.Embed(
        title="Mod Mail Panel",
        description=f"Click below to open a ticket.\nStaff: {staff_role.mention}",
        color=discord.Color.green()
    )
    view = ui.View()
    view.add_item(ui.Button(label="Open Ticket", style=ButtonStyle.blurple, custom_id="open_ticket_button"))
    await ctx.send(embed=embed, view=view)
    await ctx.respond("Panel refreshed. All old panels removed.", ephemeral=True)

@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type != discord.InteractionType.component:
        return
    if interaction.data["custom_id"] == "open_ticket_button":
        ctx = await bot.get_context(interaction.message)
        await ticket(ctx)

# ===== CLOSE COMMAND =====
@bot.slash_command(description="Close the ticket (staff/admin only)")
async def close(ctx: discord.ApplicationContext):
    if not ctx.channel.name.startswith("ticket-"):
        await ctx.respond("This is not a ticket channel.", ephemeral=True)
        return
    settings = load_settings()
    staff_role = ctx.guild.get_role(int(settings.get("staff_role", 0)))
    if staff_role.id not in [role.id for role in ctx.user.roles]:
        await ctx.respond("Only staff can close tickets.", ephemeral=True)
        return
    active_tickets = settings.get("active_tickets", {})
    for user_id, ch_id in list(active_tickets.items()):
        if ch_id == ctx.channel.id:
            del active_tickets[user_id]
    settings["active_tickets"] = active_tickets
    save_settings(settings)
    await ctx.channel.delete()

bot.run(BOT_TOKEN)
