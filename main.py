# main.py
import discord
from discord.ext import commands
from discord import app_commands, ui
import asyncio, json, os, tempfile, datetime
from flask import Flask
from threading import Thread
from typing import Optional, Tuple

# ---------------- CONFIG ----------------
OWNER_ID = 1319292111325106296                # your ID (owner)
TOKEN = os.getenv("DISCORD_BOT_TOKEN")        # set this in environment
CONFIG_FILE = "config.json"
FOOTER_IMAGE_SOURCE = "/mnt/data/DEBEC8AF-40C2-421C-8F41-B606AB6A6072.jpeg"  # developer-uploaded image path
FOOTER_ATTACHMENT_NAME = "footer.png"
FOOTER_TEXT = "@ u4_straight1"

DM_COUNTDOWN = 15         # seconds for live countdown
MODMAIL_COOLDOWN = 300    # per-guild cooldown seconds

# ---------------- FLASK KEEPALIVE ----------------
app = Flask("")
@app.route("/")
def home():
    return "ModMail bot alive"

def _run_flask():
    app.run(host="0.0.0.0", port=10000)
Thread(target=_run_flask, daemon=True).start()

# ---------------- CONFIG STORAGE ----------------
def default_guild_cfg():
    return {"category_id": None, "staff_role_id": None, "log_channel_id": None, "tickets": {}, "cooldown": MODMAIL_COOLDOWN}

if not os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, "w") as f:
        json.dump({"guilds": {}}, f, indent=4)

def load_cfg():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_cfg(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=4)

CFG = load_cfg()  # structure: { "guilds": { "<guild_id>": {category_id, staff_role_id, log_channel_id, tickets:{user:channel}, cooldown} } }

def ensure_guild_cfg(guild: discord.Guild):
    gid = str(guild.id)
    if "guilds" not in CFG:
        CFG["guilds"] = {}
    if gid not in CFG["guilds"]:
        CFG["guilds"][gid] = default_guild_cfg()
        save_cfg(CFG)
    return CFG["guilds"][gid]

def guild_cfg_by_id(guild_id: int):
    gid = str(guild_id)
    if "guilds" not in CFG: CFG["guilds"] = {}
    return CFG["guilds"].get(gid, default_guild_cfg())

# ---------------- BOT SETUP ----------------
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# track last open times per guild+user -> cooldown
_last_open = {}  # key: (guild_id, user_id) -> timestamp

def in_cooldown(guild_id: int, user_id: int) -> int:
    key = (guild_id, user_id)
    now = asyncio.get_event_loop().time()
    last = _last_open.get(key, 0)
    cooldown = guild_cfg_by_id(guild_id).get("cooldown", MODMAIL_COOLDOWN)
    remaining = int(cooldown - (now - last))
    return max(0, remaining)

def start_cooldown(guild_id: int, user_id: int):
    _last_open[(guild_id, user_id)] = asyncio.get_event_loop().time()

# ---------------- FOOTER HELP ----------------
def attach_footer_file(kwargs: dict):
    """If footer image exists, attach it and return a filename to reference in embed footer"""
    if os.path.exists(FOOTER_IMAGE_SOURCE):
        # copy to temp file as FOOTER_ATTACHMENT_NAME in working dir
        try:
            with open(FOOTER_IMAGE_SOURCE, "rb") as src:
                # use a temp filename to attach
                tmp_path = os.path.join(tempfile.gettempdir(), FOOTER_ATTACHMENT_NAME)
                with open(tmp_path, "wb") as dst:
                    dst.write(src.read())
            # pass the file in kwargs for sending (some senders expect file param)
            kwargs["file"] = discord.File(tmp_path, filename=FOOTER_ATTACHMENT_NAME)
            return f"attachment://{FOOTER_ATTACHMENT_NAME}"
        except Exception as e:
            print("Footer attach failed:", e)
    return None

def embed_with_footer(embed: discord.Embed):
    embed.set_footer(text=FOOTER_TEXT)
    return embed

# ---------------- EMBED BUILDERS ----------------
def user_embed_for_ticket(user: discord.User, content: str) -> discord.Embed:
    e = discord.Embed(description=content or "‎", color=discord.Color.blurple(), timestamp=datetime.datetime.utcnow())
    e.set_author(name=f"📩 {user}", icon_url=user.display_avatar.url if hasattr(user, "display_avatar") else None)
    e = embed_with_footer(e)
    return e

