"""
Discord Bot for Bottom List
Run separately: python discord_bot.py
"""
import os
import sys
import asyncio
import random
import uuid
from io import BytesIO
import aiohttp
import aiofiles
from datetime import datetime, timezone

# Add the site directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Setup Flask app context before importing models
from config import create_app, db
app = create_app()
app.app_context().push()

from auxiliary.settings_helpers import get_setting
from utils.security import decrypt_token
from models import User, Level, Record, LevelSubmission, SongSubmission, SparkyImage, GuessGameScore, ShopItem, MarketListing, Clan
import discord
from discord.ext import commands


# ============ HELPERS ============

def is_mod_or_admin():
    """Check if command author is a mod or admin on the site (by discord_id)."""
    async def predicate(ctx):
        user = User.query.filter_by(discord_id=str(ctx.author.id)).first()
        if user and (user.is_admin or user.is_mod):
            return True
        await ctx.send('\u274c You need to be a moderator or admin to use this command.')
        return False
    return commands.check(predicate)


# ============ CONFIG ============

PREFIX = '!'  # Fixed prefix, read from env if needed

def get_token():
    """Get bot token from database or environment."""
    try:
        enc = get_setting('discord_bot_token')
        if enc:
            tok = decrypt_token(enc)
            if tok:
                return tok.strip()
    except Exception as e:
        print(f'DB token error: {e}')
    
    # Fallback to env var
    env_token = os.environ.get('DISCORD_BOT_TOKEN', '').strip()
    return env_token


# ============ SETUP BOT ============

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True

bot = commands.Bot(
    command_prefix=PREFIX,
    intents=intents,
    help_command=None  # Disable default help
)


# ============ EVENTS ============

@bot.event
async def on_ready():
    print(f'Bot ready: {bot.user.name}#{bot.user.discriminator}')
    print(f'Bot ID: {bot.user.id}')
    print(f'Connected to {len(bot.guilds)} guilds')
    print(f'Commands: {[c.name for c in bot.commands]}')
    await bot.change_presence(activity=discord.Game(name='!help'))


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return  # Silently ignore unknown commands
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f'\u26a0\ufe0f Missing argument: {error.param.name}\nUsage: `{PREFIX}{ctx.command.name} {ctx.command.signature}`')
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send(f'\u26a0\ufe0f Bad argument: {error}')
        return
    
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(f'\u26a0\ufe0f I don\'t have permission to do that in this channel.')
        return
    if isinstance(error, commands.BotMissingPermissions):
        await ctx.send(f'\u26a0\ufe0f I\'m missing required permissions. Please check my role settings.')
        return
    if isinstance(error, discord.Forbidden):
        print(f'Forbidden in {ctx.command}: {error}')
        return
    
    print(f'Error in {ctx.command}: {error}')
    try:
        await ctx.send(f'\u274c An error occurred. Please try again.')
    except discord.Forbidden:
        pass


# ============ COMMANDS ============

@bot.command()
async def ping(ctx):
    """Test bot responsiveness"""
    await ctx.send('\U0001f3d3 Pong!')


@bot.command()
async def help(ctx):
    """Show available commands"""
    embed = discord.Embed(
        title='Bottom List Bot Commands',
        color=0x3498db,
        description='All commands use the `!` prefix'
    )

    # Info
    info_cmds = [
        ('!search <name_or_id>', 'Search for a level by name or GD level ID'),
        ('!level <id>', 'Get level details by internal ID'),
        ('!user <username>', 'Get user stats'),
        ('!top [list_type] [n]', 'Show leaderboard (main/challenge/platformer/all)'),
        ('!recent [username]', 'Show recent approved records'),
        ('!recommended [list_type]', 'Show recommended level'),
        ('!counts', 'Show total stats for levels, records, users'),
        ('!listinfo [list_type]', 'Show per-list breakdown'),
        ('!random', 'Get a random verified level'),
        ('!submit <level_id>', 'Submit a song (attach mp3/ogg file)'),
    ]
    embed.add_field(name='\U0001f4ca Info & Levels', value='\n'.join(f'`{c}` \u2014 {d}' for c, d in info_cmds), inline=False)

    # Social / Fun
    social_cmds = [
        ('!profile [username]', 'Show rich profile card'),
        ('!daily', 'Claim daily BCoins reward'),
        ('!bcoin', 'Check your BCoins balance'),
        ('!shop', 'Browse the shop'),
        ('!8ball <question>', 'Ask the magic 8-ball'),
        ('!rate <thing>', 'Rate something out of 10'),
        ('!avatar [username]', 'Get a Discord avatar'),
        ('!echo <text>', 'Make the bot repeat your message'),
    ]
    embed.add_field(name='\U0001f3ae Social & Fun', value='\n'.join(f'`{c}` \u2014 {d}' for c, d in social_cmds), inline=False)

    # Games
    game_cmds = [
        ('!sparky', 'Guess the level from a sparky image'),
        ('!leaderboard [n]', 'Show top guessers'),
        ('!giveaway <min> <prize>', 'Start a giveaway with timer'),
    ]
    embed.add_field(name='\U0001f3ae Games', value='\n'.join(f'`{c}` \u2014 {d}' for c, d in game_cmds), inline=False)

    # Moderation
    mod_cmds = [
        ('!pending', 'Show pending song submissions'),
        ('!approve <id>', 'Approve a song submission'),
        ('!reject <id> [reason]', 'Reject a song submission'),
        ('!setpos <level_id> <pos>', 'Set a level position'),
    ]
    embed.add_field(name='\U0001f6e1\ufe0f Moderation (mod only)', value='\n'.join(f'`{c}` \u2014 {d}' for c, d in mod_cmds), inline=False)

    await ctx.send(embed=embed)


