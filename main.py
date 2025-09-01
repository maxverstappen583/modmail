# main.py
# Full ModMail bot (single file)
# - Guild-locked slash commands (instant sync)
# - /send_panel, /refresh, /settings, /set_staff_role, /set_log_channel, /set_category, /set_cooldown, /close
# - Panel (red embed) with "Open Ticket" button
# - One ticket per user
# - DM <-> ticket sync (text + attachments)
# - Staff buttons: Problem Solved ✅, Not Solved ❎, Close Ticket 🔒
# - Transcript (.txt) uploaded to log channel and DM'd to user
# - Footer text + icon on all embeds
# - Optional OpenAI summarization if OPENAI_API_KEY set
# - Flask keep-alive for host services

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

# ========== Load env ==========
load_dotenv()
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
OTHER_GUILD_RESPONSE = os.getenv("OTHER_GUILD_RESPONSE", "Sorry, this bot only works in the official server.")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")  # optional

if OPENAI_API_KEY:
    try:
        import openai
        openai.api_key = OPENAI_API_KEY
        USE_OPENAI = True
    except Exception:
        USE_OPENAI = False
else:
    USE_OPENAI = False

FOOTER_TEXT = "@u4_straight1"
FOOTER_ICON = "https://i.postimg.cc/rp5b7Jkn/IMG-6152.jpg"

# Require valid env
if not BOT_TOKEN or GUILD_ID == 0:
    raise SystemExit("Please set DISCORD_BOT_TOKEN and GUILD_ID in your .env before running.")

# ========== Flask keep-alive ==========
app = Flask(__name__)
@app.route("/")
def home():
    return "ModMail bot is running."

def run_flask():
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)

Thread(target=run_flask, daemon=True).start()

# ========== Settings persistence ==========
SETTINGS_FILE = "modmail_settings.json"
DEFAULT_SETTINGS = {
    "staff_role": 0,
    "log_channel": 0,
    "ticket_category": 0,
    "cooldown": 60,
    "active_tickets": {},   # "user_id" -> channel_id
    "last_open": {}         # "user_id" -> iso timestamp
}

def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_SETTINGS, f, indent=2)
    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    # ensure defaults present
    updated = False
    for k, v in DEFAULT_SETTINGS.items():
        if k not in data:
            data[k] = v
            updated = True
    if updated:
        save_settings(data)
    return data

def save_settings(data):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

settings = load_settings()

# ========== Bot setup ==========
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

# ========== Helpers ==========
def guild_only_app():
    """Decorator: slash commands only respond in configured guild. If used elsewhere, send custom message."""
    async def check(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            return True  # allow DMs for commands if needed (we use guild commands mainly)
        if interaction.guild.id != GUILD_ID:
            # reply with custom message
            try:
                await interaction.response.send_message(OTHER_GUILD_RESPONSE, ephemeral=True)
            except Exception:
                # fallback if response already used
                pass
            return False
        return True
    return app_commands.check(check)

def is_staff_member(member: discord.Member) -> bool:
    """Return True if member has staff role set OR has manage_guild/admin perms."""
    try:
        sr = int(settings.get("staff_role", 0) or 0)
    except Exception:
        sr = 0
    if sr and member and isinstance(member, discord.Member):
        if any(r.id == sr for r in member.roles):
            return True
    if member and (member.guild_permissions.manage_guild or member.guild_permissions.administrator):
        return True
    return False

def top_role_color(member: discord.Member):
    if not member:
        return discord.Color.greyple()
    for role in reversed(member.roles):
        if role.color.value != 0:
            return role.color
    return discord.Color.greyple()

def create_user_embed(user: discord.abc.User, content: str, member_obj=None, attachments=None):
    color = top_role_color(member_obj) if member_obj else discord.Color.greyple()
    embed = discord.Embed(description=content if content else "\u200b", color=color)
    embed.set_author(name=str(user), icon_url=user.display_avatar.url)
    embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON)
    # set first image preview if present
    if attachments:
        for a in attachments:
            if a.content_type and a.content_type.startswith("image/"):
                embed.set_image(url=a.url)
                break
    return embed