def staff_embed_for_user(staff: discord.Member, content: str) -> discord.Embed:
    e = discord.Embed(description=content or "‎", color=discord.Color.orange(), timestamp=datetime.datetime.utcnow())
    e.set_author(name=f"👤 {staff}", icon_url=staff.display_avatar.url if hasattr(staff, "display_avatar") else None)
    e = embed_with_footer(e)
    return e

def log_embed(title: str, description: str) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=discord.Color.dark_grey(), timestamp=datetime.datetime.utcnow())
    e = embed_with_footer(e)
    return e

# ---------------- TRANSCRIPT ----------------
async def create_transcript(channel: discord.TextChannel) -> str:
    lines = []
    async for m in channel.history(limit=None, oldest_first=True):
        ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
        author = f"{m.author} ({m.author.id})"
        content = m.content or ""
        lines.append(f"[{ts}] {author}: {content}")
        for a in m.attachments:
            lines.append(f"    [attachment] {a.url}")
    fd, path = tempfile.mkstemp(prefix=f"transcript_{channel.id}_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path

# ---------------- TICKET CREATION (user cannot view channel) ----------------
async def create_ticket_for_user(guild: discord.Guild, user: discord.User, opened_via: str, first_message: Optional[discord.Message] = None) -> Tuple[Optional[discord.TextChannel], Optional[str]]:
    gc = ensure_guild_cfg(guild)
    cat_id = gc.get("category_id")
    staff_id = gc.get("staff_role_id")
    if not cat_id or not staff_id:
        return None, "Ticket system not configured for this server."

    category = guild.get_channel(cat_id)
    staff_role = guild.get_role(staff_id)
    if not isinstance(category, discord.CategoryChannel) or staff_role is None:
        return None, "Configured category or staff role not found."

    # if already exists mapping
    if str(user.id) in gc.get("tickets", {}):
        ch = guild.get_channel(gc["tickets"][str(user.id)])
        return ch, "You already have an open ticket."

    name = f"ticket-{user.name}".replace(" ", "-")[:90]
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        staff_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        # intentionally NOT granting permission to user (user can't view)
    }
    try:
        ch = await guild.create_text_channel(name=name, category=category, overwrites=overwrites, topic=str(user.id))
    except Exception as e:
        return None, f"Failed to create ticket channel: {e}"

    # save mapping
    gc.setdefault("tickets", {})[str(user.id)] = ch.id
    save_cfg(CFG)

    # announce and log
    await ch.send(f"{staff_role.mention} 📬 New ticket opened by **{user}** (via {opened_via}). User cannot view this channel.")
    le = log_embed("Ticket Opened", f"User: {user} ({user.id})\nChannel: {ch.mention}\nOpened via: {opened_via}")
    # attach footer file if exists
    send_kwargs = {}
    footer_url = attach_footer_file(send_kwargs)
    if footer_url:
        le.set_footer(text=FOOTER_TEXT, icon_url=footer_url)
    await send_log(guild, le)

    # forward first message if provided
    if first_message:
        emb = user_embed_for_ticket(user, first_message.content or "")
        send_kwargs2 = {}
        footer_url2 = attach_footer_file(send_kwargs2)
        if footer_url2: emb.set_footer(text=FOOTER_TEXT, icon_url=footer_url2)
        files = []
        for a in first_message.attachments:
            try:
                files.append(await a.to_file())
            except: pass
        try:
            if files:
                await ch.send(embed=emb, files=files)
            else:
                await ch.send(embed=emb)
        except: pass

    # DM user confirmation
    try:
        emb_dm = discord.Embed(title="✅ Ticket Created", description=f"Your ticket has been created in **{guild.name}**. Staff will respond in this DM.", color=discord.Color.green())
        footer_url3 = attach_footer_file(send_kwargs)
        if footer_url3: emb_dm.set_footer(text=FOOTER_TEXT, icon_url=footer_url3)
        await user.send(embed=emb_dm)
    except:
        pass

    return ch, None