@bot.command(usage='<level_id>')
async def level(ctx, level_id: str):
    """Get level details by internal ID"""
    try:
        lid = int(level_id)
    except ValueError:
        await ctx.send('\u26a0\ufe0f Level ID must be a number.')
        return
    
    lv = db.session.get(Level, lid)
    if not lv:
        await ctx.send('\u274c Level not found.')
        return
    
    pos = getattr(lv, 'position', None)
    pos_str = f'#{pos}' if pos else 'Unverified'
    pts = getattr(lv, 'points_reward', 0.0) or 0.0
    
    embed = discord.Embed(
        title=lv.name,
        color=0xe74c3c,
        description=f'by {lv.creator}'
    )
    embed.add_field(name='ID', value=f'`{lv.id}`', inline=True)
    embed.add_field(name='Position', value=pos_str, inline=True)
    embed.add_field(name='List', value=lv.list_type or 'N/A', inline=True)
    embed.add_field(name='Points', value=f'{pts:.1f}', inline=True)
    if lv.gd_level_id:
        embed.add_field(name='GD Level ID', value=f'`{lv.gd_level_id}`', inline=True)
    if lv.video_url:
        embed.add_field(name='Video', value=lv.video_url[:100], inline=False)
    
    await ctx.send(embed=embed)


@bot.command(usage='<username>')
async def user(ctx, *, username: str):
    """Get user stats"""
    u = (username or '').strip()
    if not u:
        await ctx.send('\u26a0\ufe0f Usage: `!user <username>`')
        return
    
    user = User.query.filter(db.func.lower(User.username) == u.lower()).first()
    if not user:
        await ctx.send(f'\u274c User "{username}" not found.')
        return
    
    approved = Record.query.filter(Record.user_id == user.id, Record.status == 'approved').count()
    total = Record.query.filter(Record.user_id == user.id).count()
    subs = LevelSubmission.query.filter(LevelSubmission.user_id == user.id).count()
    pts = getattr(user, 'points', 0.0) or 0.0
    
    embed = discord.Embed(
        title=user.username,
        color=0x9b59b6,
        description=f'Points: **{pts:.1f}**'
    )
    embed.add_field(name='Records', value=f'{approved}/{total} approved', inline=True)
    embed.add_field(name='Submissions', value=str(subs), inline=True)
    
    await ctx.send(embed=embed)


@bot.command(usage='[list_type] [count]')
async def top(ctx, list_type: str = 'main', n: int = 10):
    """Show leaderboard. Types: main, challenge, platformer, all"""
    lt = list_type.lower()
    if lt not in ('main', 'challenge', 'platformer', 'all'):
        await ctx.send('\u26a0\ufe0f List type must be: main, challenge, platformer, or all')
        return
    
    if n < 1 or n > 25:
        await ctx.send('\u26a0\ufe0f Count must be between 1 and 25')
        return
    
    from sqlalchemy import case
    
    record_join = db.and_(Record.user_id == User.id, Record.status == 'approved')
    level_join = db.and_(Level.id == Record.level_id)
    if lt != 'all':
        level_join = db.and_(level_join, Level.list_type == lt)
    
    points_expr = db.func.sum(case((Level.points_reward.isnot(None), Level.points_reward), else_=0.0))
    
    q = (
        db.session.query(User, points_expr.label('lb_points'))
        .outerjoin(Record, record_join)
        .outerjoin(Level, level_join)
        .group_by(User.id)
        .order_by(db.desc('lb_points'))
        .limit(n)
    )
    
    rows = q.all()
    lines = []
    rank = 1
    for user, pts in rows:
        if float(pts or 0.0) <= 0:
            continue
        lines.append(f'{rank}. {user.username} - {pts:.1f}')
        rank += 1
    
    if not lines:
        await ctx.send('No data available.')
        return
    
    title = f'Top {lt.capitalize() if lt != "all" else "Overall"}'
    await ctx.send(f'**{title}**\n' + '\n'.join(lines))


@bot.command(usage='[username]')
async def recent(ctx, *, username: str = ''):
    """Show recent approved records"""
    u = (username or '').strip()
    
    if u:
        user = User.query.filter(db.func.lower(User.username) == u.lower()).first()
        if not user:
            await ctx.send(f'\u274c User "{username}" not found.')
            return
        uid = user.id
        title = f'Recent for {user.username}'
    else:
        uid = None
        title = 'Recent Approved Records'
    
    q = (
        db.session.query(Record, Level, User)
        .join(Level, Level.id == Record.level_id)
        .join(User, User.id == Record.user_id)
        .filter(Record.status == 'approved')
        .order_by(Record.submitted_at.desc())
        .limit(10)
    )
    if uid:
        q = q.filter(Record.user_id == uid)
    
    rows = q.all()
    lines = []
    for r, lv, user in rows:
        try:
            d = r.submitted_at.strftime('%Y-%m-%d')
        except:
            d = '?'
        lines.append(f'\u2022 {user.username}: {lv.name} ({d})')
    
    if not lines:
        await ctx.send('No records found.')
        return
    
    await ctx.send(f'**{title}**\n' + '\n'.join(lines))


