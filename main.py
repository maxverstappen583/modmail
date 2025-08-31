# main.py
import discord
from discord.ext import commands
from discord import app_commands, ui
import asyncio, json, os, tempfile, datetime
from flask import Flask
from threading import Thread
from typing import Optional, Tuple

# ---------------- CONFIG ----------------
OWNER_ID = 1319292111325106296
GUILD_ID = 1364371104755613837   # <<< your server ID (locked to this guild)
TOKEN = os.getenv("DISCORD_BOT_TOKEN")  # set this in your host env

CONFIG_FILE = "config.json"
FOOTER_IMAGE_PATH = "/mnt/data/DEBEC8AF-40C2-421C-8F41-B606AB6A6072.jpeg"
FOOTER_ATTACHMENT_NAME = "footer.png"
FOOTER_TEXT = "@ u4_straight1"

DM_COUNTDOWN = 15  # seconds before creating ticket

# ---------------- FLASK KEEP-ALIVE ----------------
app = Flask(__name__)

@app.route("/")
def home():
    return "ModMail bot is alive"

def _run_flask():
    app.run(host="0.0.0.0", port=10000)

Thread(target=_run_flask, daemon=True).start()

# ---------------- STORAGE ----------------
def default_guild_config():
    return {
        "category_id": None,
        "staff_role_id": None,
        "log_channel_id": None,
        "cooldown": 300,
        "tickets": {}  # user_id -> channel_id
    }

if not os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, "w") as f:
        json.dump({"guilds": {}}, f, indent=4)

def load_cfg():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_cfg(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=4)

CFG = load_cfg()

def ensure_guild_cfg(guild_id: int):
    gid = str(guild_id)
    if "guilds" not in CFG:
        CFG["guilds"] = {}
    if gid not in CFG["guilds"]:
        CFG["guilds"][gid] = default_guild_config()
        save_cfg(CFG)
    return CFG["guilds"][gid]

def guild_cfg_obj(guild: discord.Guild):
    return ensure_guild_cfg(guild.id)

# ---------------- BOT SETUP ----------------
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# runtime cooldown tracking per (guild_id, user_id)
_last_open = {}  # (guild_id, user_id) -> timestamp

def in_cooldown(guild_id: int, user_id: int) -> int:
    key = (guild_id, user_id)
    now = asyncio.get_event_loop().time()
    last = _last_open.get(key, 0)
    cd = ensure_guild_cfg(guild_id).get("cooldown", 300)
    left = int(cd - (now - last))
    return max(0, left)

def start_cooldown(guild_id: int, user_id: int):
    _last_open[(guild_id, user_id)] = asyncio.get_event_loop().time()

# ---------------- FOOTER HELPERS ----------------
def attach_footer_send_kwargs() -> Tuple[Optional[discord.File], Optional[str]]:
    """Return (discord.File or None, icon_url or None) for footer attachment."""
    if os.path.exists(FOOTER_IMAGE_PATH):
        try:
            tmp_path = os.path.join(tempfile.gettempdir(), FOOTER_ATTACHMENT_NAME)
            with open(FOOTER_IMAGE_PATH, "rb") as src, open(tmp_path, "wb") as dst:
                dst.write(src.read())
            file_obj = discord.File(tmp_path, filename=FOOTER_ATTACHMENT_NAME)
            return file_obj, f"attachment://{FOOTER_ATTACHMENT_NAME}"
        except Exception as e:
            print("Footer attach failed:", e)
    return None, None

def set_footer_on_embed(embed: discord.Embed, icon_url: Optional[str]):
    if icon_url:
        embed.set_footer(text=FOOTER_TEXT, icon_url=icon_url)
    else:
        embed.set_footer(text=FOOTER_TEXT)

# ---------------- EMBED HELPERS ----------------
def user_embed(user: discord.User, content: str) -> discord.Embed:
    e = discord.Embed(description=content or "‎", color=discord.Color.blurple(), timestamp=datetime.datetime.utcnow())
    e.set_author(name=f"📩 {user}", icon_url=user.display_avatar.url if hasattr(user, "display_avatar") else None)
    return e