async def ensure_category(guild: discord.Guild):
    """Return configured category or create 'Tickets'."""
    cat_id = int(settings.get("ticket_category", 0) or 0)
    if cat_id:
        cat = discord.utils.get(guild.categories, id=cat_id)
        if cat:
            return cat
    # find by name
    cat = discord.utils.get(guild.categories, name="Tickets")
    if cat:
        return cat
    return await guild.create_category("Tickets")

async def create_ticket_channel_for_user(guild: discord.Guild, user: discord.User):
    member = guild.get_member(user.id)
    if not member:
        # user not in guild -> don't create
        try:
            await user.send("You must be a member of the server to open a ticket.")
        except Exception:
            pass
        return None

    cat = await ensure_category(guild)
    staff_role_id = int(settings.get("staff_role", 0) or 0)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, read_message_history=True),
        member: discord.PermissionOverwrite(read_messages=True, send_messages=True, read_message_history=True)
    }
    if staff_role_id:
        staff_role = guild.get_role(staff_role_id)
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True, read_message_history=True)

    channel = await guild.create_text_channel(name=f"ticket-{user.id}", category=cat, overwrites=overwrites)
    # store mapping
    settings["active_tickets"][str(user.id)] = channel.id
    save_settings(settings)

    # ping staff
    ping_text = (guild.get_role(staff_role_id).mention) if staff_role_id and guild.get_role(staff_role_id) else "@here"
    intro = discord.Embed(
        title="🎫 New Ticket",
        description=f"{user.mention} opened a ticket. Staff please assist.",
        color=discord.Color.red()
    )
    intro.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON)
    await channel.send(content=ping_text, embed=intro, view=TicketControlsView(opener_id=user.id))
    return channel

async def summarize_user_messages(channel: discord.TextChannel, user_id: int):
    # collect some recent messages from that user inside channel
    collected = []
    async for m in channel.history(limit=300, oldest_first=True):
        if m.author.id == user_id and (m.content or m.attachments):
            text = m.content or ""
            if m.attachments:
                text += " " + " ".join(a.filename for a in m.attachments)
            collected.append(text)
    text = "\n".join(collected[-30:])
    if not text:
        return "No clear problem detected."

    if USE_OPENAI:
        try:
            resp = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[{"role":"user","content": f"Summarize the user's issue in one sentence:\n\n{text}"}],
                max_tokens=80
            )
            return resp.choices[0].message.content.strip()
        except Exception:
            pass
    # fallback truncation
    return (text[:200] + "…") if len(text) > 200 else text

async def make_transcript_file(channel: discord.TextChannel):
    lines = []
    async for m in channel.history(limit=None, oldest_first=True):
        ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
        author = f"{m.author} ({m.author.id})"
        content = m.content or ""
        # escape newlines so transcript single-line per message
        content = content.replace("\n", "\\n")
        lines.append(f"[{ts}] {author}: {content}")
        if m.attachments:
            for a in m.attachments:
                lines.append(f"    [attachment] {a.filename} -> {a.url}")
    txt = "\n".join(lines) if lines else "No messages."
    fname = f"transcript-{channel.name}-{int(channel.id)}.txt"
    with open(fname, "w", encoding="utf-8") as f:
        f.write(txt)
    return fname

async def close_ticket_flow(channel: discord.TextChannel, closed_by: discord.abc.User):
    # find opener id
    opener_id = None
    for uid, cid in list(settings.get("active_tickets", {}).items()):
        if cid == channel.id:
            opener_id = int(uid)
            break

    # make transcript
    fname = await make_transcript_file(channel)
    file = discord.File(fname)

    # send to log channel
    lc_id = int(settings.get("log_channel", 0) or 0)
    if lc_id:
        log_ch = channel.guild.get_channel(lc_id)
        if log_ch:
            embed = discord.Embed(
                title="📑 Ticket Transcript",
                description=f"Transcript for {channel.name}\nClosed by: {getattr(closed_by,'mention',str(closed_by))}",
                color=discord.Color.red()
            )
            embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON)
            try:
                await log_ch.send(embed=embed, file=file)
            except Exception:
                pass

    # DM transcript to user
    if opener_id:
        try:
            user = await bot.fetch_user(opener_id)
            if user:
                await user.send("📑 Here is your ticket transcript:", file=discord.File(fname))
        except Exception:
            pass

    # cleanup mapping and file
    if opener_id:
        settings["active_tickets"].pop(str(opener_id), None)
        save_settings(settings)
    try:
        os.remove(fname)
    except Exception:
        pass

    # finally delete channel
    try:
        await channel.delete()
    except Exception:
        pass