@bot.command(usage='[list_type]')
async def recommended(ctx, list_type: str = 'main'):
    """Show recommended level to beat"""
    lt = list_type.lower()
    if lt not in ('main', 'challenge', 'platformer'):
        await ctx.send('\u26a0\ufe0f List type must be: main, challenge, or platformer')
        return
    
    lv = (
        Level.query
        .filter(Level.is_verified == True, Level.list_type == lt, Level.position.isnot(None))
        .order_by(Level.position.asc())
        .first()
    )
    
    if not lv:
        await ctx.send(f'No recommended {lt} level found.')
        return
    
    await ctx.send(f'\u2b50 Recommended ({lt}): **{lv.name}** by {lv.creator} | Position: #{lv.position}')


@bot.command()
async def counts(ctx):
    """Show total counts for levels, records, users, and songs"""
    total_levels = Level.query.count()
    verified = Level.query.filter_by(is_verified=True).count()
    total_records = Record.query.filter_by(status='approved').count()
    pending_records = Record.query.filter_by(status='pending').count()
    total_users = User.query.count()
    pending_songs = SongSubmission.query.filter_by(status='pending').count()
    total_clans = Clan.query.count()

    embed = discord.Embed(title='Bottom List Stats', color=0x3498db)
    embed.add_field(name='\U0001f3ae Levels', value=f'{total_levels} total\n{verified} verified', inline=True)
    embed.add_field(name='\U0001f3c5 Records', value=f'{total_records} approved\n{pending_records} pending', inline=True)
    embed.add_field(name='\U0001f465 Users', value=str(total_users), inline=True)
    embed.add_field(name='\U0001f3b5 Songs', value=f'{pending_songs} pending review', inline=True)
    embed.add_field(name='\U0001f451 Clans', value=str(total_clans), inline=True)
    await ctx.send(embed=embed)


@bot.command()
async def listinfo(ctx, list_type: str = 'all'):
    """Show per-list stats. Types: main, challenge, platformer, all"""
    lt = list_type.lower()
    if lt not in ('main', 'challenge', 'platformer', 'all'):
        await ctx.send('\u26a0\ufe0f List type must be: main, challenge, platformer, or all')
        return

    embed = discord.Embed(title='List Info', color=0xe67e22)

    types = ['main', 'challenge', 'platformer'] if lt == 'all' else [lt]
    for t in types:
        total = Level.query.filter_by(list_type=t).count()
        verified = Level.query.filter_by(list_type=t, is_verified=True).count
        records = (
            db.session.query(Record)
            .join(Level, Level.id == Record.level_id)
            .filter(Level.list_type == t, Record.status == 'approved')
            .count()
        )
        embed.add_field(
            name=f'{t.capitalize()}',
            value=f'{total} levels ({verified} verified)\n{records} records',
            inline=True
        )

    await ctx.send(embed=embed)


@bot.command()
async def random(ctx):
    """Get a random verified level"""
    lv = (
        Level.query
        .filter_by(is_verified=True)
        .order_by(db.func.random())
        .first()
    )
    if not lv:
        await ctx.send('\u274c No verified levels found.')
        return

    pos_str = f'#{lv.position}' if lv.position else 'Unverified'
    pts = getattr(lv, 'points_reward', 0.0) or 0.0

    embed = discord.Embed(
        title=lv.name,
        color=0x1abc9c,
        description=f'by {lv.creator}'
    )
    embed.add_field(name='Position', value=pos_str, inline=True)
    embed.add_field(name='List', value=lv.list_type or 'N/A', inline=True)
    embed.add_field(name='Points', value=f'{pts:.1f}', inline=True)
    if lv.gd_level_id:
        embed.add_field(name='GD Level ID', value=f'`{lv.gd_level_id}`', inline=True)
    if lv.video_url:
        embed.add_field(name='Video', value=lv.video_url[:100], inline=False)

    embed.set_footer(text=f'Internal ID: {lv.id}')
    await ctx.send(embed=embed)


@bot.command(usage='<name_or_id>')
async def search(ctx, *, query: str):
    """Search for a level by name or GD level ID"""
    q = (query or '').strip()
    if not q:
        await ctx.send('\u26a0\ufe0f Usage: `!search <name_or_id>`')
        return
    
    # Try GD level ID first
    level = None
    if q.isdigit():
        level = Level.query.filter_by(gd_level_id=q).first()
    
    # Search by name
    if not level:
        level = Level.query.filter(db.func.lower(Level.name).like(f'%{q.lower()}%')).first()
    
    # Search by creator
    if not level:
        level = Level.query.filter(db.func.lower(Level.creator).like(f'%{q.lower()}%')).first()
    
    if not level:
        await ctx.send(f'\u274c No level found matching "{q}".')
        return
    
    pos_str = f'#{level.position}' if level.position else 'Unverified'
    
    embed = discord.Embed(
        title=level.name,
        color=0x2ecc71,
        description=f'by {level.creator}'
    )
    embed.add_field(name='Internal ID', value=f'`{level.id}`', inline=True)
    if level.gd_level_id:
        embed.add_field(name='GD Level ID', value=f'`{level.gd_level_id}`', inline=True)
    embed.add_field(name='Position', value=pos_str, inline=True)
    embed.add_field(name='List', value=level.list_type or 'N/A', inline=True)
    
    embed.set_footer(text=f'Use !submit {level.id} to submit a song')
    await ctx.send(embed=embed)


