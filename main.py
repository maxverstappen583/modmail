# main.py
# Full ModMail bot - guild-locked, slash commands, DM <-> ticket sync, attachments, transcripts, footer.

import os
import json
import asyncio
import datetime
from threading import Thread
from dotenv import load_dotenv
from flask import Flask

import discord
from discord.ext import commands
from discord import ui, ButtonStyle, app_commands

# ----------------- CONFIG / ENV -----------------
load_dotenv()
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")  # required
GUILD_ID = int(os.getenv("GUILD_ID", "0"))  # required (your server id)
OTHER_GUILD_RESPONSE = os.getenv("OTHER_GUILD_RESPONSE", "Sorry, this bot only works in the official server.")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")  # optional (leave blank to disable OpenAI)

# Optional OpenAI setup
USE_OPENAI = False
if OPENAI_API_KEY:
    try:
        import openai
        openai.api_key = OPENAI_API_KEY
        USE_OPENAI = True
    except Exception:
        USE_OPENAI = False

# Footer
FOOTER_TEXT = "@u4_straight1"
FOOTER_ICON = "https://i.postimg.cc/rp5b7Jkn/IMG-6152.jpg"

# ----------------- FLASK KEEP-ALIVE (optional) -----------------
app = Flask("keepalive")

@app.route("/")
def home():
    return "ModMail bot running."

def run_flask():
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)

Thread(target=run_flask, daemon=True).start()

# ----------------- SETTINGS STORAGE -----------------
SETTINGS_FILE = "modmail_settings.json"
DEFAULT_SETTINGS = {
    "staff_role": 0,
    "log_channel": 0,
    "ticket_category": 0,
    "cooldown": 60,
    "active_tickets": {},  # "user_id": channel_id
    "last_open": {}        # "user_id": iso timestamp
}

def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        save_settings(DEFAULT_SETTINGS.copy())
    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    changed = False
    for k, v in DEFAULT_SETTINGS.items():
        if k not in data:
            data[k] = v
            changed = True
    if changed:
        save_settings(data)
    return data

def save_settings(data):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

settings = load_settings()

# ----------------- BOT SETUP -----------------
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

# ----------------- HELPERS -----------------
def guild_only_app():
    """Decorator to only allow slash commands in the configured guild; respond with custom message elsewhere."""
    async def check(interaction: discord.Interaction) -> bool:
        if interaction.guild is None or interaction.guild.id != GUILD_ID:
            # If used in other guild or in a DM, reply with custom message
            try:
                await interaction.response.send_message(OTHER_GUILD_RESPONSE, ephemeral=True)
            except Exception:
                # fallback if response already sent
                pass
            return False
        return True
    return app_commands.check(check)

def is_staff(member: discord.Member) -> bool:
    sr = int(settings.get("staff_role") or 0)
    if sr and any(role.id == sr for role in member.roles):
        return True
    # fallback
    return member.guild_permissions.manage_guild or member.guild_permissions.administrator

def role_color_for_member(member: discord.Member) -> discord.Color:
    for r in reversed(member.roles):
        if r.color.value != 0:
            return r.color
    return discord.Color.greyple()

def make_user_embed(user: discord.User, content: str, member_obj: discord.Member | None = None, attachments=None) -> discord.Embed:
    color = role_color_for_member(member_obj) if member_obj else discord.Color.blurple()
    embed = discord.Embed(description=content or "\u200b", color=color)
    embed.set_author(name=str(user), icon_url=user.display_avatar.url)
    embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON)
    if attachments:
        for a in attachments:
            if a.content_type and a.content_type.startswith("image/"):
                embed.set_image(url=a.url)
                break
    return embed

async def ensure_category(guild: discord.Guild) -> discord.CategoryChannel:
    cat_id = int(settings.get("ticket_category") or 0)
    if cat_id:
        cat = discord.utils.get(guild.categories, id=cat_id)
        if cat:
            return cat
    cat = discord.utils.get(guild.categories, name="Tickets")
    if cat:
        return cat
    return await guild.create_category("Tickets")

async def create_ticket_channel(guild: discord.Guild, opener: discord.User) -> discord.TextChannel:
    staff_role_id = int(settings.get("staff_role") or 0)
    staff_role = guild.get_role(staff_role_id) if staff_role_id else None
    category = await ensure_category(guild)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    }
    overwrites[opener] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    ch = await guild.create_text_channel(name=f"ticket-{opener.id}", category=category, overwrites=overwrites)
    settings["active_tickets"][str(opener.id)] = ch.id
    save_settings(settings)

    ping = staff_role.mention if staff_role else "@here"
    intro = discord.Embed(title="🎫 New Ticket", description=f"{opener.mention} opened a ticket.\n{ping}", color=discord.Color.red())
    intro.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON)
    await ch.send(content=ping, embed=intro, view=TicketControlsView(opener_id=opener.id))
    return ch