# ========== Views (buttons) ==========
class PanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Open Ticket", style=ButtonStyle.danger, custom_id="panel_open_ticket")
    async def open_ticket_button(self, interaction: discord.Interaction, button: ui.Button):
        # only allow inside configured guild
        if not interaction.guild or interaction.guild.id != GUILD_ID:
            return await interaction.response.send_message(OTHER_GUILD_RESPONSE, ephemeral=True)

        uid = str(interaction.user.id)
        # cooldown & one-ticket checks
        active = settings.get("active_tickets", {})
        last_open = settings.get("last_open", {})
        if uid in active:
            ch = interaction.guild.get_channel(active[uid])
            if ch:
                return await interaction.response.send_message(f"You already have a ticket: {ch.mention}", ephemeral=True)
            else:
                # stale mapping
                active.pop(uid, None)
                settings["active_tickets"] = active
                save_settings(settings)

        cd = int(settings.get("cooldown", 0) or 0)
        if cd and uid in last_open:
            try:
                last_ts = datetime.datetime.fromisoformat(last_open[uid])
                delta = (datetime.datetime.utcnow() - last_ts).total_seconds()
                if delta < cd:
                    return await interaction.response.send_message(f"Please wait {int(cd-delta)}s before opening another ticket.", ephemeral=True)
            except Exception:
                pass

        ch = await create_ticket_channel_for_user(interaction.guild, interaction.user)
        if ch:
            settings["last_open"][uid] = datetime.datetime.utcnow().isoformat()
            save_settings(settings)
            await interaction.response.send_message(f"Ticket created: {ch.mention}", ephemeral=True)
        else:
            await interaction.response.send_message("Unable to create ticket. Make sure you are a member of the server.", ephemeral=True)

class TicketControlsView(ui.View):
    def __init__(self, opener_id: int):
        super().__init__(timeout=None)
        self.opener_id = opener_id

    @ui.button(label="Problem Solved ✅", style=ButtonStyle.success, custom_id="ticket_solved")
    async def solved(self, interaction: discord.Interaction, button: ui.Button):
        # only staff
        if not is_staff_member(interaction.user):
            return await interaction.response.send_message("Only staff can use this.", ephemeral=True)
        # lock user from sending
        opener_member = interaction.guild.get_member(self.opener_id)
        if opener_member:
            overwrites = interaction.channel.overwrites
            overwrites[opener_member] = discord.PermissionOverwrite(read_messages=True, send_messages=False, read_message_history=True)
            await interaction.channel.edit(overwrites=overwrites)
        # summarize and present close view
        summary = await summarize_user_messages(interaction.channel, self.opener_id)
        embed = discord.Embed(
            title="Ticket marked as solved",
            description=f"**Detected problem:**\n{summary}",
            color=discord.Color.red()
        )
        embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON)
        await interaction.response.send_message(embed=embed, view=CloseView(self.opener_id), ephemeral=False)

    @ui.button(label="Not Solved ❎", style=ButtonStyle.secondary, custom_id="ticket_not_solved")
    async def not_solved(self, interaction: discord.Interaction, button: ui.Button):
        if not is_staff_member(interaction.user):
            return await interaction.response.send_message("Only staff can use this.", ephemeral=True)
        opener_member = interaction.guild.get_member(self.opener_id)
        if opener_member:
            overwrites = interaction.channel.overwrites
            overwrites[opener_member] = discord.PermissionOverwrite(read_messages=True, send_messages=True, read_message_history=True)
            await interaction.channel.edit(overwrites=overwrites)
        await interaction.response.send_message("Ticket re-opened for the user to reply.", ephemeral=True)

