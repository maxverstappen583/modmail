import discord
from discord.ext import commands
from discord import app_commands, ui
import asyncio, datetime, json, os, tempfile
from typing import Optional, Tuple
from flask import Flask
from threading import Thread

# ============ BASIC CONFIG ============
OWNER_ID = 1319292111325106296  # You (still allowed everywhere)
TOKEN = os.getenv("DISCORD_BOT_TOKEN")  # set in your hosting env
CONFIG_FILE = "config.json"

DM_COUNTDOWN_SECONDS = 15         # live countdown in DM
MODMAIL_COOLDOWN = 300            # 5 minutes (applies to /modmail and panel clicks)

# ============ FLASK KEEP-ALIVE ============
app = Flask(__name__)
@app.route("/")
def home():
    return "ModMail bot is alive"
Thread(target=lambda: app.run(host="0.0.0.0", port=10000), daemon=True).start()

# ============ STORAGE ============
def _default_guild_cfg():
    return {"category_id": None, "staff_role_id": None, "log_channel_id": None, "tickets": {}}

def load_cfg():
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f:
            json.dump({"guilds": {}}, f, indent=4)
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_cfg(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

cfg = load_cfg()  # { "guilds": { str(guild_id): {category_id, staff_role_id, log_channel_id, tickets{user_id: channel_id}} } }

def gcfg(guild: discord.Guild) -> dict:
    gid = str(guild.id)
    if "guilds" not in cfg: cfg["guilds"] = {}
    if gid not in cfg["guilds"]:
        cfg["guilds"][gid] = _default_guild_cfg()
        save_cfg(cfg)
    return cfg["guilds"][gid]

# ============ BOT ============
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree
_last_open = {}  # cooldown: {(guild_id, user_id): timestamp}

# ============ HELPERS ============
def is_owner(u: discord.abc.Snowflake) -> bool:
    return getattr(u, "id", None) == OWNER_ID

def is_admin_or_owner(inter: discord.Interaction) -> bool:
    if is_owner(inter.user): return True
    m = inter.user if isinstance(inter.user, discord.Member) else None
    return bool(m and m.guild_permissions.administrator)

def staff_role(guild: discord.Guild) -> Optional[discord.Role]:
    rid = gcfg(guild).get("staff_role_id")
    return guild.get_role(rid) if rid else None

def log_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    lid = gcfg(guild).get("log_channel_id")
    return guild.get_channel(lid) if lid else None

def category(guild: discord.Guild) -> Optional[discord.CategoryChannel]:
    cid = gcfg(guild).get("category_id")
    ch = guild.get_channel(cid) if cid else None
    return ch if isinstance(ch, discord.CategoryChannel) else None

def has_staff(member: discord.Member) -> bool:
    sr = staff_role(member.guild)
    return bool(sr and sr in member.roles) or member.guild_permissions.administrator or member.id == OWNER_ID

def in_cooldown(guild_id: int, user_id: int) -> int:
    key = (guild_id, user_id)
    now = asyncio.get_event_loop().time()
    last = _last_open.get(key, 0)
    left = int(MODMAIL_COOLDOWN - (now - last))
    return max(0, left)

def start_cooldown(guild_id: int, user_id: int):
    _last_open[(guild_id, user_id)] = asyncio.get_event_loop().time()

def user_embed(user: discord.User, content: str) -> discord.Embed:
    e = discord.Embed(description=content or "‎", color=discord.Color.blurple(), timestamp=datetime.datetime.utcnow())
    e.set_author(name=str(user), icon_url=user.display_avatar.url if hasattr(user, "display_avatar") else None)
    return e

def staff_embed(author: discord.Member, content: str) -> discord.Embed:
    e = discord.Embed(description=content or "‎", color=discord.Color.orange(), timestamp=datetime.datetime.utcnow())
    e.set_author(name=str(author), icon_url=author.display_avatar.url if hasattr(author, "display_avatar") else None)
    return e

async def send_log(guild: discord.Guild, embed: discord.Embed, file_path: Optional[str] = None):
    ch = log_channel(guild)
    if not ch: return
    try:
        if file_path:
            await ch.send(embed=embed, file=discord.File(file_path))
        else:
            await ch.send(embed=embed)
    except Exception as e:
        print("Log send failed:", e)

async def build_transcript(channel: discord.TextChannel) -> str:
    lines = []
    async for m in channel.history(limit=None, oldest_first=True):
        ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
        who = f"{m.author} ({m.author.id})"
        lines.append(f"[{ts}] {who}: {m.content}")
        for a in m.attachments:
            lines.append(f"    [attachment] {a.url}")
    fd, path = tempfile.mkstemp(prefix=f"transcript_{channel.id}_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path

async def pick_target_guild_for_dm(user: discord.User) -> Optional[discord.Guild]:
    """Pick the first mutual guild where bot has a configured category & staff role."""
    for g in bot.guilds:
        gc = gcfg(g)
        if gc.get("category_id") and gc.get("staff_role_id"):
            return g
    return None

async def create_ticket(guild: discord.Guild, user: discord.User, opened_via: str) -> Tuple[Optional[discord.TextChannel], Optional[str]]:
    gc = gcfg(guild)
    cat = category(guild)
    sr = staff_role(guild)
    if not cat or not sr:
        return None, "Ticket system not configured in this server."

    # already open?
    if str(user.id) in gc["tickets"]:
        ch = guild.get_channel(gc["tickets"][str(user.id)])
        return ch, "You already have an open ticket."

    # Channel that is NOT visible to user
    name = f"ticket-{user.name}".replace(" ", "-")[:90]
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        sr: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        # NOTE: user explicitly has NO view permission here
    }
    ch = await guild.create_text_channel(name=name, category=cat, overwrites=overwrites, topic=str(user.id))

    gc["tickets"][str(user.id)] = ch.id
    save_cfg(cfg)

    await ch.send(f"{sr.mention} 📬 New ticket opened by **{user}** via **{opened_via}** (user cannot view this channel).")
    emb = discord.Embed(title="Ticket Opened", color=discord.Color.green(), timestamp=datetime.datetime.utcnow())
    emb.add_field(name="User", value=f"{user} ({user.id})", inline=False)
    emb.add_field(name="Channel", value=ch.mention, inline=False)
    emb.add_field(name="Opened via", value=opened_via, inline=True)
    await send_log(guild, emb)
    return ch, None

async def close_ticket(guild: discord.Guild, channel: discord.TextChannel, closed_by: discord.Member):
    gc = gcfg(guild)
    uid = None
    if channel.topic and channel.topic.isdigit():
        uid = channel.topic
    if not uid:
        for k, v in gc["tickets"].items():
            if v == channel.id:
                uid = k
                break

    # transcript + log
    transcript = None
    try:
        transcript = await build_transcript(channel)
    except Exception as e:
        print("Transcript error:", e)

    e = discord.Embed(title="Ticket Closed", color=discord.Color.red(), timestamp=datetime.datetime.utcnow())
    e.add_field(name="Channel", value=channel.name, inline=True)
    e.add_field(name="Closed by", value=str(closed_by), inline=True)
    if uid:
        e.add_field(name="User ID", value=uid, inline=True)
        try:
            u = await bot.fetch_user(int(uid))
            try: await u.send("✅ Your ticket has been closed by staff.")
            except: pass
        except: pass
    await send_log(guild, e, file_path=transcript)

    if uid: gc["tickets"].pop(str(uid), None); save_cfg(cfg)
    try: await channel.delete()
    except: pass
    if transcript:
        try: os.remove(transcript)
        except: pass

# ============ UI (Panel) ============
class PanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="🎟 Create Ticket", style=discord.ButtonStyle.green, custom_id="panel_create_ticket")
    async def create_ticket_btn(self, interaction: discord.Interaction, button: ui.Button):
        # cooldown check
        left = in_cooldown(interaction.guild.id, interaction.user.id)
        if left > 0:
            return await interaction.response.send_message(f"⏳ Please wait **{left}s** before opening another ticket.", ephemeral=True)

        await interaction.response.send_message("📩 Check your DMs — starting ModMail.", ephemeral=True)
        try:
            await start_dm_countdown_then_open(interaction.user, interaction.guild, source="panel")
            start_cooldown(interaction.guild.id, interaction.user.id)
        except discord.Forbidden:
            await interaction.followup.send("❗ I can't DM you. Please enable DMs and try again.", ephemeral=True)

