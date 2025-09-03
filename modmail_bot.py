# modmail_bot.py
import os
import sys
import json
import discord
import asyncio
from discord.ext import commands
from discord import ui
from flask import Flask
from threading import Thread
from datetime import datetime, timezone

# Optional OpenAI usage (only if OPENAI_API_KEY provided)
try:
    import openai
except Exception:
    openai = None

# -------------------------
# Environment & Validation
# -------------------------
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
PRIMARY_GUILD_ID = os.getenv("PRIMARY_GUILD_ID")
OWNER_ID = os.getenv("OWNER_ID", "1319292111325106296")  # fallback to your ID

OPENAI_KEY = os.getenv("OPENAI_API_KEY")  # optional

if not DISCORD_TOKEN:
    print("❌ ERROR: DISCORD_BOT_TOKEN not set. Exiting.")
    sys.exit(1)
if not PRIMARY_GUILD_ID:
    print("❌ ERROR: PRIMARY_GUILD_ID not set. Exiting.")
    sys.exit(1)

try:
    PRIMARY_GUILD_ID = int(PRIMARY_GUILD_ID)
    OWNER_ID = int(OWNER_ID)
except Exception:
    print("❌ ERROR: PRIMARY_GUILD_ID and OWNER_ID must be valid integers (IDs). Exiting.")
    sys.exit(1)

if OPENAI_KEY and openai:
    openai.api_key = OPENAI_KEY

# -------------------------
# Keep-alive Flask app
# -------------------------
app = Flask("modmail_keepalive")

@app.route("/")
def home():
    return "Modmail bot is running."

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

Thread(target=run_flask, daemon=True).start()

# -------------------------
# Data persistence
# -------------------------
DATA_FILE = "modmail_data.json"
DEFAULT_DATA = {
    "category_id": None,
    "staff_role_id": None,
    "solve_keyword": "solved",
    "close_keyword": "close",
    "tickets": {}  # maps user_id (str) -> channel_id (int)
}

def load_data():
    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, "w") as f:
            json.dump(DEFAULT_DATA, f, indent=4)
        return DEFAULT_DATA.copy()
    with open(DATA_FILE, "r") as f:
        d = json.load(f)
    # ensure defaults
    for k, v in DEFAULT_DATA.items():
        if k not in d:
            d[k] = v
    return d

def save_data(d):
    with open(DATA_FILE, "w") as f:
        json.dump(d, f, indent=4)

data = load_data()

# -------------------------
# Helpers
# -------------------------
def now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def is_staff(member: discord.Member):
    role_id = data.get("staff_role_id")
    if not role_id:
        return False
    return any(r.id == role_id for r in member.roles)

async def try_dm(user: discord.User, text: str):
    try:
        await user.send(text)
    except Exception:
        # ignore DM errors silently
        pass