@bot.command(usage='(level_id) [song_name] (song_id)')
async def submit(ctx, *, args: str = ''):
    """Submit a song for a level. Format: !submit (level_id) [song_name] (song_id). Attach mp3/ogg file."""
    args = (args or '').strip()
    if not args:
        await ctx.send('\u26a0\ufe0f Usage: `!submit (level_id) [song_name] (song_id)` (attach an audio file)\nExample: `!submit (12345) [Cool Song Mashup] (67890)`\n- `level_id`: The Geometry Dash level ID\n- `song_name`: Name of your song (required)\n- `song_id`: Newgrounds or official song ID to replace (required)')
        return
    
    # Parse the format: (level_id) [song_name] (song_id)
    import re
    level_id_match = re.search(r'\((\d+)\)', args)
    song_name_match = re.search(r'\[([^\]]+)\]', args)
    
    # Find all numbers in parentheses - the second one should be song_id
    all_paren_nums = re.findall(r'\((\d+)\)', args)
    
    if not level_id_match:
        await ctx.send('\u26a0\ufe0f Please provide level ID in parentheses. Example: `!submit (12345) [My Song] (67890)`')
        return
    
    lid = level_id_match.group(1)
    song_name = song_name_match.group(1) if song_name_match else None
    
    # Song name is required
    if not song_name:
        await ctx.send('\u26a0\ufe0f Song name is **required**. Use format: `!submit (level_id) [song_name] (song_id)`\nExample: `!submit (12345) [My Awesome Song] (67890)`')
        return
    
    # Song ID is required - look for a second number in parentheses
    song_id = None
    if len(all_paren_nums) >= 2:
        # The second number in parentheses is the song ID
        song_id = all_paren_nums[1]
    
    if not song_id:
        await ctx.send('\u26a0\ufe0f Song ID is **required**. This is the Newgrounds song ID or official song ID for your submission.\nFormat: `!submit (level_id) [song_name] (song_id)`\nExample: `!submit (12345) [My Song] (67890)`')
        return
    
    if not ctx.message.attachments:
        await ctx.send('\u26a0\ufe0f Please attach an audio file (mp3/ogg, max 10MB).')
        return
    
    if len(ctx.message.attachments) > 10:
        await ctx.send('\u26a0\ufe0f You can only submit up to 10 files at once.')
        return
    
    with app.app_context():
        # Find level
        level = None
        if lid.isdigit():
            level = db.session.get(Level, int(lid))
        if not level:
            level = Level.query.filter_by(gd_level_id=lid).first()
        
        if not level:
            await ctx.send(f'\u274c Level not found: `{lid}`. Use `!search` first.')
            return
        
        # Check pending count
        pending_count = SongSubmission.query.filter_by(level_id=level.id, status='pending').count()
        if pending_count >= 10:
            await ctx.send('\u26a0\ufe0f This level already has 10 pending submissions. Please wait for moderation.')
            return
        
        # Process files
        saved_files = []
        errors = []
        
        for attachment in ctx.message.attachments:
            filename = attachment.filename.lower()
            if not (filename.endswith('.mp3') or filename.endswith('.ogg')):
                errors.append(f'{attachment.filename}: Only mp3/ogg supported')
                continue
            
            if attachment.size > 10 * 1024 * 1024:
                errors.append(f'{attachment.filename}: File too large (max 10MB)')
                continue
            
            unique = f"{uuid.uuid4().hex}_{attachment.filename}"
            save_path = os.path.join(app.root_path, 'static', 'pending_songs', unique)
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(attachment.url) as resp:
                        if resp.status == 200:
                            async with aiofiles.open(save_path, 'wb') as f:
                                await f.write(await resp.read())
                            
                            sub = SongSubmission(
                                gd_level_id=level.gd_level_id,
                                level_id=level.id,
                                discord_user_id=str(ctx.author.id),
                                discord_username=str(ctx.author),
                                song_name=song_name,
                                song_id=song_id,
                                song_filename=unique,
                                original_filename=attachment.filename,
                                file_size=attachment.size,
                                status='pending'
                            )
                            db.session.add(sub)
                            saved_files.append(attachment.filename)
                        else:
                            errors.append(f'{attachment.filename}: Download failed (HTTP {resp.status})')
            except Exception as e:
                errors.append(f'{attachment.filename}: {str(e)[:50]}')
        
        if saved_files:
            db.session.commit()
            msg = f'\u2705 Submitted **{len(saved_files)}** song(s) for **{level.name}**!'
            if song_name:
                msg += f'\n\U0001f3b5 Song name: `{song_name}`'
            msg += f'\n\U0001f194 Song ID: `{song_id}` (replaces this song)'
            msg += '\nA moderator will review your submission.'
            if errors:
                msg += f'\n\u26a0\ufe0f Skipped: {", ".join(errors)}'
            await ctx.send(msg)
        else:
            await ctx.send('\u274c No files were submitted. ' + (f'Errors: {", ".join(errors)}' if errors else ''))