# ============ COUNTDOWN DM FLOW ============
async def start_dm_countdown_then_open(user: discord.User, guild: discord.Guild, source: str, first_message: Optional[discord.Message] = None):
    """Show a single DM embed that live-updates from 15->0, then auto-create the ticket."""
    title = "Creating Your Support Ticket"
    desc = "Your ticket will be opened automatically once the countdown finishes."

    # send initial
    seconds = DM_COUNTDOWN_SECONDS
    embed = discord.Embed(title=title, description=f"{desc}\n\n**Starting in:** `{seconds}s`", color=discord.Color.green())
    embed.set_footer(text=f"Server: {guild.name}")
    msg = await user.send(embed=embed)

    # live update every second
    for s in range(seconds - 1, -1, -1):
        await asyncio.sleep(1)
        embed.description = f"{desc}\n\n**Opening in:** `{s}s`"
        try:
            await msg.edit(embed=embed)
        except:
            pass

    # create ticket and forward first message, if any
    ch, err = await create_ticket(guild, user, opened_via=source)
    if err:
        try: await user.send(f"⚠️ {err}")
        except: pass
        return

    try:
        await user.send("✅ Ticket created. Please reply here — staff will see your messages.")
    except: pass

    # forward the first message (for DM flow) if provided
    if first_message:
        files = []
        for a in first_message.attachments:
            try: files.append(await a.to_file())
            except: pass
        emb = user_embed(user, first_message.content or "")
        try:
            if files: await ch.send(embed=emb, files=files)
            else:     await ch.send(embed=emb)
        except: pass