async def transcript_for_channel(channel: discord.TextChannel) -> str:
    parts = []
    async for m in channel.history(limit=None, oldest_first=True):
        ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {m.author} ({m.author.id}): {m.content or ''}"
        parts.append(line)
        if m.attachments:
            for a in m.attachments:
                parts.append(f"    [attachment] {a.filename} -> {a.url}")
    text = "\n".join(parts) if parts else "No messages."
    fname = f"transcript-{channel.name}-{channel.id}.txt"
    with open(fname, "w", encoding="utf-8") as f:
        f.write(text)
    return fname

async def close_and_archive(channel: discord.TextChannel, closed_by: discord.abc.User):
    fname = await transcript_for_channel(channel)
    file = discord.File(fname)

    log_id = int(settings.get("log_channel") or 0)
    if log_id:
        log_ch = channel.guild.get_channel(log_id)
        if log_ch:
            embed = discord.Embed(title="📑 Ticket Closed", description=f"Channel: {channel.mention}\nClosed by: {closed_by.mention}", color=discord.Color.red())
            embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON)
            await log_ch.send(embed=embed, file=file)

    # DM transcript to opener if mapping exists
    opener_uid = None
    for uid, cid in list(settings.get("active_tickets", {}).items()):
        if cid == channel.id:
            opener_uid = int(uid)
            break
    if opener_uid:
        try:
            user = await bot.fetch_user(opener_uid)
            await user.send("📑 Here is the transcript of your ticket.", file=discord.File(fname))
        except Exception:
            pass

    try: os.remove(fname)
    except Exception: pass

    if opener_uid:
        settings["active_tickets"].pop(str(opener_uid), None)
        save_settings(settings)

    try:
        await channel.delete()
    except Exception:
        pass

# ----------------- UI / Views -----------------
class PanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Open Ticket", style=ButtonStyle.danger, custom_id="modmail_open_ticket")
    async def open_ticket(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.guild is None or interaction.guild.id != GUILD_ID:
            return await interaction.response.send_message(OTHER_GUILD_RESPONSE, ephemeral=True)

        s = load_settings()
        uid = str(interaction.user.id)
        active = s.get("active_tickets", {})
        if uid in active:
            ch = interaction.guild.get_channel(active[uid])
            if ch:
                return await interaction.response.send_message(f"❌ You already have a ticket: {ch.mention}", ephemeral=True)
            else:
                active.pop(uid, None)
                s["active_tickets"] = active
                save_settings(s)

        cooldown = int(s.get("cooldown") or 0)
        last_open = s.get("last_open", {})
        if cooldown and uid in last_open:
            try:
                last_ts = datetime.datetime.fromisoformat(last_open[uid])
                delta = (datetime.datetime.utcnow() - last_ts).total_seconds()
                if delta < cooldown:
                    return await interaction.response.send_message(f"⏳ Wait {int(cooldown - delta)}s to open another ticket.", ephemeral=True)
            except Exception:
                pass

        ch = await create_ticket_channel(interaction.guild, interaction.user)
        s["last_open"][uid] = datetime.datetime.utcnow().isoformat()
        save_settings(s)
        await interaction.response.send_message(f"✅ Ticket created: {ch.mention}", ephemeral=True)

class TicketControlsView(ui.View):
    def __init__(self, opener_id: int):
        super().__init__(timeout=None)
        self.opener_id = opener_id

    @ui.button(label="Problem Solved ✅", style=ButtonStyle.success, custom_id="ticket_solved_btn")
    async def solved(self, interaction: discord.Interaction, button: ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Only staff can use this.", ephemeral=True)

        opener_member = interaction.guild.get_member(self.opener_id)
        if opener_member:
            overwrites = interaction.channel.overwrites
            overwrites[opener_member] = discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True)
            await interaction.channel.edit(overwrites=overwrites)

        # Summary (OpenAI optional)
        summary = "Could not summarize."
        try:
            summary = await summarize_ticket(interaction.channel, self.opener_id)
        except Exception:
            pass

        embed = discord.Embed(title="✅ Marked as Solved", description=f"**Detected problem:**\n{summary}", color=discord.Color.red())
        embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON)
        await interaction.response.send_message(embed=embed, view=CloseView(self.opener_id))

    @ui.button(label="Not Solved ❎", style=ButtonStyle.secondary, custom_id="ticket_not_solved_btn")
    async def not_solved(self, interaction: discord.Interaction, button: ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Only staff can use this.", ephemeral=True)
        opener_member = interaction.guild.get_member(self.opener_id)
        if opener_member:
            overwrites = interaction.channel.overwrites
            overwrites[opener_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
            await interaction.channel.edit(overwrites=overwrites)
        await interaction.response.send_message("🔓 Ticket re-opened for the user.", ephemeral=True)

class CloseView(ui.View):
    def __init__(self, opener_id: int):
        super().__init__(timeout=None)
        self.opener_id = opener_id

    @ui.button(label="Close Ticket 🔒", style=ButtonStyle.danger, custom_id="ticket_close_btn")
    async def close_btn(self, interaction: discord.Interaction, button: ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Only staff can close tickets.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await close_and_archive(interaction.channel, interaction.user)

# ----------------- Summarizer (optional OpenAI) -----------------
async def summarize_ticket(channel: discord.TextChannel, user_id: int) -> str:
    parts = []
    async for m in channel.history(limit=200, oldest_first=True):
        if m.author.id == user_id and (m.content or m.attachments):
            chunk = m.content or ""
            if m.attachments:
                chunk += " " + " ".join(a.filename for a in m.attachments)
            parts.append(chunk)
    text = "\n".join(parts[-40:]).strip()
    if not text:
        return "No user messages to summarize."
    if USE_OPENAI:
        try:
            resp = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[{"role":"user", "content": f"Summarize the user's issue in one sentence:\n\n{text}"}],
                max_tokens=120, temperature=0.2
            )
            return resp.choices[0].message.content.strip()
        except Exception:
            pass
    return (text[:300] + "…") if len(text) > 300 else text

# ----------------- EVENTS -----------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    # Sync commands to your guild for instant availability
    try:
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        print(f"✅ Slash commands synced to guild {GUILD_ID}")
    except Exception as e:
        print("❌ Failed to sync commands:", e)
    # persistent panel view so button works after restart
    bot.add_view(PanelView())
    try:
        await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="ModMail"))
    except Exception:
        pass