# ── Sparky commands / GeoGuessr ──────────────────────────────────────────────────

active_games = {}  # channel_id -> game state

async def _start_round(channel):
    """Start a sparky guessing round in the given channel."""
    if channel.id in active_games:
        return

    img = (
        SparkyImage.query
        .filter(SparkyImage.is_approved == True, SparkyImage.level_id.isnot(None))
        .order_by(db.func.random())
        .first()
    )
    if not img:
        await channel.send('\u274c No sparky images linked to a level available.')
        return

    level = db.session.get(Level, img.level_id)
    if not level:
        await channel.send('\u274c Linked level not found.')
        return

    game = {
        'id': str(uuid.uuid4()),
        'image_id': img.id,
        'level_id': level.id,
        'level_name': level.name.lower().strip(),
        'level_display': level.name,
        'channel_id': channel.id,
        'started_at': asyncio.get_event_loop().time(),
        'timed_out': False,
    }
    active_games[channel.id] = game

    async def timer():
        gid = game['id']
        await asyncio.sleep(30)
        g = active_games.get(channel.id)
        if g is not None and g.get('id') == gid and not g.get('done', False):
            g['timed_out'] = True
            active_games.pop(channel.id, None)
            await channel.send('\u23f0 Time\'s up! Nobody guessed in time.')
            view = PlayAgainView(channel)
            await channel.send('Play again?', view=view)

    asyncio.create_task(timer())

    file_path = os.path.join(app.root_path, 'static', 'sparky', img.filename)
    embed = discord.Embed(
        title='\U0001f50d Guess the Level!',
        color=0xf1c40f,
        description='Which level does this sparky image represent? First person to type the level name in chat wins!\nYou have **30 seconds**!'
    )
    if os.path.exists(file_path):
        async with aiofiles.open(file_path, 'rb') as f:
            data = await f.read()
        file = discord.File(fp=BytesIO(data), filename=img.filename)
        embed.set_image(url=f'attachment://{img.filename}')
        embed.set_footer(text='30 seconds on the clock!')
        await channel.send(file=file, embed=embed)
    else:
        await channel.send('\u274c Image file not found. Cancelling round.')
        active_games.pop(channel.id, None)


class PlayAgainView(discord.ui.View):
    def __init__(self, channel):
        super().__init__(timeout=30)
        self.channel = channel

    @discord.ui.button(label='Play Again', style=discord.ButtonStyle.primary)
    async def play_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.channel.id != self.channel.id:
            await interaction.response.send_message('This button is for another channel.', ephemeral=True)
            return
        button.disabled = True
        await interaction.response.edit_message(view=self)
        await _start_round(self.channel)


@bot.command()
async def sparky(ctx):
    """Start a guessing round — bot shows a sparky image, first to guess the level wins points!"""
    await _start_round(ctx.channel)


@bot.command(usage='[n]')
async def leaderboard(ctx, n: int = 10):
    """Show top guessers"""
    if n < 1 or n > 50:
        n = 10
    rows = (
        GuessGameScore.query
        .order_by(GuessGameScore.score.desc())
        .limit(n)
        .all()
    )
    if not rows:
        await ctx.send('\u274c No scores yet. Play with `!sparky`!')
        return
    lines = [f'{i+1}. **{row.discord_username or row.discord_user_id}** — {row.score} point{"s" if row.score != 1 else ""}' for i, row in enumerate(rows)]
    embed = discord.Embed(title='\U0001f3c6 Guess Leaderboard', color=0xf1c40f, description='\n'.join(lines))
    await ctx.send(embed=embed)


# ── Social / Fun commands ─────────────────────────────────────────────────────

@bot.command(usage='<question>')
async def eightball(ctx, *, question: str = ''):
    """Ask the magic 8-ball a question"""
    if not question:
        await ctx.send('\u26a0\ufe0f Usage: `!8ball <your question>`')
        return

    responses = [
        '\u2705 It is certain.',
        '\u2705 It is decidedly so.',
        '\u2705 Without a doubt.',
        '\u2705 Yes \u2014 definitely.',
        '\u2705 You may rely on it.',
        '\U0001f52c Reply hazy, try again.',
        '\U0001f52c Ask again later.',
        '\U0001f52c Better not tell you now.',
        '\u274c Don\'t count on it.',
        '\u274c My reply is no.',
        '\u274c My sources say no.',
        '\u274c Outlook not so good.',
        '\u274c Very doubtful.',
    ]

    embed = discord.Embed(title='\U0001f52e Magic 8-Ball', color=0x8e44ad)
    embed.add_field(name='Question', value=question[:200], inline=False)
    embed.add_field(name='Answer', value=random.choice(responses), inline=False)
    await ctx.send(embed=embed)