# ---------------- DM COUNTDOWN FLOW ----------------
async def dm_countdown_and_create(user: discord.User, guild: discord.Guild, initial_message: Optional[discord.Message] = None, source: str = "DM"):
    """Send one DM embed that updates countdown each second, then create ticket automatically and forward initial_message."""
    # initial embed
    desc = "A private support ticket will be created. You will continue in DM — staff cannot see your DMs directly."
    seconds = DM_COUNTDOWN
    embed = discord.Embed(title="Create Support Ticket", description=f"{desc}\n\n**Opening in:** `{seconds}s`", color=discord.Color.blue())
    footer_url = None
    send_kwargs = {}
    footer_url = attach_footer_file(send_kwargs)
    if footer_url:
        embed.set_footer(text=FOOTER_TEXT, icon_url=footer_url)
    try:
        msg = await user.send(embed=embed, **({"file": send_kwargs.get("file")} if "file" in send_kwargs else {}))
    except Exception:
        return  # cannot DM

    # update countdown
    for remaining in range(seconds - 1, -1, -1):
        await asyncio.sleep(1)
        try:
            embed.description = f"{desc}\n\n**Opening in:** `{remaining}s`"
            await msg.edit(embed=embed)
        except:
            pass

    # pick guild (use provided guild)
    ch, err = await create_ticket_for_user(guild, user, opened_via=source, first_message=initial_message)
    if err:
        try:
            await user.send(f"⚠️ {err}")
        except:
            pass
        return
    # done

# ---------------- COMMANDS ----------------
def admin_check(interaction: discord.Interaction) -> bool:
    # Admin if member has administrator permission
    if isinstance(interaction.user, discord.Member):
        return interaction.user.guild_permissions.administrator or interaction.user.id == OWNER_ID
    return interaction.user.id == OWNER_ID

@tree.command(name="set_category", description="Set ticket category (Admin)")
@app_commands.describe(category="Category where tickets will be created")
async def set_category(interaction: discord.Interaction, category: discord.CategoryChannel):
    if not admin_check(interaction):
        return await interaction.response.send_message("❌ You don’t have permission to use this command.", ephemeral=True)
    gc = ensure_guild_cfg(interaction.guild)
    gc["category_id"] = category.id
    save_cfg(CFG)
    await interaction.response.send_message(f"✅ Category set to **{category.name}**", ephemeral=True)

@tree.command(name="set_staffrole", description="Set staff role (Admin)")
@app_commands.describe(role="Role that will be staff")
async def set_staffrole(interaction: discord.Interaction, role: discord.Role):
    if not admin_check(interaction):
        return await interaction.response.send_message("❌ You don’t have permission to use this command.", ephemeral=True)
    gc = ensure_guild_cfg(interaction.guild)
    gc["staff_role_id"] = role.id
    save_cfg(CFG)
    await interaction.response.send_message(f"✅ Staff role set to {role.mention}", ephemeral=True)

@tree.command(name="set_logchannel", description="Set log channel (Admin)")
@app_commands.describe(channel="Channel where logs & transcripts will be posted")
async def set_logchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not admin_check(interaction):
        return await interaction.response.send_message("❌ You don’t have permission to use this command.", ephemeral=True)
    gc = ensure_guild_cfg(interaction.guild)
    gc["log_channel_id"] = channel.id
    save_cfg(CFG)
    await interaction.response.send_message(f"✅ Log channel set to {channel.mention}", ephemeral=True)

@tree.command(name="settings", description="Show ModMail settings (Admin/Staff)")
async def settings(interaction: discord.Interaction):
    gc = ensure_guild_cfg(interaction.guild)
    cat = interaction.guild.get_channel(gc.get("category_id")) if gc.get("category_id") else None
    sr = interaction.guild.get_role(gc.get("staff_role_id")) if gc.get("staff_role_id") else None
    log = interaction.guild.get_channel(gc.get("log_channel_id")) if gc.get("log_channel_id") else None
    e = discord.Embed(title="ModMail Settings", color=discord.Color.blurple())
    e.add_field(name="Category", value=cat.name if cat else "❌ Not set", inline=False)
    e.add_field(name="Staff Role", value=sr.mention if sr else "❌ Not set", inline=False)
    e.add_field(name="Log Channel", value=log.mention if log else "❌ Not set", inline=False)
    footer_url = attach_footer_file({})
    if footer_url: e.set_footer(text=FOOTER_TEXT, icon_url=footer_url)
    await interaction.response.send_message(embed=e, ephemeral=True)