def staff_embed(member: discord.Member, content: str) -> discord.Embed:
    e = discord.Embed(description=content or "‎", color=discord.Color.orange(), timestamp=datetime.datetime.utcnow())
    e.set_author(name=f"👤 {member}", icon_url=member.display_avatar.url if hasattr(member, "display_avatar") else None)
    return e

def log_embed(title: str, description: str) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=discord.Color.dark_grey(), timestamp=datetime.datetime.utcnow())
    return e

# ---------------- TRANSCRIPT ----------------
async def build_transcript(channel: discord.TextChannel) -> str:
    lines = []
    async for m in channel.history(limit=None, oldest_first=True):
        ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
        who = f"{m.author} ({m.author.id})"
        content = m.content or ""
        lines.append(f"[{ts}] {who}: {content}")
        for a in m.attachments:
            lines.append(f"    [attachment] {a.url}")
    fd, path = tempfile.mkstemp(prefix=f"transcript_{channel.id}_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path

# ---------------- CREATE / CLOSE ----------------
async def create_ticket(guild: discord.Guild, user: discord.User, opened_via: str, first_message: Optional[discord.Message] = None) -> Tuple[Optional[discord.TextChannel], Optional[str]]:
    cfg_g = guild_cfg_obj(guild.id)
    cat_id = cfg_g.get("category_id")
    staff_role_id = cfg_g.get("staff_role_id")
    if not cat_id or not staff_role_id:
        return None, "Ticket system not configured (category or staff role missing)."

    cat = guild.get_channel(cat_id)
    staff_role = guild.get_role(staff_role_id)
    if not isinstance(cat, discord.CategoryChannel) or staff_role is None:
        return None, "Configured category or staff role not found."

    if str(user.id) in cfg_g.get("tickets", {}):
        ch_id = cfg_g["tickets"][str(user.id)]
        ch = guild.get_channel(ch_id)
        return ch, "You already have an open ticket."

    name = f"ticket-{user.name}".replace(" ", "-")[:90]
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        staff_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    try:
        ch = await guild.create_text_channel(name=name, category=cat, overwrites=overwrites, topic=str(user.id))
    except Exception as e:
        return None, f"Failed to create channel: {e}"

    cfg_g.setdefault("tickets", {})[str(user.id)] = ch.id
    save_cfg(CFG)

    # announce in ticket and log
    await ch.send(f"{staff_role.mention} 📬 New ticket opened by **{user}** (via {opened_via}). User cannot view this channel.")
    le = log_embed("Ticket Opened", f"User: {user} ({user.id})\nChannel: {ch.mention}\nOpened via: {opened_via}")
    file_obj, icon_url = attach_footer_send_kwargs()
    set_footer_on_embed(le, icon_url)
    try:
        if file_obj:
            await send_log(guild, le, file_obj)
        else:
            await send_log(guild, le, None)
    except:
        pass

    # forward first message if present
    if first_message:
        emb = user_embed(user, first_message.content or "")
        files = []
        for a in first_message.attachments:
            try: files.append(await a.to_file())
            except: pass
        try:
            if files: await ch.send(embed=emb, files=files)
            else:     await ch.send(embed=emb)
        except:
            pass

    # DM confirmation
    try:
        emb_dm = discord.Embed(title="✅ Ticket Created", description=f"Your private ticket in **{guild.name}** has been created. Staff will reply here.", color=discord.Color.green())
        set_footer_on_embed(emb_dm, icon_url)
        if file_obj:
            await user.send(embed=emb_dm, file=file_obj)
        else:
            await user.send(embed=emb_dm)
    except:
        pass

    return ch, None

async def close_ticket(channel: discord.TextChannel, closed_by: discord.Member):
    guild = channel.guild
    cfg_g = guild_cfg_obj(guild.id)
    user_id = None
    if channel.topic and channel.topic.isdigit():
        user_id = channel.topic
    else:
        for uid, cid in cfg_g.get("tickets", {}).items():
            if cid == channel.id:
                user_id = uid
                break

    transcript = None
    try:
        transcript = await build_transcript(channel)
    except Exception as e:
        print("Transcript create failed:", e)

    e = log_embed("Ticket Closed", f"Channel: {channel.name}\nClosed by: {closed_by}")
    file_obj, icon_url = attach_footer_send_kwargs()
    set_footer_on_embed(e, icon_url)
    try:
        if transcript:
            await send_log(guild, e, discord.File(transcript, filename=os.path.basename(transcript)))
        else:
            await send_log(guild, e, file_obj)
    except:
        pass

    if user_id:
        try:
            u = await bot.fetch_user(int(user_id))
            try: await u.send("✅ Your ticket has been closed by staff.")
            except: pass
        except:
            pass
        cfg_g.get("tickets", {}).pop(str(user_id), None)
        save_cfg(CFG)

    try:
        await channel.delete()
    except:
        pass

    if transcript:
        try: os.remove(transcript)
        except: pass

# ---------------- LOGGING ----------------
async def send_log(guild: discord.Guild, embed: discord.Embed, file_obj: Optional[discord.File] = None):
    cfg_g = guild_cfg_obj(guild.id)
    lid = cfg_g.get("log_channel_id")
    if not lid: return
    ch = guild.get_channel(lid)
    if not ch: return
    try:
        if file_obj:
            await ch.send(embed=embed, file=file_obj)
        else:
            await ch.send(embed=embed)
    except Exception as e:
        print("Log send failed:", e)

# ---------------- DM COUNTDOWN ----------------
async def dm_countdown_then_create(user: discord.User, guild: discord.Guild, initial_message: Optional[discord.Message] = None, source: str = "DM"):
    try:
        file_obj, icon_url = attach_footer_send_kwargs()
        embed = discord.Embed(title="Create Support Ticket", description="A private ticket will be created and staff will respond in the server. You will continue in DM.", color=discord.Color.blue())
        embed.add_field(name="Starting in", value=f"`{DM_COUNTDOWN}s`", inline=False)
        set_footer_on_embed(embed, icon_url)
        if file_obj:
            dm_msg = await user.send(embed=embed, file=file_obj)
        else:
            dm_msg = await user.send(embed=embed)
    except Exception:
        return

    for remain in range(DM_COUNTDOWN - 1, -1, -1):
        await asyncio.sleep(1)
        try:
            embed.clear_fields()
            embed.add_field(name="Starting in", value=f"`{remain}s`", inline=False)
            await dm_msg.edit(embed=embed)
        except:
            pass

    ch, err = await create_ticket(guild, user, opened_via=source, first_message=initial_message)
    if err:
        try: await user.send(f"⚠️ {err}")
        except: pass
        return
    start_cooldown(guild.id, user.id)

# ---------------- PANEL VIEW ----------------
class PanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="🎟 Create Ticket", style=discord.ButtonStyle.green, custom_id="panel_create_ticket")
    async def create_ticket_button(self, interaction: discord.Interaction, button: ui.Button):
        cfg_g = guild_cfg_obj(interaction.guild)
        if not cfg_g.get("category_id") or not cfg_g.get("staff_role_id"):
            return await interaction.response.send_message("⚠️ Ticket system not configured on this server.", ephemeral=True)

        left = in_cooldown(interaction.guild.id, interaction.user.id)
        if left > 0:
            return await interaction.response.send_message(f"⏳ Please wait {left}s before opening another ticket.", ephemeral=True)

        # MUST respond or defer
        await interaction.response.send_message("📩 Check your DMs — starting countdown.", ephemeral=True)
        try:
            await dm_countdown_then_create(interaction.user, interaction.guild, source="panel")
        except discord.Forbidden:
            await interaction.followup.send("❗ I can't DM you. Please enable DMs and try again.", ephemeral=True)