# ============ COMMANDS ============
@bot.event
async def on_ready():
    try:
        await tree.sync()  # global sync (works across servers)
    except Exception as e:
        print("Sync error:", e)
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")

# --- Setup commands (Admin or Owner) ---
@tree.command(name="set_category", description="Set the ticket category (Admin/Owner)")
async def set_category_cmd(inter: discord.Interaction, category_chan: discord.CategoryChannel):
    if not is_admin_or_owner(inter):
        return await inter.response.send_message("❌ Admin only.", ephemeral=True)
    gcfg(inter.guild)["category_id"] = category_chan.id
    save_cfg(cfg)
    await inter.response.send_message(f"✅ Category set to **{category_chan.name}**", ephemeral=True)

@tree.command(name="set_staff_role", description="Set the staff role (Admin/Owner)")
async def set_staff_role_cmd(inter: discord.Interaction, role: discord.Role):
    if not is_admin_or_owner(inter):
        return await inter.response.send_message("❌ Admin only.", ephemeral=True)
    gcfg(inter.guild)["staff_role_id"] = role.id
    save_cfg(cfg)
    await inter.response.send_message(f"✅ Staff role set to {role.mention}", ephemeral=True)

@tree.command(name="set_log_channel", description="Set the log channel (Admin/Owner)")
async def set_log_channel_cmd(inter: discord.Interaction, channel: discord.TextChannel):
    if not is_admin_or_owner(inter):
        return await inter.response.send_message("❌ Admin only.", ephemeral=True)
    gcfg(inter.guild)["log_channel_id"] = channel.id
    save_cfg(cfg)
    await inter.response.send_message(f"✅ Log channel set to {channel.mention}", ephemeral=True)

@tree.command(name="settings", description="View current ModMail settings")
async def settings_cmd(inter: discord.Interaction):
    gc = gcfg(inter.guild)
    cat = inter.guild.get_channel(gc.get("category_id")) if gc.get("category_id") else None
    sr = inter.guild.get_role(gc.get("staff_role_id")) if gc.get("staff_role_id") else None
    log = inter.guild.get_channel(gc.get("log_channel_id")) if gc.get("log_channel_id") else None
    e = discord.Embed(title="ModMail Settings", color=discord.Color.blurple())
    e.add_field(name="Category", value=cat.name if cat else "❌ Not set", inline=False)
    e.add_field(name="Staff Role", value=sr.mention if sr else "❌ Not set", inline=False)
    e.add_field(name="Log Channel", value=log.mention if log else "❌ Not set", inline=False)
    await inter.response.send_message(embed=e, ephemeral=True)

@tree.command(name="send_panel", description="Send a Create Ticket panel (Staff/Admin/Owner)")
async def send_panel_cmd(inter: discord.Interaction):
    if not has_staff(inter.user):
        return await inter.response.send_message("❌ Staff/Admin only.", ephemeral=True)
    if not category(inter.guild) or not staff_role(inter.guild):
        return await inter.response.send_message("⚠️ Configure category & staff role first.", ephemeral=True)
    e = discord.Embed(
        title="📩 Support Panel",
        description="Need help? Click the button below to start a private ModMail ticket.\nYou will chat **via DM** only.",
        color=discord.Color.green()
    )
    await inter.channel.send(embed=e, view=PanelView())
    await inter.response.send_message("✅ Panel sent.", ephemeral=True)

