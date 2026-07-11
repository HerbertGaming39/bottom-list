"""
Discord Bot for Bottom List
Run separately: python discord_bot.py
"""
import os
import sys
import asyncio
import uuid
import aiohttp
import aiofiles

# Add the site directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Setup Flask app context before importing models
from app import app
app.app_context().push()

from app import db, _get_setting, _decrypt_token
from models import User, Level, Record, LevelSubmission, SongSubmission
import discord
from discord.ext import commands


# ============ CONFIG ============

PREFIX = '!'  # Fixed prefix, read from env if needed

def get_token():
    """Get bot token from database or environment."""
    try:
        enc = _get_setting('discord_bot_token')
        if enc:
            tok = _decrypt_token(enc)
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
        await ctx.send(f'⚠️ Missing argument: {error.param.name}\nUsage: `{PREFIX}{ctx.command.name} {ctx.command.signature}`')
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send(f'⚠️ Bad argument: {error}')
        return
    
    print(f'Error in {ctx.command}: {error}')
    await ctx.send(f'❌ An error occurred. Please try again.')


# ============ COMMANDS ============

@bot.command()
async def ping(ctx):
    """Test bot responsiveness"""
    await ctx.send('🏓 Pong!')


@bot.command()
async def help(ctx):
    """Show available commands"""
    embed = discord.Embed(
        title='Bottom List Bot Commands',
        color=0x3498db,
        description='All commands use the `!` prefix'
    )
    
    commands_list = [
        ('!ping', 'Test if bot is working'),
        ('!search <name_or_id>', 'Search for a level by name or GD level ID'),
        ('!submit <level_id>', 'Submit a song (attach mp3/ogg file)'),
        ('!level <id>', 'Get level details by internal ID'),
        ('!user <username>', 'Get user stats'),
        ('!top [list_type] [n]', 'Show leaderboard (main/challenge/platformer/all)'),
        ('!recent [username]', 'Show recent approved records'),
        ('!recommended [list_type]', 'Show recommended level'),
        ('!blindex', 'Show NONG index stats'),
        ('!help', 'Show this help message'),
    ]
    
    for name, desc in commands_list:
        embed.add_field(name=name, value=desc, inline=False)
    
    await ctx.send(embed=embed)


@bot.command(usage='<level_id>')
async def level(ctx, level_id: str):
    """Get level details by internal ID"""
    try:
        lid = int(level_id)
    except ValueError:
        await ctx.send('⚠️ Level ID must be a number.')
        return
    
    lv = db.session.get(Level, lid)
    if not lv:
        await ctx.send('❌ Level not found.')
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
        await ctx.send('⚠️ Usage: `!user <username>`')
        return
    
    user = User.query.filter(db.func.lower(User.username) == u.lower()).first()
    if not user:
        await ctx.send(f'❌ User "{username}" not found.')
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
        await ctx.send('⚠️ List type must be: main, challenge, platformer, or all')
        return
    
    if n < 1 or n > 25:
        await ctx.send('⚠️ Count must be between 1 and 25')
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
            await ctx.send(f'❌ User "{username}" not found.')
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
        lines.append(f'• {user.username}: {lv.name} ({d})')
    
    if not lines:
        await ctx.send('No records found.')
        return
    
    await ctx.send(f'**{title}**\n' + '\n'.join(lines))


@bot.command(usage='[list_type]')
async def recommended(ctx, list_type: str = 'main'):
    """Show recommended level to beat"""
    lt = list_type.lower()
    if lt not in ('main', 'challenge', 'platformer'):
        await ctx.send('⚠️ List type must be: main, challenge, or platformer')
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
    
    await ctx.send(f'⭐ Recommended ({lt}): **{lv.name}** by {lv.creator} | Position: #{lv.position}')


@bot.command()
async def blindex(ctx):
    """Show NONG index stats"""
    count = int(Level.query.filter(Level.uses_nong == True, Level.song_filename.isnot(None), Level.song_filename != '').count() or 0)
    await ctx.send(f'📊 BL Index: `/api/bl-index` | NONG entries: **{count}**')


@bot.command(usage='<name_or_id>')
async def search(ctx, *, query: str):
    """Search for a level by name or GD level ID"""
    q = (query or '').strip()
    if not q:
        await ctx.send('⚠️ Usage: `!search <name_or_id>`')
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
        await ctx.send(f'❌ No level found matching "{q}".')
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
    
    # Check for existing NONG
    has_nong = Level.query.filter_by(gd_level_id=level.gd_level_id, uses_nong=True).first()
    if has_nong and has_nong.song_filename:
        embed.add_field(name='⚠️ Note', value='This level already has a custom song', inline=False)
    
    embed.set_footer(text=f'Use !submit {level.id} to submit a song')
    await ctx.send(embed=embed)