# ---------------- COMMANDS ----------------
def admin_check(interaction: discord.Interaction):
    m = interaction.user if isinstance(interaction.user, discord.Member) else None
    return bool(m and m.guild_permissions.administrator) or interaction.user.id == OWNER_ID

# We always defer when we will do longer work; all commands respond or defer.

@tree.command(name="set_category", description="Set the ticket category (Admin required)", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(category="Category where tickets will be created")
async def cmd_set_category(interaction: discord.Interaction, category: discord.CategoryChannel):
    if not admin_check(interaction):
        return await interaction.response.send_message("❌ You don't have permission.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    cfg_g = ensure_guild_cfg(interaction.guild.id)
    cfg_g["category_id"] = category.id
    save_cfg(CFG)
    await interaction.followup.send(f"✅ Category set to **{category.name}**", ephemeral=True)

@tree.command(name="set_staffrole", description="Set the staff role (Admin required)", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(role="Role to set as staff")
async def cmd_set_staffrole(interaction: discord.Interaction, role: discord.Role):
    if not admin_check(interaction):
        return await interaction.response.send_message("❌ You don't have permission.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    cfg_g = ensure_guild_cfg(interaction.guild.id)
    cfg_g["staff_role_id"] = role.id
    save_cfg(CFG)
    await interaction.followup.send(f"✅ Staff role set to {role.mention}", ephemeral=True)

@tree.command(name="set_logchannel", description="Set log channel for tickets (Admin required)", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(channel="Channel to post ticket logs/transcripts")
async def cmd_set_logchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not admin_check(interaction):
        return await interaction.response.send_message("❌ You don't have permission.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    cfg_g = ensure_guild_cfg(interaction.guild.id)
    cfg_g["log_channel_id"] = channel.id
    save_cfg(CFG)
    await interaction.followup.send(f"✅ Log channel set to {channel.mention}", ephemeral=True)

@tree.command(name="set_cooldown", description="Set per-guild ticket creation cooldown in seconds (Admin)", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(seconds="Cooldown in seconds")
async def cmd_set_cooldown(interaction: discord.Interaction, seconds: int):
    if not admin_check(interaction):
        return await interaction.response.send_message("❌ You don't have permission.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    cfg_g = ensure_guild_cfg(interaction.guild.id)
    cfg_g["cooldown"] = max(0, int(seconds))
    save_cfg(CFG)
    await interaction.followup.send(f"✅ Cooldown set to {cfg_g['cooldown']} seconds", ephemeral=True)

@tree.command(name="settings", description="View ModMail settings (Admin/Staff)", guild=discord.Object(id=GUILD_ID))
async def cmd_settings(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    cfg_g = ensure_guild_cfg(interaction.guild.id)
    cat = interaction.guild.get_channel(cfg_g.get("category_id")) if cfg_g.get("category_id") else None
    sr = interaction.guild.get_role(cfg_g.get("staff_role_id")) if cfg_g.get("staff_role_id") else None
    log = interaction.guild.get_channel(cfg_g.get("log_channel_id")) if cfg_g.get("log_channel_id") else None
    e = discord.Embed(title="ModMail Settings", color=discord.Color.blurple())
    e.add_field(name="Category", value=cat.name if cat else "❌ Not set", inline=False)
    e.add_field(name="Staff Role", value=sr.mention if sr else "❌ Not set", inline=False)
    e.add_field(name="Log Channel", value=log.mention if log else "❌ Not set", inline=False)
    e.add_field(name="Cooldown", value=f"{cfg_g.get('cooldown', 300)}s", inline=False)
    file_obj, icon_url = attach_footer_send_kwargs()
    set_footer_on_embed(e, icon_url)
    if file_obj:
        await interaction.followup.send(embed=e, file=file_obj, ephemeral=True)
    else:
        await interaction.followup.send(embed=e, ephemeral=True)

@tree.command(name="send_panel", description="Send a Create Ticket panel (Admin only)", guild=discord.Object(id=GUILD_ID))
async def cmd_send_panel(interaction: discord.Interaction):
    if not admin_check(interaction):
        return await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    cfg_g = ensure_guild_cfg(interaction.guild.id)
    if not cfg_g.get("category_id") or not cfg_g.get("staff_role_id"):
        return await interaction.followup.send("⚠️ Configure category & staff role first.", ephemeral=True)
    e = discord.Embed(title="📩 Support Panel", description="Click the button below to open a private ModMail ticket. You will chat via DM only.", color=discord.Color.green())
    file_obj, icon_url = attach_footer_send_kwargs()
    set_footer_on_embed(e, icon_url)
    view = PanelView()
    if file_obj:
        await interaction.channel.send(embed=e, view=view, file=file_obj)
    else:
        await interaction.channel.send(embed=e, view=view)
    await interaction.followup.send("✅ Panel sent.", ephemeral=True)

@tree.command(name="modmail", description="Open a ModMail ticket (starts DM countdown)", guild=discord.Object(id=GUILD_ID))
async def cmd_modmail(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    cfg_g = ensure_guild_cfg(interaction.guild.id)
    if not cfg_g.get("category_id") or not cfg_g.get("staff_role_id"):
        return await interaction.followup.send("⚠️ Ticket system not configured.", ephemeral=True)
    left = in_cooldown(interaction.guild.id, interaction.user.id)
    if left > 0:
        return await interaction.followup.send(f"⏳ Please wait {left}s before opening another ticket.", ephemeral=True)
    await interaction.followup.send("📩 Check your DMs — starting the ticket countdown.", ephemeral=True)
    try:
        await dm_countdown_then_create(interaction.user, interaction.guild, source="slash")
    except discord.Forbidden:
        await interaction.followup.send("❗ I can't DM you. Enable DMs and try again.", ephemeral=True)

@tree.command(name="close", description="Close this ticket (staff/admin)", guild=discord.Object(id=GUILD_ID))
async def cmd_close(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    cfg_g = ensure_guild_cfg(interaction.guild.id)
    staff_role = interaction.guild.get_role(cfg_g.get("staff_role_id")) if cfg_g.get("staff_role_id") else None
    if not (member and (member.guild_permissions.administrator or (staff_role and staff_role in member.roles))):
        return await interaction.followup.send("❌ You don't have permission to close tickets.", ephemeral=True)
    if str(interaction.channel.id) not in map(str, cfg_g.get("tickets", {}).values()):
        return await interaction.followup.send("⚠️ This is not a ModMail ticket channel.", ephemeral=True)
    await interaction.followup.send("🗂 Closing ticket and saving transcript...", ephemeral=True)
    await close_ticket(interaction.channel, interaction.user)

@tree.command(name="refresh", description="Admin: refresh commands for this guild (clears old commands & re-sync)", guild=discord.Object(id=GUILD_ID))
async def cmd_refresh(interaction: discord.Interaction):
    if not admin_check(interaction):
        return await interaction.response.send_message("❌ You don't have permission.", ephemeral=True)
    # MUST defer because this may take > 3s
    await interaction.response.defer(ephemeral=True)
    try:
        # clear guild commands then sync current ones
        tree.clear_commands(guild=discord.Object(id=GUILD_ID))
        await tree.sync(guild=discord.Object(id=GUILD_ID))
        # also sync global to be safe
        await tree.sync()
        await interaction.followup.send("✅ Guild commands refreshed (old guild commands cleared).", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"⚠️ Refresh failed: {e}", ephemeral=True)

# Owner helper to fully clear global & guild commands and re-sync (owner only)
@bot.command(name="_owner_clear_and_refresh", hidden=True)
@commands.is_owner()
async def _owner_clear_and_refresh(ctx: commands.Context):
    await ctx.send("⏳ Clearing global & guild application commands...")
    try:
        # clear global
        await tree.sync(guild=None)
        await tree.clear_commands(guild=None)
    except Exception as e:
        print("clear global failed:", e)
    # clear per-guild
    for g in bot.guilds:
        try:
            await tree.sync(guild=discord.Object(id=g.id))
            await tree.clear_commands(guild=discord.Object(id=g.id))
        except Exception as e:
            print(f"clear guild {g.id} failed:", e)
    # resync current commands
    await tree.sync()
    await ctx.send("✅ Cleared and re-synced commands. Old sticky commands should be gone.")

# ---------------- MESSAGE FORWARDING ----------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # DMs from user -> forward to their ticket if exists, otherwise start countdown and create ticket
    if isinstance(message.channel, discord.DMChannel):
        forwarded = False
        for gid, gdata in CFG.get("guilds", {}).items():
            if str(message.author.id) in gdata.get("tickets", {}):
                g = bot.get_guild(int(gid))
                if not g: continue
                ch = g.get_channel(gdata["tickets"][str(message.author.id)])
                if not ch: continue
                emb = user_embed(message.author, message.content or "")
                files = []
                for a in message.attachments:
                    try: files.append(await a.to_file())
                    except: pass
                try:
                    if files: await ch.send(embed=emb, files=files)
                    else:     await ch.send(embed=emb)
                except:
                    pass
                forwarded = True
                break
        if forwarded:
            return

        # pick first configured guild that is usable
        target_guild = None
        for g in bot.guilds:
            gcfg = ensure_guild_cfg(g.id)
            if gcfg.get("category_id") and gcfg.get("staff_role_id"):
                target_guild = g
                break
        if not target_guild:
            try: await message.author.send("⚠️ No server is configured for ModMail yet. Please try later.")
            except: pass
            return

        left = in_cooldown(target_guild.id, message.author.id)
        if left > 0:
            try: await message.author.send(f"⏳ Please wait {left}s before opening another ticket.")
            except: pass
            return

        try:
            await dm_countdown_then_create(message.author, target_guild, initial_message=message, source="DM")
        except discord.Forbidden:
            try: await message.author.send("❗ I can't DM you. Enable DMs and try again.")
            except: pass
        return

    # Guild channel message -> forward staff replies in ticket channel to user's DM
    if message.guild:
        cfg_g = ensure_guild_cfg(message.guild.id)
        tickets = cfg_g.get("tickets", {})
        for uid, cid in list(tickets.items()):
            if cid == message.channel.id:
                # only forward staff/admin messages
                member = message.author if isinstance(message.author, discord.Member) else None
                if not member:
                    return
                staff_role_id = cfg_g.get("staff_role_id")
                role_obj = message.guild.get_role(staff_role_id) if staff_role_id else None
                if not (member.guild_permissions.administrator or (role_obj and role_obj in member.roles)):
                    return
                # forward
                try:
                    user = await bot.fetch_user(int(uid))
                except:
                    return
                emb = staff_embed(member, message.content or "")
                files = []
                for a in message.attachments:
                    try: files.append(await a.to_file())
                    except: pass
                try:
                    if files: await user.send(embed=emb, files=files)
                    else:     await user.send(embed=emb)
                except:
                    try: await message.channel.send("⚠️ Could not DM the user (their DMs may be closed).")
                    except: pass
                # log
                le = log_embed("Staff → User", f"{member} → {user} in {message.channel.mention}")
                file_obj, icon_url = attach_footer_send_kwargs()
                set_footer_on_embed(le, icon_url)
                await send_log(message.guild, le, file_obj)
                return

    await bot.process_commands(message)

# ---------------- READY ----------------
@bot.event
async def on_ready():
    print(f"Bot ready: {bot.user} (id: {bot.user.id})")
    try:
        await tree.sync(guild=discord.Object(id=GUILD_ID))
        print(f"Commands synced for guild {GUILD_ID}")
    except Exception as e:
        print("Sync failed:", e)

# ---------------- RUN ----------------
if not TOKEN:
    print("ERROR: set DISCORD_BOT_TOKEN environment variable")
else:
    bot.run(TOKEN)