@bot.command(usage='<thing>')
async def rate(ctx, *, thing: str = ''):
    """Rate something out of 10"""
    if not thing:
        await ctx.send('\u26a0\ufe0f Usage: `!rate <something>`')
        return

    score = hash(thing + str(datetime.now(timezone.utc).date())) % 11
    stars = '\u2b50' * min(score, 10) + '\u2606' * max(0, 10 - score)
    embed = discord.Embed(title='\U0001f4ca Rate', color=0xe91e63)
    embed.add_field(name=thing[:100], value=f'{stars}\n**{score}/10**', inline=False)
    await ctx.send(embed=embed)


@bot.command(usage='<username>')
async def profile(ctx, *, username: str = ''):
    """Show a user's rich profile card"""
    u = (username or '').strip()

    if u:
        user = User.query.filter(db.func.lower(User.username) == u.lower()).first()
    else:
        # Look up by Discord ID
        user = User.query.filter_by(discord_id=str(ctx.author.id)).first()

    if not user:
        await ctx.send(f'\u274c User "{username or ctx.author.name}" not found. Link your Discord account on the site first.')
        return

    approved = Record.query.filter(Record.user_id == user.id, Record.status == 'approved').count()
    pts = getattr(user, 'points', 0.0) or 0.0
    bcoins = getattr(user, 'bcoins', 0) or 0
    rank = getattr(user, 'rank', 'member')
    clan = user.clan
    subs = LevelSubmission.query.filter(LevelSubmission.user_id == user.id).count()

    embed = discord.Embed(
        title=user.username,
        color=0x9b59b6,
        description=f'Rank: **{rank}**'
    )
    embed.add_field(name='\u2b50 Points', value=f'{pts:.1f}', inline=True)
    embed.add_field(name='\U0001f4b0 BCoins', value=str(bcoins), inline=True)
    embed.add_field(name='\U0001f3c5 Records', value=str(approved), inline=True)
    embed.add_field(name='\U0001f4e6 Submissions', value=str(subs), inline=True)
    if clan:
        embed.add_field(name='\U0001f451 Clan', value=clan.name, inline=True)
    if user.youtube_link:
        embed.add_field(name='\U0001f3ac YouTube', value=user.youtube_link[:80], inline=False)
    if user.bio:
        embed.add_field(name='\U0001f4dd Bio', value=user.bio[:200], inline=False)

    await ctx.send(embed=embed)