class CloseView(ui.View):
    def __init__(self, opener_id: int):
        super().__init__(timeout=None)
        self.opener_id = opener_id

    @ui.button(label="Close Ticket 🔒", style=ButtonStyle.danger, custom_id="ticket_close")
    async def close_ticket(self, interaction: discord.Interaction, button: ui.Button):
        if not is_staff_member(interaction.user):
            return await interaction.response.send_message("Only staff can close tickets.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await close_ticket_flow(interaction.channel, interaction.user)

# ========== Events ==========
@bot.event
async def on_ready():
    print(f"Bot online as {bot.user} (id: {bot.user.id})")
    # sync slash commands to configured guild (instant)
    try:
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        print(f"Slash commands synced to guild {GUILD_ID}")
    except Exception as e:
        print("Failed to sync slash commands:", e)
    # persist views (panel)
    bot.add_view(PanelView())
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="ModMail"))

@bot.event
async def on_typing(channel, user, when):
    # If user is typing in DM and they have a ticket, show typing in staff channel
    if user.bot:
        return
    if isinstance(channel, discord.DMChannel):
        uid = str(user.id)
        active = settings.get("active_tickets", {})
        if uid in active:
            guild = bot.get_guild(GUILD_ID)
            ch_id = active[uid]
            ch = guild.get_channel(ch_id) if guild else None
            if ch:
                async with ch.typing():
                    await asyncio.sleep(2)

@bot.event
async def on_message(message: discord.Message):
    # Do not process messages from other guilds
    if message.guild and message.guild.id != GUILD_ID:
        return

    # ignore bots
    if message.author.bot:
        return

    active = settings.get("active_tickets", {})
    guild = bot.get_guild(GUILD_ID)

    # User sends DM -> forward to/create ticket channel
    if isinstance(message.channel, discord.DMChannel):
        uid = str(message.author.id)
        member = guild.get_member(message.author.id) if guild else None
        if not member:
            # If not in guild, tell user they need to be a member
            try:
                await message.author.send("You must be a member of the server to open a ticket.")
            except Exception:
                pass
            return

        # create or get ticket channel
        if uid in active:
            ch = guild.get_channel(active[uid])
            if ch is None:
                ch = await create_ticket_channel_for_user(guild, message.author)
        else:
            ch = await create_ticket_channel_for_user(guild, message.author)

        if not ch:
            return

        files = [await a.to_file() for a in message.attachments] if message.attachments else None
        embed = create_user_embed(message.author, message.content, member_obj=member, attachments=message.attachments)
        if files:
            await ch.send(embed=embed, files=files)
        else:
            await ch.send(embed=embed)

    # Staff message inside ticket channel -> forward to user DM
    elif message.channel and isinstance(message.channel, discord.TextChannel) and message.channel.name.startswith("ticket-"):
        # find opener
        opener_id = None
        for uid, cid in list(settings.get("active_tickets", {}).items()):
            if cid == message.channel.id:
                opener_id = int(uid)
                break
        if not opener_id:
            return  # not found
        try:
            user = await bot.fetch_user(opener_id)
        except Exception:
            user = bot.get_user(opener_id)
        if not user:
            return

        # send content and attachments
        content = (f"**Staff:** {message.content}") if message.content else ""
        files = [await a.to_file() for a in message.attachments] if message.attachments else None
        try:
            if files:
                await user.send(content or "\u200b", files=files)
            else:
                # if empty content but attachments none, at least send newline to avoid empty send
                await user.send(content or "\u200b")
        except Exception:
            # can't DM user, inform staff in channel
            await message.channel.send("⚠️ Could not DM the user. They may have DMs closed.")

    # process commands if any prefix commands used (we rely on slash commands primarily)
    await bot.process_commands(message)

# ========== Slash commands ==========
@bot.tree.command(name="send_panel", description="Send the ModMail panel to this channel")
@guild_only_app()
async def send_panel(interaction: discord.Interaction):
    if not is_staff_member(interaction.user):
        return await interaction.response.send_message("Only staff can use this.", ephemeral=True)
    embed = discord.Embed(title="📮 Need Help?", description="Press **Open Ticket** to contact staff privately.", color=discord.Color.red())
    embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON)
    await interaction.response.send_message(embed=embed, view=PanelView())