@bot.command(usage='(level_id) [song_name] (song_id)')
async def submit(ctx, *, args: str = ''):
    """Submit a song for a level. Format: !submit (level_id) [song_name] (song_id). Attach mp3/ogg file."""
    args = (args or '').strip()
    if not args:
        await ctx.send('⚠️ Usage: `!submit (level_id) [song_name] (song_id)` (attach an audio file)\nExample: `!submit (12345) [Cool Song Mashup] (67890)`\n- `level_id`: The Geometry Dash level ID\n- `song_name`: Name of your song (required)\n- `song_id`: Newgrounds or official song ID to replace (required)')
        return
    
    # Parse the format: (level_id) [song_name] (song_id)
    import re
    level_id_match = re.search(r'\((\d+)\)', args)
    song_name_match = re.search(r'\[([^\]]+)\]', args)
    
    # Find all numbers in parentheses - the second one should be song_id
    all_paren_nums = re.findall(r'\((\d+)\)', args)
    
    if not level_id_match:
        await ctx.send('⚠️ Please provide level ID in parentheses. Example: `!submit (12345) [My Song] (67890)`')
        return
    
    lid = level_id_match.group(1)
    song_name = song_name_match.group(1) if song_name_match else None
    
    # Song name is required
    if not song_name:
        await ctx.send('⚠️ Song name is **required**. Use format: `!submit (level_id) [song_name] (song_id)`\nExample: `!submit (12345) [My Awesome Song] (67890)`')
        return
    
    # Song ID is required - look for a second number in parentheses
    song_id = None
    if len(all_paren_nums) >= 2:
        # The second number in parentheses is the song ID
        song_id = all_paren_nums[1]
    
    if not song_id:
        await ctx.send('⚠️ Song ID is **required**. This is the Newgrounds song ID or official song ID that your NONG replaces.\nFormat: `!submit (level_id) [song_name] (song_id)`\nExample: `!submit (12345) [My Song] (67890)`')
        return
    
    if not ctx.message.attachments:
        await ctx.send('⚠️ Please attach an audio file (mp3/ogg, max 10MB).')
        return
    
    if len(ctx.message.attachments) > 10:
        await ctx.send('⚠️ You can only submit up to 10 files at once.')
        return
    
    with app.app_context():
        # Find level
        level = None
        if lid.isdigit():
            level = db.session.get(Level, int(lid))
        if not level:
            level = Level.query.filter_by(gd_level_id=lid).first()
        
        if not level:
            await ctx.send(f'❌ Level not found: `{lid}`. Use `!search` first.')
            return
        
        # Check pending count
        pending_count = SongSubmission.query.filter_by(level_id=level.id, status='pending').count()
        if pending_count >= 10:
            await ctx.send('⚠️ This level already has 10 pending submissions. Please wait for moderation.')
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
                                song_name=song_name,  # Save the song name from the command
                                song_id=song_id,  # Save the song ID (required)
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
            msg = f'✅ Submitted **{len(saved_files)}** song(s) for **{level.name}**!'
            if song_name:
                msg += f'\n🎵 Song name: `{song_name}`'
            msg += f'\n🆔 Song ID: `{song_id}` (replaces this song)'
            msg += '\nA moderator will review your submission.'
            if errors:
                msg += f'\n⚠️ Skipped: {", ".join(errors)}'
            await ctx.send(msg)
        else:
            await ctx.send('❌ No files were submitted. ' + (f'Errors: {", ".join(errors)}' if errors else ''))


# ============ MAIN ============

def main():
    print('=' * 40)
    print('Bottom List Discord Bot')
    print('=' * 40)
    
    # Ensure directories exist
    pending_dir = os.path.join(app.root_path, 'static', 'pending_songs')
    os.makedirs(pending_dir, exist_ok=True)
    print(f'Directory: {pending_dir}')
    
    # Get token
    token = get_token()
    preview = token[:10] + '...' if len(token) > 10 else 'NOT SET'
    print(f'Token: {preview}')
    
    if not token:
        print('\n❌ No bot token found!')
        print('Set it via:')
        print('  1. Database setting: discord_bot_token')
        print('  2. Environment variable: DISCORD_BOT_TOKEN')
        sys.exit(1)
    
    print('\nConnecting to Discord...')
    try:
        bot.run(token)
    except Exception as e:
        print(f'\n❌ Failed to start: {e}')
        sys.exit(1)


if __name__ == '__main__':
    main()