@bot.command()
async def daily(ctx):
    """Claim your daily BCoins reward (once per day)"""
    user = User.query.filter_by(discord_id=str(ctx.author.id)).first()
    if not user:
        await ctx.send('\u274c Link your Discord account on the site first!')
        return

    now = datetime.now(timezone.utc)
    last = user.last_wheel_spin_at
    if last and (now - last).total_seconds() < 86400:
        remaining = 86400 - (now - last).total_seconds()
        hours = int(remaining // 3600)
        mins = int((remaining % 3600) // 60)
        await ctx.send(f'\u23f0 You already claimed today! Come back in **{hours}h {mins}m**.')
        return

    reward = random.randint(5, 25)
    user.bcoins = (user.bcoins or 0) + reward
    user.total_bcoins_earned = (user.total_bcoins_earned or 0) + reward
    user.last_wheel_spin_at = now
    db.session.commit()

    embed = discord.Embed(
        title='\U0001f381 Daily Reward!',
        color=0xf39c12,
        description=f'You received **{reward} BCoins**!\nBalance: **{user.bcoins} BCoins**'
    )
    await ctx.send(embed=embed)


@bot.command()
async def bcoin(ctx):
    """Check your BCoins balance"""
    user = User.query.filter_by(discord_id=str(ctx.author.id)).first()
    if not user:
        await ctx.send('\u274c Link your Discord account on the site first!')
        return

    bal = getattr(user, 'bcoins', 0) or 0
    total = getattr(user, 'total_bcoins_earned', 0) or 0
    embed = discord.Embed(title='\U0001f4b0 Your BCoins', color=0xf39c12)
    embed.add_field(name='Balance', value=str(bal), inline=True)
    embed.add_field(name='Total Earned', value=str(total), inline=True)
    await ctx.send(embed=embed)


@bot.command()
async def shop(ctx):
    """Browse the shop items"""
    items = ShopItem.query.filter_by(is_available=True).order_by(ShopItem.price.asc()).limit(15).all()
    if not items:
        await ctx.send('\u274c No items in the shop right now.')
        return

    lines = []
    for item in items:
        emoji = {'profile_deco': '\U0001f3a8', 'avatar': '\U0001f5bc\ufe0f', 'background': '\U0001f5fe', 'title': '\U0001f3f7\ufe0f'}.get(item.item_type, '\U0001f6cd\ufe0f')
        limited = ' [LIMITED]' if item.is_limited else ''
        lines.append(f'{emoji} **{item.name}** — {item.price} BCoins{limited}')

    embed = discord.Embed(title='\U0001f6d2 Shop', color=0xe74c3c, description='\n'.join(lines))
    embed.set_footer(text='Visit the site to purchase items!')
    await ctx.send(embed=embed)


# ── Moderation commands ───────────────────────────────────────────────────────

@bot.command()
@is_mod_or_admin()
async def pending(ctx):
    """Show pending song submissions (mod only)"""
    subs = (
        SongSubmission.query
        .filter_by(status='pending')
        .order_by(SongSubmission.submitted_at.desc())
        .limit(15)
        .all()
    )
    if not subs:
        await ctx.send('\u2705 No pending song submissions!')
        return

    lines = []
    for s in subs:
        try:
            d = s.submitted_at.strftime('%Y-%m-%d')
        except:
            d = '?'
        lines.append(f'**#{s.id}** — {s.song_name or "N/A"} by `{s.discord_username}` for level `{s.level_id}` ({d})')

    embed = discord.Embed(title='\U0001f4e5 Pending Songs', color=0xe67e22, description='\n'.join(lines))
    embed.set_footer(text='Use !approve <id> or !reject <id>')
    await ctx.send(embed=embed)


@bot.command(usage='<submission_id>')
@is_mod_or_admin()
async def approve(ctx, sub_id: str):
    """Approve a song submission (mod only)"""
    try:
        sid = int(sub_id)
    except ValueError:
        await ctx.send('\u26a0\ufe0f Submission ID must be a number.')
        return

    sub = db.session.get(SongSubmission, sid)
    if not sub:
        await ctx.send('\u274c Submission not found.')
        return
    if sub.status != 'pending':
        await ctx.send(f'\u26a0\ufe0f This submission is already **{sub.status}**.')
        return

    sub.status = 'approved'
    sub.reviewed_at = datetime.now(timezone.utc)
    db.session.commit()

    embed = discord.Embed(title='\u2705 Song Approved', color=0x2ecc71)
    embed.add_field(name='Song', value=sub.song_name or 'N/A', inline=True)
    embed.add_field(name='Level ID', value=str(sub.level_id), inline=True)
    embed.add_field(name='By', value=sub.discord_username or 'Unknown', inline=True)
    await ctx.send(embed=embed)


@bot.command(usage='<submission_id> [reason]')
@is_mod_or_admin()
async def reject(ctx, sub_id: str, *, reason: str = ''):
    """Reject a song submission (mod only)"""
    try:
        sid = int(sub_id)
    except ValueError:
        await ctx.send('\u26a0\ufe0f Submission ID must be a number.')
        return

    sub = db.session.get(SongSubmission, sid)
    if not sub:
        await ctx.send('\u274c Submission not found.')
        return
    if sub.status != 'pending':
        await ctx.send(f'\u26a0\ufe0f This submission is already **{sub.status}**.')
        return

    sub.status = 'rejected'
    sub.reviewed_at = datetime.now(timezone.utc)
    if reason:
        sub.notes = reason
    db.session.commit()

    embed = discord.Embed(title='\u274c Song Rejected', color=0xe74c3c)
    embed.add_field(name='Song', value=sub.song_name or 'N/A', inline=True)
    embed.add_field(name='Level ID', value=str(sub.level_id), inline=True)
    embed.add_field(name='By', value=sub.discord_username or 'Unknown', inline=True)
    if reason:
        embed.add_field(name='Reason', value=reason[:200], inline=False)
    await ctx.send(embed=embed)


@bot.command(usage='<level_id> <position>')
@is_mod_or_admin()
async def setpos(ctx, level_id: str, position: int):
    """Set a level's position on the list (mod only)"""
    try:
        lid = int(level_id)
    except ValueError:
        await ctx.send('\u26a0\ufe0f Level ID must be a number.')
        return

    lv = db.session.get(Level, lid)
    if not lv:
        await ctx.send('\u274c Level not found.')
        return

    lv.position = position
    db.session.commit()
    await ctx.send(f'\u2705 **{lv.name}** moved to position **#{position}**.')


# ── Giveaway ──────────────────────────────────────────────────────────────────

active_giveaways = {}

class GiveawayView(discord.ui.View):
    def __init__(self, giveaway_id, end_time):
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id
        self.end_time = end_time

    @discord.ui.button(label='Enter Giveaway', style=discord.ButtonStyle.success, emoji='\U0001f389')
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        ga = active_giveaways.get(self.giveaway_id)
        if not ga:
            await interaction.response.send_message('This giveaway has ended.', ephemeral=True)
            return
        if interaction.user.id in ga['entries']:
            await interaction.response.send_message('You already entered!', ephemeral=True)
            return
        ga['entries'].add(interaction.user.id)
        await interaction.response.send_message(f'\u2705 Entered! ({len(ga["entries"])} total entries)', ephemeral=True)

    @discord.ui.button(label='End Now', style=discord.ButtonStyle.danger, emoji='\u23f9\ufe0f')
    async def end_early(self, interaction: discord.Interaction, button: discord.ui.Button):
        ga = active_giveaways.get(self.giveaway_id)
        if not ga:
            await interaction.response.send_message('This giveaway has already ended.', ephemeral=True)
            return
        if interaction.user.id != ga['host_id']:
            await interaction.response.send_message('Only the host can end early.', ephemeral=True)
            return
        button.disabled = True
        await interaction.response.edit_message(view=self)
        await _finish_giveaway(ga)


async def _finish_giveaway(ga):
    active_giveaways.pop(ga['id'], None)
    channel = ga['channel']
    entries = ga['entries']

    if not entries:
        try:
            await channel.send('\U0001f389 **Giveaway ended** \u2014 no entries.')
        except discord.Forbidden:
            pass
        return

    winner_id = random.choice(list(entries))
    winner = channel.guild.get_member(winner_id)
    name = winner.mention if winner else f'<@{winner_id}>'

    embed = discord.Embed(
        title='\U0001f389 Giveaway Winner!',
        color=0xf1c40f,
        description=f'**{ga["prize"]}**\n\nWinner: {name}\nEntries: {len(entries)}'
    )
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        pass


@bot.command(usage='<minutes> <prize>')
async def giveaway(ctx, minutes: int = 0, *, prize: str = ''):
    """Start a giveaway. Usage: !giveaway <minutes> <prize>"""
    if minutes < 1 or minutes > 1440:
        await ctx.send('\u26a0\ufe0f Duration must be between 1 and 1440 minutes (24h).')
        return
    if not prize:
        await ctx.send('\u26a0\ufe0f Usage: `!giveaway <minutes> <prize>`')
        return

    ga_id = uuid.uuid4().hex[:8]
    end_time = datetime.now(timezone.utc).timestamp() + minutes * 60

    ga = {
        'id': ga_id,
        'host_id': ctx.author.id,
        'channel': ctx.channel,
        'prize': prize[:200],
        'entries': set(),
        'end_time': end_time,
    }
    active_giveaways[ga_id] = ga

    embed = discord.Embed(
        title='\U0001f389 GIVEAWAY',
        color=0xf1c40f,
        description=f'**{prize}**\n\nHosted by {ctx.author.mention}\nEnds in **{minutes} minutes**\nEntries: **0**'
    )
    embed.set_footer(text=f'Giveaway ID: {ga_id}')
    view = GiveawayView(ga_id, end_time)
    await ctx.send(embed=embed, view=view)

    async def timer():
        await asyncio.sleep(minutes * 60)
        if ga_id in active_giveaways:
            try:
                await _finish_giveaway(ga)
            except discord.Forbidden:
                active_giveaways.pop(ga_id, None)
                return
            except Exception as e:
                print(f'Giveaway finish error: {e}')
                active_giveaways.pop(ga_id, None)
                return
            # Disable buttons
            try:
                view.children[0].disabled = True
                view.children[1].disabled = True
            except Exception:
                pass

    asyncio.create_task(timer())


# ── Misc ──────────────────────────────────────────────────────────────────────

@bot.command(usage='<text>')
async def echo(ctx, *, text: str = ''):
    """Make the bot repeat your message"""
    if not text:
        await ctx.send('\u26a0\ufe0f Usage: `!echo <text>`')
        return
    await ctx.send(text[:1500])


@bot.command()
async def avatar(ctx, *, username: str = ''):
    """Get a user's Discord avatar"""
    if username:
        member = discord.utils.find(lambda m: m.name.lower() == username.lower() or m.display_name.lower() == username.lower(), ctx.guild.members)
    else:
        member = ctx.author

    if not member:
        await ctx.send(f'\u274c User "{username}" not found in this server.')
        return

    embed = discord.Embed(title=f'{member.display_name}\'s Avatar', color=0x3498db)
    embed.set_image(url=member.display_avatar.url)
    await ctx.send(embed=embed)


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    game = active_games.get(message.channel.id)
    if game and not game.get('timed_out') and not game.get('done') and message.content.lower().strip() == game['level_name']:
        game['done'] = True
        active_games.pop(message.channel.id, None)

        score = GuessGameScore.query.filter_by(discord_user_id=str(message.author.id)).first()
        if not score:
            score = GuessGameScore(
                discord_user_id=str(message.author.id),
                discord_username=str(message.author),
                score=0
            )
            db.session.add(score)
        score.score += 1
        score.last_guess_at = datetime.now(timezone.utc)
        db.session.commit()

        level = db.session.get(Level, game['level_id'])
        level_name_display = level.name if level else game['level_name']

        embed = discord.Embed(
            title='\u2705 Correct!',
            color=0x2ecc71,
            description=f'**{message.author.mention}** guessed **{level_name_display}** correctly!'
        )
        embed.add_field(name='Points', value=f'{score.score}', inline=True)
        await message.channel.send(embed=embed)
        view = PlayAgainView(message.channel)
        await message.channel.send('Play again?', view=view)
        return

    await bot.process_commands(message)


# ============ MAIN ============

def main():
    print('=' * 40)
    print('Bottom List Discord Bot')
    print('=' * 40)
    
    # Ensure directories exist
    pending_dir = os.path.join(app.root_path, 'static', 'pending_songs')
    sparky_dir = os.path.join(app.root_path, 'static', 'sparky')
    os.makedirs(pending_dir, exist_ok=True)
    os.makedirs(sparky_dir, exist_ok=True)
    print(f'Directories: {pending_dir}, {sparky_dir}')
    
    # Get token
    token = get_token()
    print(f'Token: {"SET" if token else "NOT SET"}')  # never print token material
    
    if not token:
        print('\n\u274c No bot token found!')
        print('Set it via:')
        print('  1. Database setting: discord_bot_token')
        print('  2. Environment variable: DISCORD_BOT_TOKEN')
        sys.exit(1)
    
    print('\nConnecting to Discord...')
    try:
        bot.run(token)
    except Exception as e:
        print(f'\n\u274c Failed to start: {e}')
        sys.exit(1)


if __name__ == '__main__':
    main()