@bot.event
async def on_message(message: discord.Message):
    # ignore bots
    if message.author.bot:
        return

    # ignore messages outside the guild (we only manage modmail for configured guild)
    if message.guild and message.guild.id != GUILD_ID:
        return

    s = load_settings()
    active = s.get("active_tickets", {})
    guild = bot.get_guild(GUILD_ID)

    # If user sends DM -> forward to/create ticket channel
    if isinstance(message.channel, discord.DMChannel):
        uid = str(message.author.id)
        ch = None
        if uid in active:
            ch = guild.get_channel(active[uid])
            if ch is None:
                ch = await create_ticket_channel(guild, message.author)
        else:
            ch = await create_ticket_channel(guild, message.author)

        member_obj = guild.get_member(message.author.id)
        embed = make_user_embed(message.author, message.content or "", member_obj=member_obj, attachments=message.attachments)
        files = [await a.to_file() for a in message.attachments] if message.attachments else None
        await ch.send(embed=embed, files=files or [])

    # If staff types in ticket channel -> forward to user's DM
    elif message.channel and message.channel.name.startswith("ticket-"):
        target_uid = None
        for uid, cid in active.items():
            if cid == message.channel.id:
                target_uid = int(uid)
                break
        if target_uid:
            try:
                user = await bot.fetch_user(target_uid)
            except Exception:
                user = bot.get_user(target_uid)
            if user:
                content = message.content or ""
                files = [await a.to_file() for a in message.attachments] if message.attachments else None
                if content:
                    content = f"**Staff:** {content}"
                try:
                    await user.send(content or "\u200b", files=files or [])
                except Exception:
                    pass

    # allow commands and other processing
    await bot.process_commands(message)

# ----------------- SLASH COMMANDS -----------------
@bot.tree.command(name="send_panel", description="Send the modmail panel here")
@guild_only_app()
async def send_panel(interaction: discord.Interaction):
    if not is_staff(interaction.user):
        return await interaction.response.send_message("Only staff can use this.", ephemeral=True)
    embed = discord.Embed(title="📮 Need Help?", description="Click **Open Ticket** to contact staff privately.", color=discord.Color.red())
    embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON)
    await interaction.response.send_message(embed=embed, view=PanelView())

@bot.tree.command(name="refresh", description="Refresh (re-sync) slash commands")
@guild_only_app()
async def refresh_cmd(interaction: discord.Interaction):
    if not is_staff(interaction.user):
        return await interaction.response.send_message("Only staff can use this.", ephemeral=True)
    try:
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        await interaction.response.send_message("✅ Commands refreshed.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Refresh failed: {e}", ephemeral=True)