@tree.command(name="modmail", description="Open a ModMail ticket (DM-only, live countdown)")
async def modmail_cmd(inter: discord.Interaction):
    if not category(inter.guild) or not staff_role(inter.guild):
        return await inter.response.send_message("⚠️ Ticket system not configured.", ephemeral=True)

    left = in_cooldown(inter.guild.id, inter.user.id)
    if left > 0:
        return await inter.response.send_message(f"⏳ Please wait **{left}s** before opening another ticket.", ephemeral=True)

    await inter.response.send_message("📩 Check your DMs — starting ModMail.", ephemeral=True)
    try:
        await start_dm_countdown_then_open(inter.user, inter.guild, source="slash")
        start_cooldown(inter.guild.id, inter.user.id)
    except discord.Forbidden:
        await inter.followup.send("❗ I can't DM you. Please enable DMs and try again.", ephemeral=True)

@tree.command(name="close", description="Close this ticket (Staff/Admin/Owner)")
async def close_cmd(inter: discord.Interaction):
    if not has_staff(inter.user):
        return await inter.response.send_message("❌ Staff/Admin only.", ephemeral=True)

    gc = gcfg(inter.guild)
    if str(inter.channel.id) not in map(str, gc["tickets"].values()):
        return await inter.response.send_message("❌ This is not a ModMail ticket channel.", ephemeral=True)

    await inter.response.send_message("🗂 Closing ticket and saving transcript…", ephemeral=True)
    await close_ticket(inter.guild, inter.channel, inter.user)

@tree.command(name="refresh", description="Refresh slash commands (Admin/Owner)")
async def refresh_cmd(inter: discord.Interaction):
    if not is_admin_or_owner(inter):
        return await inter.response.send_message("❌ Admin only.", ephemeral=True)
    await inter.response.defer(ephemeral=True)
    try:
        await tree.sync()
        await inter.followup.send("✅ Commands refreshed.", ephemeral=True)
    except Exception as e:
        await inter.followup.send(f"⚠️ Sync failed: {e}", ephemeral=True)

# ============ MESSAGE FORWARDING ============
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # 1) DMs -> forward to a configured server ticket (or start countdown)
    if isinstance(message.channel, discord.DMChannel):
        # If user already has an open ticket in any configured guild, forward there.
        target_guild = None
        target_channel = None
        for g in bot.guilds:
            gc = gcfg(g)
            if str(message.author.id) in gc["tickets"]:
                ch = g.get_channel(gc["tickets"][str(message.author.id)])
                if ch:
                    target_guild = g; target_channel = ch; break

        if target_channel:
            emb = user_embed(message.author, message.content or "")
            files = []
            for a in message.attachments:
                try: files.append(await a.to_file())
                except: pass
            try:
                if files: await target_channel.send(embed=emb, files=files)
                else:     await target_channel.send(embed=emb)
            except: pass
            return

        # No open ticket -> pick a configured guild and start countdown, then create
        g = await pick_target_guild_for_dm(message.author)
        if not g:
            try: await message.author.send("⚠️ No server is configured for ModMail yet. Please try again later.")
            except: pass
            return

        # Cooldown per (guild, user)
        left = in_cooldown(g.id, message.author.id)
        if left > 0:
            try: await message.author.send(f"⏳ Please wait **{left}s** before opening another ticket.")
            except: pass
            return

        # Live countdown; auto-creates and forwards THIS first message
        try:
            await start_dm_countdown_then_open(message.author, g, source="DM", first_message=message)
            start_cooldown(g.id, message.author.id)
        except discord.Forbidden:
            # Can't DM (shouldn't happen since we're already in DM), ignore
            pass

        return

    # 2) Guild messages in ticket channels by staff -> forward to user's DM
    if message.guild:
        gc = gcfg(message.guild)
        # if message channel is a ticket channel we manage
        if str(message.channel.id) in map(str, gc["tickets"].values()):
            # Only forward staff/admin/owner responses to user
            if not has_staff(message.author):
                return

            # find user id
            uid = None
            for k, v in gc["tickets"].items():
                if v == message.channel.id:
                    uid = k; break
            if not uid: return

            try:
                user = await bot.fetch_user(int(uid))
            except:
                return

            emb = staff_embed(message.author, message.content or "")
            files = []
            for a in message.attachments:
                try: files.append(await a.to_file())
                except: pass
            try:
                if files: await user.send(embed=emb, files=files)
                else:     await user.send(embed=emb)
            except:  # DM closed
                try:
                    await message.channel.send("⚠️ Could not DM the user (DMs are closed).")
                except: pass

    await bot.process_commands(message)

# ============ RUN ============
if not TOKEN:
    print("ERROR: set DISCORD_BOT_TOKEN env var")
else:
    bot.run(TOKEN)