@tree.command(name="send_panel", description="Send ticket panel (Requires Administrator permission)")
async def send_panel(interaction: discord.Interaction):
    # permission: the user who invokes must have Administrator perms on server
    if not admin_check(interaction):
        return await interaction.response.send_message("❌ You don’t have permission to use this command.", ephemeral=True)
    gc = ensure_guild_cfg(interaction.guild)
    if not gc.get("category_id") or not gc.get("staff_role_id"):
        return await interaction.response.send_message("⚠️ Configure category & staff role first (use /set_category and /set_staffrole).", ephemeral=True)
    embed = discord.Embed(title="📩 Support", description="Click the button below to open a private ModMail ticket. You will chat via DM only.", color=discord.Color.green())
    footer_url = attach_footer_file({})
    if footer_url: embed.set_footer(text=FOOTER_TEXT, icon_url=footer_url)
    await interaction.channel.send(embed=embed, view=PanelView())
    await interaction.response.send_message("✅ Panel sent.", ephemeral=True)

@tree.command(name="modmail", description="Open a ticket (starts a DM countdown and auto-creates)")
async def modmail(interaction: discord.Interaction):
    gc = ensure_guild_cfg(interaction.guild)
    if not gc.get("category_id") or not gc.get("staff_role_id"):
        return await interaction.response.send_message("⚠️ Ticket system not configured.", ephemeral=True)
    left = in_cooldown(interaction.guild.id, interaction.user.id)
    if left > 0:
        return await interaction.response.send_message(f"⏳ Please wait {left}s before creating another ticket.", ephemeral=True)
    await interaction.response.send_message("📩 Check your DMs — starting the ticket countdown.", ephemeral=True)
    try:
        await dm_countdown_and_create(interaction.user, interaction.guild, source="slash")
        start_cooldown(interaction.guild.id, interaction.user.id)
    except discord.Forbidden:
        await interaction.followup.send("❗ I cannot DM you. Enable DMs and try again.", ephemeral=True)

@tree.command(name="close", description="Close this ticket (staff/admin)")
async def close(interaction: discord.Interaction):
    if not has_staff_member(interaction.user if isinstance(interaction.user, discord.Member) else None, interaction.guild):
        return await interaction.response.send_message("❌ You don’t have permission to close tickets.", ephemeral=True)
    gc = ensure_guild_cfg(interaction.guild)
    if str(interaction.channel.id) not in map(str, gc.get("tickets", {}).values()):
        return await interaction.response.send_message("⚠️ This is not a ticket channel.", ephemeral=True)
    await interaction.response.send_message("🗂 Closing ticket and saving transcript...", ephemeral=True)
    await close_ticket(interaction.guild, interaction.channel, interaction.user)