# -------------------------
# Bot Setup
# -------------------------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# -------------------------
# Ticket UI (buttons)
# -------------------------
class TicketView(ui.View):
    def __init__(self, ticket_user_id: int, timeout=None):
        super().__init__(timeout=timeout)
        self.ticket_user_id = ticket_user_id

    @ui.button(label="Mark Solved", style=discord.ButtonStyle.success, emoji="✅")
    async def mark_solved(self, interaction: discord.Interaction, button: ui.Button):
        # staff-only
        if not interaction.guild:
            return await interaction.response.send_message("This button works only inside the server.", ephemeral=True)
        if not is_staff(interaction.user) and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❎ You are not staff.", ephemeral=True)
        embed = discord.Embed(title="✅ Problem Marked Solved",
                              description=f"Marked solved by {interaction.user.mention}",
                              color=discord.Color.green(),
                              timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Time", value=now_ts())
        await interaction.channel.send(embed=embed)
        await interaction.response.send_message("✅ Marked solved.", ephemeral=True)

    @ui.button(label="AI Reply (draft)", style=discord.ButtonStyle.secondary, emoji="🤖")
    async def ai_reply(self, interaction: discord.Interaction, button: ui.Button):
        # staff only
        if not interaction.guild:
            return await interaction.response.send_message("This button works only inside the server.", ephemeral=True)
        if not is_staff(interaction.user) and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❎ You are not staff.", ephemeral=True)
        if not OPENAI_KEY or openai is None:
            return await interaction.response.send_message("❎ OpenAI not configured.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)

        # Collect recent messages (excluding bots)
        msgs = []
        async for m in interaction.channel.history(limit=30, oldest_first=False):
            if m.author.bot:
                continue
            msgs.append(f"{m.author.name}: {m.content}")
        convo = "\n".join(reversed(msgs[:30]))
        prompt = (
            "You are a professional support assistant. Given the conversation, draft a short, professional reply staff can paste to the user.\n\n"
            f"Conversation:\n{convo}\n\nReply:"
        )

        try:
            completion = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[{"role":"user","content":prompt}],
                max_tokens=300,
                temperature=0.2
            )
            reply = completion.choices[0].message.content.strip()
            await interaction.followup.send(f"🤖 **AI draft:**\n\n{reply}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❎ OpenAI error: {e}", ephemeral=True)

    @ui.button(label="Close Ticket (delete)", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def close_ticket(self, interaction: discord.Interaction, button: ui.Button):
        if not interaction.guild:
            return await interaction.response.send_message("This button works only inside the server.", ephemeral=True)
        if not is_staff(interaction.user) and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❎ You are not staff.", ephemeral=True)

        await interaction.response.send_message("✅ Closing ticket — channel will be deleted in 5 seconds.", ephemeral=True)
        final_embed = discord.Embed(title="🗑️ Ticket Closed",
                                    description=f"Closed by {interaction.user.mention}",
                                    color=discord.Color.dark_gray(),
                                    timestamp=datetime.now(timezone.utc))
        final_embed.add_field(name="Time", value=now_ts())
        await interaction.channel.send(embed=final_embed)

        # remove mapping
        ch_id = interaction.channel.id
        removed_uid = None
        for uid, cid in list(data.get("tickets", {}).items()):
            if cid == ch_id:
                removed_uid = uid
                break
        if removed_uid:
            data["tickets"].pop(removed_uid, None)
            save_data(data)
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")
        except Exception:
            pass

# -------------------------
# Create ticket channel helper
# -------------------------
async def create_ticket_channel(guild: discord.Guild, user: discord.User):
    cat = guild.get_channel(data.get("category_id")) if data.get("category_id") else None
    staff_role = guild.get_role(data.get("staff_role_id")) if data.get("staff_role_id") else None

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    }
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    channel_name = f"ticket-{user.id}"
    if cat and isinstance(cat, discord.CategoryChannel):
        ch = await cat.create_text_channel(channel_name, overwrites=overwrites, reason="Modmail ticket created")
    else:
        ch = await guild.create_text_channel(channel_name, overwrites=overwrites, reason="Modmail ticket created")

    # save mapping
    data["tickets"][str(user.id)] = ch.id
    save_data(data)
    return ch

# -------------------------
# Message handler (DM -> ticket, staff keyword handling)
# -------------------------
@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)  # ensure commands still work

    if message.author.bot:
        return

    # 1) DMs: create or forward ticket
    if isinstance(message.channel, discord.DMChannel):
        user = message.author
        # account age check
        acct_days = (datetime.now(timezone.utc) - user.created_at).days
        if acct_days < 35:
            try:
                await user.send("❌ You cannot open a ticket — your Discord account is under 5 weeks old.")
            except Exception:
                pass
            return

        guild = bot.get_guild(PRIMARY_GUILD_ID)
        if not guild:
            await try_dm(user, "❌ Support is unavailable right now. Try again later.")
            return

        uid = str(user.id)
        ch_id = data.get("tickets", {}).get(uid)
        channel = guild.get_channel(ch_id) if ch_id else None
        if not channel:
            channel = await create_ticket_channel(guild, user)
            embed = discord.Embed(title="📩 New Ticket",
                                  description=f"Ticket created by {user.mention} ({user.id})",
                                  color=discord.Color.blurple(),
                                  timestamp=datetime.now(timezone.utc))
            embed.add_field(name="Account Age (days)", value=str(acct_days), inline=False)
            embed.add_field(name="Created", value=now_ts(), inline=False)
            try:
                await channel.send(embed=embed, view=TicketView(ticket_user_id=user.id))
            except Exception:
                await channel.send(embed=embed)
            try:
                await user.send(f"✅ Your ticket has been created in **{guild.name}**. Staff will respond there.")
            except Exception:
                pass

        # forward message content and attachments
        forward = discord.Embed(title=f"📨 Message from {user}", description=message.content or "[attachment]", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
        forward.set_footer(text=f"User ID: {user.id} • {now_ts()}")
        files = []
        for att in message.attachments:
            try:
                files.append(await att.to_file())
            except Exception:
                pass
        try:
            if files:
                await channel.send(embed=forward, files=files)
            else:
                await channel.send(embed=forward)
        except Exception:
            pass
        return

    # 2) In-guild messages — check if in ticket channel and staff typed a keyword
    if message.guild and message.guild.id == PRIMARY_GUILD_ID:
        ch_id = message.channel.id
        if ch_id in list(data.get("tickets", {}).values()):
            member = message.author
            # ensure member is staff
            if not is_staff(member) and not member.guild_permissions.administrator:
                return
            content = (message.content or "").strip().lower()
            solve_kw = (data.get("solve_keyword") or "solved").lower().strip()
            close_kw = (data.get("close_keyword") or "close").lower().strip()
            matched_solve = (content == solve_kw) or content.startswith(solve_kw)
            matched_close = (content == close_kw) or content.startswith(close_kw)
            if matched_solve:
                embed = discord.Embed(title="✅ Problem Marked Solved", description=f"Marked solved by {member.mention}", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
                embed.add_field(name="Time", value=now_ts())
                await message.channel.send(embed=embed)
                try:
                    await message.delete()
                except Exception:
                    pass
            elif matched_close:
                embed = discord.Embed(title="🗑️ Ticket Closed", description=f"Closed by {member.mention}", color=discord.Color.dark_gray(), timestamp=datetime.now(timezone.utc))
                embed.add_field(name="Time", value=now_ts())
                await message.channel.send(embed=embed)
                # remove mapping then delete channel after short delay
                to_remove = None
                for uid, cid in list(data.get("tickets", {}).items()):
                    if cid == ch_id:
                        to_remove = uid
                        break
                if to_remove:
                    data["tickets"].pop(to_remove, None)
                    save_data(data)
                await asyncio.sleep(3)
                try:
                    await message.channel.delete(reason=f"Closed by keyword by {member}")
                except Exception:
                    pass

# -------------------------
# Slash commands (registered to primary guild at startup)
# -------------------------
@bot.tree.command(name="set_category", description="Set the category where ticket channels will be created (admin only).")
async def set_category(interaction: discord.Interaction, category: discord.CategoryChannel):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❎ You must be an administrator.", ephemeral=True)
    data["category_id"] = category.id
    save_data(data)
    await interaction.response.send_message(f"✅ Category set to **{category.name}** — {now_ts()}", ephemeral=True)

@bot.tree.command(name="set_staff_role", description="Set the staff role for ticket viewing (admin only).")
async def set_staff_role(interaction: discord.Interaction, role: discord.Role):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❎ You must be an administrator.", ephemeral=True)
    data["staff_role_id"] = role.id
    save_data(data)
    await interaction.response.send_message(f"✅ Staff role set to **{role.name}** — {now_ts()}", ephemeral=True)

@bot.tree.command(name="set_solve_keyword", description="Set the keyword staff can type to mark a ticket solved (admin only).")
async def set_solve_keyword(interaction: discord.Interaction, keyword: str):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❎ Admins only.", ephemeral=True)
    data["solve_keyword"] = keyword.strip()
    save_data(data)
    await interaction.response.send_message(f"✅ Solve keyword set to `{keyword}` — {now_ts()}", ephemeral=True)

@bot.tree.command(name="set_close_keyword", description="Set the keyword staff can type to close a ticket (deletes channel).")
async def set_close_keyword(interaction: discord.Interaction, keyword: str):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❎ Admins only.", ephemeral=True)
    data["close_keyword"] = keyword.strip()
    save_data(data)
    await interaction.response.send_message(f"✅ Close keyword set to `{keyword}` — {now_ts()}", ephemeral=True)

@bot.tree.command(name="settings", description="Show current modmail settings (admins only).")
async def settings(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❎ Admins only.", ephemeral=True)
    guild = bot.get_guild(PRIMARY_GUILD_ID)
    category = guild.get_channel(data.get("category_id")) if data.get("category_id") else None
    staff_role = guild.get_role(data.get("staff_role_id")) if data.get("staff_role_id") else None
    embed = discord.Embed(title="⚙️ Modmail Settings", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Category", value=(f"{category.name} ({category.id})" if category else "❎ Not set"), inline=False)
    embed.add_field(name="Staff Role", value=(f"{staff_role.name} ({staff_role.id})" if staff_role else "❎ Not set"), inline=False)
    embed.add_field(name="Solve Keyword", value=f"`{data.get('solve_keyword')}`", inline=True)
    embed.add_field(name="Close Keyword", value=f"`{data.get('close_keyword')}`", inline=True)
    embed.add_field(name="Primary Guild ID", value=str(PRIMARY_GUILD_ID), inline=False)
    embed.add_field(name="OpenAI configured", value=("✅ Yes" if OPENAI_KEY and openai else "❎ No"), inline=False)
    embed.set_footer(text=f"Requested by {interaction.user} • {now_ts()}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="force_close", description="Force close this ticket (staff/admin only; deletes channel).")
async def force_close(interaction: discord.Interaction):
    ch = interaction.channel
    if not isinstance(ch, discord.TextChannel):
        return await interaction.response.send_message("❎ This command must be used in a ticket channel.", ephemeral=True)
    if not is_staff(interaction.user) and not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❎ Staff only.", ephemeral=True)
    await interaction.response.send_message("✅ Force closing and deleting channel in 4s...", ephemeral=True)
    ch_id = ch.id
    to_remove = None
    for uid, cid in list(data.get("tickets", {}).items()):
        if cid == ch_id:
            to_remove = uid
            break
    if to_remove:
        data["tickets"].pop(to_remove, None)
        save_data(data)
    await asyncio.sleep(4)
    try:
        await ch.delete(reason=f"Force-closed by {interaction.user}")
    except Exception:
        pass

# -------------------------
# Owner-only utilities
# -------------------------
@bot.tree.command(name="restart_bot", description="Restart the bot process (owner only).")
async def restart_bot(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("❎ Owner only.", ephemeral=True)
    await interaction.response.send_message("✅ Restarting bot now...", ephemeral=True)
    # small delay to ensure response is delivered
    await asyncio.sleep(1)
    # replace the process
    try:
        python = sys.executable
        os.execv(python, [python] + sys.argv)
    except Exception as e:
        # fallback: exit and rely on host to restart
        await interaction.followup.send(f"❎ Restart error: {e}. Exiting; please restart the service.", ephemeral=True)
        sys.exit(0)

@bot.tree.command(name="refresh_commands", description="Refresh/sync slash commands for primary guild (owner only).")
async def refresh_commands(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("❎ Owner only.", ephemeral=True)
    await interaction.response.send_message("🔁 Refreshing commands for primary guild...", ephemeral=True)
    try:
        synced = await bot.tree.sync(guild=discord.Object(id=PRIMARY_GUILD_ID))
        await interaction.followup.send(f"✅ Synced {len(synced)} slash commands to primary guild. — {now_ts()}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❎ Sync error: {e}", ephemeral=True)

# -------------------------
# on_ready: sync and owner DM
# -------------------------
@bot.event
async def on_ready():
    # sync commands to primary guild (fast)
    try:
        synced = await bot.tree.sync(guild=discord.Object(id=PRIMARY_GUILD_ID))
        print(f"✅ Synced {len(synced)} commands to guild {PRIMARY_GUILD_ID}")
    except Exception as e:
        print(f"⚠️ Guild sync failed: {e}. Falling back to global sync...")
        try:
            all_synced = await bot.tree.sync()
            print(f"✅ Globally synced {len(all_synced)} commands")
        except Exception as e2:
            print(f"❌ Global sync failed: {e2}")

    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    # DM owner
    try:
        owner = await bot.fetch_user(OWNER_ID)
        host = os.getenv("HOSTNAME", "unknown-host")
        await owner.send(f"✅ Modmail bot logged in as **{bot.user}** on `{host}` — {now_ts()}")
    except Exception as e:
        print(f"⚠️ Could not DM owner: {e}")

# -------------------------
# Run
# -------------------------
if __name__ == "__main__":
    print("Starting modmail bot...")
    bot.run(DISCORD_TOKEN)
