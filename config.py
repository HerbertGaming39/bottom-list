"""
config.py – Flask app factory and shared configuration.
Import `app`, `db`, `bcrypt`, `login_manager` from here.
"""
import os
import secrets
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_bcrypt import Bcrypt
from werkzeug.middleware.proxy_fix import ProxyFix

# ── re-exported so other modules can `from config import db, bcrypt, ...` ──
db = SQLAlchemy()
bcrypt = Bcrypt()
login_manager = LoginManager()

WEBHOOK_URL = os.environ.get('DISCORD_WEBHOOK_URL', '')
LOG_WEBHOOK_URL = os.environ.get('DISCORD_LOGS_WEBHOOK_URL', '')


def _load_secret_key(app: Flask) -> str:
    env = (os.environ.get('SECRET_KEY') or '').strip()
    if env:
        return env
    try:
        os.makedirs(app.instance_path, exist_ok=True)
    except Exception:
        pass
    key_path = os.path.join(app.instance_path, 'secret_key.txt')
    try:
        if os.path.exists(key_path):
            with open(key_path, 'r', encoding='utf-8') as f:
                s = (f.read() or '').strip()
                if s:
                    return s
    except Exception:
        pass
    generated = secrets.token_urlsafe(64)
    try:
        with open(key_path, 'w', encoding='utf-8') as f:
            f.write(generated)
    except Exception:
        pass
    return generated


def create_app() -> Flask:
    app = Flask(__name__, template_folder='templates')

    app.config['SECRET_KEY'] = _load_secret_key(app)
    db_path = os.path.join(app.instance_path, 'database.db')
    # Use 4 slashes for absolute Windows path: sqlite:///<drive>:/path
    db_path_fixed = db_path.replace('\\', '/')
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path_fixed}'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SONG_UPLOAD_FOLDER'] = os.path.join(app.root_path, 'static', 'songs')
    app.config['VC_PROOF_UPLOAD_FOLDER'] = os.path.join(app.root_path, 'static', 'vc_proofs')
    app.config['RECORD_PROOF_UPLOAD_FOLDER'] = os.path.join(app.root_path, 'static', 'record_proofs')
    app.config['PROFILE_MEDIA_UPLOAD_FOLDER'] = os.path.join(app.root_path, 'static', 'profile_media')
    app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024
    app.config['RECAPTCHA_SITE_KEY'] = os.environ.get('RECAPTCHA_SITE_KEY', '')
    app.config['RECAPTCHA_SECRET_KEY'] = os.environ.get('RECAPTCHA_SECRET_KEY', '')
    app.config['HCAPTCHA_SITE_KEY'] = os.environ.get('HCAPTCHA_SITE_KEY', '')
    app.config['HCAPTCHA_SECRET_KEY'] = os.environ.get('HCAPTCHA_SECRET_KEY', '')
    app.config['DISCORD_CLIENT_ID'] = os.environ.get('DISCORD_CLIENT_ID', '')
    app.config['DISCORD_CLIENT_SECRET'] = os.environ.get('DISCORD_CLIENT_SECRET', '')
    app.config['DISCORD_REDIRECT_URI'] = os.environ.get('DISCORD_REDIRECT_URI', '')
    app.config['DISCORD_GUILD_ID'] = os.environ.get('DISCORD_GUILD_ID', '')
    app.config['DISCORD_BOT_TOKEN'] = os.environ.get('DISCORD_BOT_TOKEN', '')
    app.config['TEMPLATES_AUTO_RELOAD'] = False
    app.config['TRAP_HTTP_EXCEPTIONS'] = True

    _USE_SECURE_COOKIES = str(os.environ.get('COOKIE_SECURE') or '').strip().lower() in ('1', 'true', 'yes', 'on')
    app.config['SESSION_COOKIE_SECURE'] = _USE_SECURE_COOKIES
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['REMEMBER_COOKIE_SECURE'] = _USE_SECURE_COOKIES
    app.config['REMEMBER_COOKIE_HTTPONLY'] = True
    app.config['REMEMBER_COOKIE_SAMESITE'] = 'Lax'
    app.jinja_env.auto_reload = False

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    with app.app_context():
        import sqlalchemy as sa
        try:
            db.session.execute(sa.text("ALTER TABLE site_log ADD COLUMN body TEXT"))
            db.session.commit()
        except Exception:
            db.session.rollback()
        try:
            db.session.execute(sa.text("ALTER TABLE sparky_image ADD COLUMN level_id INTEGER REFERENCES level(id)"))
            db.session.commit()
        except Exception:
            db.session.rollback()
    for folder_key in ['PROFILE_MEDIA_UPLOAD_FOLDER', 'SONG_UPLOAD_FOLDER', 'VC_PROOF_UPLOAD_FOLDER', 'RECORD_PROOF_UPLOAD_FOLDER']:
        try:
            os.makedirs(app.config[folder_key], exist_ok=True)
        except Exception:
            pass
    bcrypt.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'

    return app