@tree.command(name="refresh", description="Refresh slash commands (Admin/Owner)")
async def refresh_cmd(inter: discord.Interaction):
    if not admin_check(inter):
        return await interaction.response.send_message("❌ You don’t have permission.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        await tree.sync()
        await interaction.followup.send("✅ Commands refreshed.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"⚠️ Sync failed: {e}", ephemeral=True)

# Owner-only command to completely clear old application commands (use if previous commands are stuck)
@tree.command(name="clear_commands", description="Owner: Clear global & guild slash commands (use if old commands persist)")
async def clear_commands(inter: discord.Interaction):
    if interaction := None: pass
    if getattr(interaction, "user", None) is not None:
        pass
    # check owner
    if not (isinstance(interaction := None, type(None))): pass  # dummy to satisfy linter
    # implement owner check via context below instead (app commands don't expose ctx)
    # We'll implement as plain check:
    if not (app := None):
        pass

# Because the above app_command can't easily access owner in a single-line, implement clear via a command
@bot.command(name="__clear_commands_owner_only_internal", hidden=True)
@commands.is_owner()
async def _clear_commands_owner_only(ctx: commands.Context):
    # clear global
    await tree.sync(guild=None)
    await tree.clear_commands(guild=None)
    # clear per-guild
    for g in bot.guilds:
        await tree.sync(guild=discord.Object(id=g.id))
        await tree.clear_commands(guild=discord.Object(id=g.id))
    await ctx.send("✅ Cleared global and guild commands. Restart bot and re-sync desired commands.")

# ---------------- UTIL: staff check ----------------
def has_staff_member(member: Optional[discord.Member], guild: discord.Guild) -> bool:
    if member is None:
        return False
    if member.id == OWNER_ID:
        return True
    gc = ensure_guild_cfg(guild)
    rid = gc.get("staff_role_id")
    if not rid:
        return False
    role = guild.get_role(rid)
    return bool(role and role in member.roles) or member.guild_permissions.administrator

# ---------------- PANEL VIEW ----------------
class PanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="🎟 Create Ticket", style=discord.ButtonStyle.green, custom_id="panel_create_ticket")
    async def btn_create(self, interaction: discord.Interaction, button: ui.Button):
        # check cooldown
        left = in_cooldown(interaction.guild.id, interaction.user.id)
        if left > 0:
            return await interaction.response.send_message(f"⏳ Please wait {left}s before opening another ticket.", ephemeral=True)
        await interaction.response.send_message("📩 Check your DMs — starting the ticket countdown.", ephemeral=True)
        try:
            await dm_countdown_and_create(interaction.user, interaction.guild, source="panel")
            start_cooldown(interaction.guild.id, interaction.user.id)
        except discord.Forbidden:
            await interaction.followup.send("❗ I can't DM you. Enable DMs and try again.", ephemeral=True)

# ---------------- MESSAGE FORWARDING ----------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # DM from user
    if isinstance(message.channel, discord.DMChannel):
        # if user already has open ticket in any guild we manage, forward to that ticket
        forwarded = False
        for gid, gcfg in CFG.get("guilds", {}).items():
            if str(message.author.id) in gcfg.get("tickets", {}):
                g = bot.get_guild(int(gid))
                if g:
                    ch = g.get_channel(gcfg["tickets"][str(message.author.id)])
                    if ch:
                        emb = user_embed_for_ticket(message.author, message.content or "")
                        files = []
                        for a in message.attachments:
                            try: files.append(await a.to_file())
                            except: pass
                        try:
                            if files: await ch.send(embed=emb, files=files)
                            else:     await ch.send(embed=emb)
                        except: pass
                        forwarded = True
                        break
        if forwarded:
            return

        # else pick the first guild that has category+role configured (per earlier behavior)
        target_guild = None
        for g in bot.guilds:
            gcfg = ensure_guild_cfg(g)
            if gcfg.get("category_id") and gcfg.get("staff_role_id"):
                target_guild = g
                break
        if not target_guild:
            try: await message.author.send("⚠️ No servers are configured for ModMail yet. Please try later.")
            except: pass
            return

        # cooldown check
        left = in_cooldown(target_guild.id, message.author.id)
        if left > 0:
            try: await message.author.send(f"⏳ Please wait {left}s before opening another ticket.")
            except: pass
            return

        # start countdown and auto-create and forward this first message
        try:
            await dm_countdown_and_create(message.author, target_guild, initial_message=message, source="DM")
            start_cooldown(target_guild.id, message.author.id)
        except discord.Forbidden:
            try: await message.author.send("❗ I can't DM you. Enable DMs and try again.")
            except: pass
        return

    # message in guild channel -> maybe staff replying in ticket to forward to user
    if message.guild:
        gc = ensure_guild_cfg(message.guild)
        tickets = gc.get("tickets", {})
        # if this channel is one of ticket channels:
        for uid, ch_id in tickets.items():
            if ch_id == message.channel.id:
                # only forward staff messages
                if not has_staff_member(message.author if isinstance(message.author, discord.Member) else None, message.guild):
                    return
                # forward to user
                try:
                    user = await bot.fetch_user(int(uid))
                except: return
                emb = staff_embed_for_user(message.author, message.content or "")
                files = []
                for a in message.attachments:
                    try: files.append(await a.to_file())
                    except: pass
                try:
                    if files: await user.send(embed=emb, files=files)
                    else:     await user.send(embed=emb)
                except:
                    try: await message.channel.send("⚠️ Couldn't DM the user (their DMs are closed).")
                    except: pass
                # log forwarding
                le = log_embed("Staff replied", f"{message.author} → {user} in {message.channel.mention}")
                send_kwargs = {}
                footer_url = attach_footer_file(send_kwargs)
                if footer_url: le.set_footer(text=FOOTER_TEXT, icon_url=footer_url)
                await send_log(message.guild, le)
                return

    await bot.process_commands(message)

# ---------------- READY ----------------
@bot.event
async def on_ready():
    print("Bot ready:", bot.user)
    try:
        await tree.sync()
        print("Commands synced.")
    except Exception as e:
        print("Sync error:", e)

# ---------------- RUN ----------------
if not TOKEN:
    print("ERROR: set DISCORD_BOT_TOKEN environment variable")
else:
    bot.run(TOKEN)