@bot.tree.command(name="refresh", description="Refresh (resync) slash commands")
@guild_only_app()
async def refresh(interaction: discord.Interaction):
    if not is_staff_member(interaction.user):
        return await interaction.response.send_message("Only staff can use this.", ephemeral=True)
    try:
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        await interaction.response.send_message("✅ Commands refreshed.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Failed to refresh: {e}", ephemeral=True)

@bot.tree.command(name="settings", description="Show modmail settings")
@guild_only_app()
async def settings_cmd(interaction: discord.Interaction):
    if not is_staff_member(interaction.user):
        return await interaction.response.send_message("Only staff can use this.", ephemeral=True)
    s = load_settings()
    staff_role = f"<@&{int(s.get('staff_role') or 0)}>" if int(s.get('staff_role') or 0) else "Not set"
    log_channel = f"<#{int(s.get('log_channel') or 0)}>" if int(s.get('log_channel') or 0) else "Not set"
    ticket_cat = f"{int(s.get('ticket_category') or 0) or 'Auto/\"Tickets\"'}"
    cooldown = int(s.get('cooldown') or 0)
    active_count = len(s.get("active_tickets", {}))
    desc = (
        f"**Staff role:** {staff_role}\n"
        f"**Log channel:** {log_channel}\n"
        f"**Ticket category:** {ticket_cat}\n"
        f"**Cooldown:** {cooldown}s\n"
        f"**Active tickets:** {active_count}\n"
        f"**Guild ID:** {GUILD_ID}"
    )
    embed = discord.Embed(title="⚙️ ModMail Settings", description=desc, color=discord.Color.blurple())
    embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="set_staff_role", description="Set staff role (admins only)")
@guild_only_app()
@app_commands.describe(role="Role to assign as staff")
async def set_staff_role(interaction: discord.Interaction, role: discord.Role):
    if not (interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator):
        return await interaction.response.send_message("You need Manage Server permission.", ephemeral=True)
    settings["staff_role"] = role.id
    save_settings(settings)
    await interaction.response.send_message(f"✅ Staff role set to {role.mention}", ephemeral=True)

@bot.tree.command(name="set_log_channel", description="Set channel to receive transcripts")
@guild_only_app()
@app_commands.describe(channel="Text channel to send transcripts to")
async def set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not (interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator):
        return await interaction.response.send_message("You need Manage Server permission.", ephemeral=True)
    settings["log_channel"] = channel.id
    save_settings(settings)
    await interaction.response.send_message(f"✅ Log channel set to {channel.mention}", ephemeral=True)

@bot.tree.command(name="set_category", description="Set ticket category")
@guild_only_app()
@app_commands.describe(category="Category where tickets are created")
async def set_category(interaction: discord.Interaction, category: discord.CategoryChannel):
    if not (interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator):
        return await interaction.response.send_message("You need Manage Server permission.", ephemeral=True)
    settings["ticket_category"] = category.id
    save_settings(settings)
    await interaction.response.send_message(f"✅ Ticket category set to **{category.name}**", ephemeral=True)

@bot.tree.command(name="set_cooldown", description="Set cooldown (seconds) between opens")
@guild_only_app()
@app_commands.describe(seconds="Seconds (0 to disable)")
async def set_cooldown(interaction: discord.Interaction, seconds: int):
    if not (interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator):
        return await interaction.response.send_message("You need Manage Server permission.", ephemeral=True)
    if seconds < 0: seconds = 0
    settings["cooldown"] = seconds
    save_settings(settings)
    await interaction.response.send_message(f"✅ Cooldown set to {seconds}s", ephemeral=True)

@bot.tree.command(name="close", description="Close this ticket and save transcript")
@guild_only_app()
async def close_cmd(interaction: discord.Interaction):
    if not is_staff_member(interaction.user):
        return await interaction.response.send_message("Only staff can close tickets.", ephemeral=True)
    if not interaction.channel or not isinstance(interaction.channel, discord.TextChannel) or not interaction.channel.name.startswith("ticket-"):
        return await interaction.response.send_message("This command must be used in a ticket channel.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    await close_ticket_flow(interaction.channel, interaction.user)

# ========== Start bot ==========
bot.run(BOT_TOKEN)