@bot.tree.command(name="settings", description="Show modmail settings")
@guild_only_app()
async def settings_cmd(interaction: discord.Interaction):
    if not is_staff(interaction.user):
        return await interaction.response.send_message("Only staff can use this.", ephemeral=True)
    s = load_settings()
    def rf(rid): return f"<@&{rid}>" if rid else "Not set"
    def cf(cid): return f"<#{cid}>" if cid else "Not set"
    desc = (
        f"**Staff Role:** {rf(int(s.get('staff_role') or 0))}\n"
        f"**Log Channel:** {cf(int(s.get('log_channel') or 0))}\n"
        f"**Ticket Category:** {int(s.get('ticket_category') or 0) or 'Auto/Tickets'}\n"
        f"**Cooldown:** {int(s.get('cooldown') or 0)} seconds\n"
        f"**Active Tickets:** {len(s.get('active_tickets', {}))}\n"
        f"**Guild Lock:** {GUILD_ID}"
    )
    embed = discord.Embed(title="⚙️ ModMail Settings", description=desc, color=discord.Color.blurple())
    embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="set_staff_role", description="Set the staff role")
@guild_only_app()
@app_commands.describe(role="Role to be used as staff")
async def set_staff_role_cmd(interaction: discord.Interaction, role: discord.Role):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("You need Manage Server permission.", ephemeral=True)
    settings["staff_role"] = role.id
    save_settings(settings)
    await interaction.response.send_message(f"✅ Staff role set to {role.mention}", ephemeral=True)

@bot.tree.command(name="set_log_channel", description="Set log channel for transcripts")
@guild_only_app()
@app_commands.describe(channel="Text channel for logs")
async def set_log_channel_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("You need Manage Server permission.", ephemeral=True)
    settings["log_channel"] = channel.id
    save_settings(settings)
    await interaction.response.send_message(f"✅ Log channel set to {channel.mention}", ephemeral=True)

@bot.tree.command(name="set_category", description="Set ticket category")
@guild_only_app()
@app_commands.describe(category="Category for tickets")
async def set_category_cmd(interaction: discord.Interaction, category: discord.CategoryChannel):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("You need Manage Server permission.", ephemeral=True)
    settings["ticket_category"] = category.id
    save_settings(settings)
    await interaction.response.send_message(f"✅ Ticket category set to {category.name}", ephemeral=True)

@bot.tree.command(name="set_cooldown", description="Set cooldown (seconds) between opening tickets")
@guild_only_app()
@app_commands.describe(seconds="0 to disable")
async def set_cooldown_cmd(interaction: discord.Interaction, seconds: int):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("You need Manage Server permission.", ephemeral=True)
    if seconds < 0: seconds = 0
    settings["cooldown"] = seconds
    save_settings(settings)
    await interaction.response.send_message(f"✅ Cooldown set to {seconds} seconds", ephemeral=True)

@bot.tree.command(name="open", description="Open a ticket (creates a private ticket channel for you)")
@guild_only_app()
async def open_cmd(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    s = load_settings()
    active = s.get("active_tickets", {})
    if uid in active:
        ch = interaction.guild.get_channel(active[uid])
        if ch:
            return await interaction.response.send_message(f"❌ You already have a ticket: {ch.mention}", ephemeral=True)
        else:
            active.pop(uid, None)
            s["active_tickets"] = active
            save_settings(s)
    ch = await create_ticket_channel(interaction.guild, interaction.user)
    s["last_open"][uid] = datetime.datetime.utcnow().isoformat()
    save_settings(s)
    await interaction.response.send_message(f"✅ Ticket created: {ch.mention}", ephemeral=True)

@bot.tree.command(name="close_ticket", description="Close this ticket (staff only)")
@guild_only_app()
async def close_ticket_cmd(interaction: discord.Interaction):
    if not is_staff(interaction.user):
        return await interaction.response.send_message("Only staff can close tickets.", ephemeral=True)
    if not interaction.channel or not interaction.channel.name.startswith("ticket-"):
        return await interaction.response.send_message("This is not a ticket channel.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    await close_and_archive(interaction.channel, interaction.user)
    await interaction.followup.send("✅ Ticket closed and archived.", ephemeral=True)

# ----------------- RUN -----------------
if not BOT_TOKEN or not GUILD_ID:
    raise SystemExit("Please set DISCORD_BOT_TOKEN and GUILD_ID in your .env")

bot.run(BOT_TOKEN)
