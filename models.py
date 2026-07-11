from flask_login import UserMixin
from datetime import datetime, timezone
from config import db

def utcnow():
    """Returns naive UTC datetime."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

class Clan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    description = db.Column(db.String(500), nullable=True)
    owner_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    members = db.relationship('User', backref='clan', lazy=True, foreign_keys='User.clan_id')
    owner = db.relationship('User', foreign_keys=[owner_user_id], lazy=True)

class ClanJoinRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    clan_id = db.Column(db.Integer, db.ForeignKey('clan.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    clan = db.relationship('Clan', backref=db.backref('join_requests', lazy=True, cascade="all, delete-orphan"))
    user = db.relationship('User', backref=db.backref('clan_join_requests', lazy=True, cascade="all, delete-orphan"))
    __table_args__ = (db.UniqueConstraint('clan_id', 'user_id', name='uq_clan_join_request'),)

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(150), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    is_mod = db.Column(db.Boolean, default=False)
    is_disabled = db.Column(db.Boolean, default=False)
    rank = db.Column(db.String(30), nullable=False, default='member')
    points = db.Column(db.Float, default=0.0)
    bio = db.Column(db.String(500), nullable=True)
    youtube_link = db.Column(db.String(200), nullable=True)
    discord_tag = db.Column(db.String(100), nullable=True)
    discord_id = db.Column(db.String(50), nullable=True)
    discord_username = db.Column(db.String(100), nullable=True)
    discord_avatar = db.Column(db.String(100), nullable=True)
    avatar_filename = db.Column(db.String(200), nullable=True)
    banner_filename = db.Column(db.String(200), nullable=True)
    equipped_avatar_item_id = db.Column(db.Integer, db.ForeignKey('shop_item.id'), nullable=True)
    equipped_background_item_id = db.Column(db.Integer, db.ForeignKey('shop_item.id'), nullable=True)
    equipped_deco_item_id = db.Column(db.Integer, db.ForeignKey('shop_item.id'), nullable=True)
    api_token = db.Column(db.String(128), unique=True, nullable=True)
    clan_id = db.Column(db.Integer, db.ForeignKey('clan.id'), nullable=True)
    ui_version = db.Column(db.String(20), nullable=True)
    ui_template_slug = db.Column(db.String(80), nullable=True)
    ui_custom_mode = db.Column(db.String(20), nullable=True)
    ui_custom_script = db.Column(db.Text, nullable=True)
    theme_accent = db.Column(db.String(32), nullable=True)
    theme_accent_bg = db.Column(db.String(32), nullable=True)
    last_seen_at = db.Column(db.DateTime, nullable=True)
    guidelines_accepted_at = db.Column(db.DateTime, nullable=True)
    register_ip = db.Column(db.String(100), nullable=True)
    bcoins = db.Column(db.Integer, default=0)
    total_bcoins_earned = db.Column(db.Integer, default=0)
    last_wheel_spin_at = db.Column(db.DateTime, nullable=True)
    found_clues = db.Column(db.Text, nullable=True)
    is_donor = db.Column(db.Boolean, default=False)
    scheduled_deletion_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow)

class NewsPost(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    author_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    author = db.relationship('User', backref=db.backref('news_posts', lazy=True))

class Page(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(100), unique=True, nullable=False)
    body = db.Column(db.Text, nullable=False)
    is_published = db.Column(db.Boolean, default=True)
    is_html = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow)

class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=True)
    starts_at = db.Column(db.DateTime, nullable=True)
    ends_at = db.Column(db.DateTime, nullable=True)
    is_published = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=utcnow)

class Setting(db.Model):
    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, nullable=True)

class SiteLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(50), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    actor_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    actor = db.relationship('User', backref=db.backref('site_logs', lazy=True))

class CustomList(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    creator_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    creator = db.relationship('User', backref=db.backref('custom_lists', lazy=True))

class MapPack(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    level_ids = db.Column(db.Text, nullable=True)
    thumbnail_filename = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow)

class Level(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    creator = db.Column(db.String(200), nullable=False)
    video_url = db.Column(db.String(500), nullable=True)
    description = db.Column(db.Text, nullable=True)
    gd_level_id = db.Column(db.String(50), nullable=True)
    song_id = db.Column(db.Integer, nullable=True)
    official_song_id = db.Column(db.Integer, nullable=True)
    points_reward = db.Column(db.Float, default=0.0)
    list_type = db.Column(db.String(20), default='main')
    difficulty = db.Column(db.String(50), nullable=True)
    is_verified = db.Column(db.Boolean, default=False)
    position = db.Column(db.Integer, nullable=True)
    thumbnail_filename = db.Column(db.String(200), nullable=True)
    categories = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow)

class Record(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    level_id = db.Column(db.Integer, db.ForeignKey('level.id'), nullable=False)
    video_url = db.Column(db.String(500), nullable=True)
    device = db.Column(db.String(20), nullable=True)
    comment = db.Column(db.String(500), nullable=True)
    beaten_on_vc = db.Column(db.Boolean, default=False)
    vc_proof_filename = db.Column(db.String(200), nullable=True)
    proof_image_filename = db.Column(db.String(200), nullable=True)
    status = db.Column(db.String(20), default='pending')
    rating_deco = db.Column(db.Integer, nullable=True)
    rating_gameplay = db.Column(db.Integer, nullable=True)
    rating_difficulty = db.Column(db.Integer, nullable=True)
    submitted_at = db.Column(db.DateTime, default=utcnow)
    
    user = db.relationship('User', backref=db.backref('records', lazy=True, cascade="all, delete-orphan"))
    level = db.relationship('Level', backref=db.backref('records', lazy=True, cascade="all, delete-orphan"))

class LevelSubmission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    creator = db.Column(db.String(200), nullable=False)
    video_url = db.Column(db.String(500), nullable=True)
    gd_level_id = db.Column(db.String(50), nullable=True)
    proposed_position = db.Column(db.Integer, nullable=True)
    status = db.Column(db.String(20), default='pending')
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    submitted_at = db.Column(db.DateTime, default=utcnow)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    reviewed_by_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    
    thumbnail_filename = db.Column(db.String(200), nullable=True)

    user = db.relationship('User', foreign_keys=[user_id], backref=db.backref('level_submissions', lazy=True))
    reviewed_by = db.relationship('User', foreign_keys=[reviewed_by_user_id], lazy=True)

class SongSubmission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    gd_level_id = db.Column(db.String(50), nullable=True)
    level_id = db.Column(db.Integer, db.ForeignKey('level.id'), nullable=True)
    discord_user_id = db.Column(db.String(50), nullable=True)
    discord_username = db.Column(db.String(100), nullable=True)
    song_name = db.Column(db.String(200), nullable=True)
    artist_name = db.Column(db.String(200), nullable=True)
    song_id = db.Column(db.String(50), nullable=True)  # Newgrounds/official song ID to replace
    song_filename = db.Column(db.String(200), nullable=False)
    original_filename = db.Column(db.String(200), nullable=True)
    file_size = db.Column(db.Integer, nullable=True)
    status = db.Column(db.String(20), default='pending')
    submitted_at = db.Column(db.DateTime, default=utcnow)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    reviewed_by_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    notes = db.Column(db.Text, nullable=True)

    level = db.relationship('Level', backref=db.backref('song_submissions', lazy=True))
    reviewed_by = db.relationship('User', foreign_keys=[reviewed_by_user_id], lazy=True)

class ShopItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    price = db.Column(db.Integer, nullable=False, default=0)
    item_type = db.Column(db.String(50), nullable=False, default='profile_deco')
    image_filename = db.Column(db.String(200), nullable=True)
    is_available = db.Column(db.Boolean, default=True)
    is_limited = db.Column(db.Boolean, default=False)
    expires_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow)

    @property
    def is_expired(self):
        if not self.expires_at:
            return False
        from datetime import datetime
        return datetime.utcnow() > self.expires_at

class MarketListing(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey('shop_item.id'), nullable=False)
    seller_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    price = db.Column(db.Integer, nullable=False)
    is_sold = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    item = db.relationship('ShopItem', lazy=True)
    seller = db.relationship('User', foreign_keys=[seller_id], lazy=True)


class Clue(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    clue_key = db.Column(db.String(80), unique=True, nullable=False)
    clue_text = db.Column(db.Text, nullable=False)
    location_hint = db.Column(db.String(200), nullable=True)
    is_found = db.Column(db.Boolean, default=False)
    order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=utcnow)


class UiTemplate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    slug = db.Column(db.String(80), unique=True, nullable=False)
    html_content = db.Column(db.Text, nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=utcnow)

class SparkyImage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    filename = db.Column(db.String(200), nullable=False)
    level_id = db.Column(db.Integer, db.ForeignKey('level.id'), nullable=True)
    description = db.Column(db.String(500), nullable=True)
    submitted_by_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    is_approved = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    discord_username = db.Column(db.String(100), nullable=True)
    submitted_by = db.relationship('User', backref=db.backref('sparky_images', lazy=True))
    level = db.relationship('Level', backref=db.backref('sparky_images', lazy=True))

class GuessGameScore(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    discord_user_id = db.Column(db.String(50), nullable=False)
    discord_username = db.Column(db.String(100), nullable=True)
    score = db.Column(db.Integer, default=0)
    last_guess_at = db.Column(db.DateTime, nullable=True)

class UserPurchase(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey('shop_item.id'), nullable=False)
    purchased_at = db.Column(db.DateTime, default=utcnow)
    user = db.relationship('User', backref=db.backref('purchases', lazy=True, cascade="all, delete-orphan"))
    item = db.relationship('ShopItem', backref=db.backref('purchases', lazy=True))
