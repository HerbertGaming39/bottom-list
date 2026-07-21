"""
schema.py – ensure_schema() applies forward-only SQLite migrations on startup
or when triggered manually from the admin maintenance panel.
"""
import uuid as _uuid

from sqlalchemy import text

from config import db


def ensure_schema():
    """Add any missing columns / indexes to the SQLite database."""
    try:
        db.create_all()
    except Exception:
        pass

    # ── record ─────────────────────────────────────────────────────────────
    try:
        info = db.session.execute(text("PRAGMA table_info(record)")).fetchall()
        cols = [row[1] for row in info]
        _add_col_if_missing(cols, "record", "device", "TEXT")
        _add_col_if_missing(cols, "record", "beaten_on_vc", "INTEGER DEFAULT 0")
        _add_col_if_missing(cols, "record", "vc_proof_filename", "TEXT")
        _add_col_if_missing(cols, "record", "proof_image_filename", "TEXT")
        _add_col_if_missing(cols, "record", "rating_deco", "INTEGER")
        _add_col_if_missing(cols, "record", "rating_gameplay", "INTEGER")
        _add_col_if_missing(cols, "record", "rating_difficulty", "INTEGER")
        _add_col_if_missing(cols, "record", "comment", "TEXT")
        db.session.execute(text("CREATE INDEX IF NOT EXISTS idx_record_status_submitted_at ON record(status, submitted_at)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS idx_record_user_status ON record(user_id, status)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS idx_record_level_status ON record(level_id, status)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS idx_record_user_submitted_at ON record(user_id, submitted_at)"))
        db.session.commit()
    except Exception:
        _rollback()

    # ── user ───────────────────────────────────────────────────────────────
    try:
        info = db.session.execute(text("PRAGMA table_info(user)")).fetchall()
        cols = [row[1] for row in info]
        for col, definition in [
            ("discord_id",             "TEXT"),
            ("discord_username",       "TEXT"),
            ("discord_avatar",         "TEXT"),
            ("avatar_filename",        "TEXT"),
            ("banner_filename",        "TEXT"),
            ("api_token",              "TEXT"),
            ("register_ip",            "TEXT"),
            ("clan_id",                "INTEGER"),
            ("ui_version",             "TEXT"),
            ("theme_accent",           "TEXT"),
            ("theme_accent_bg",        "TEXT"),
            ("last_seen_at",           "DATETIME"),
            ("guidelines_accepted_at", "DATETIME"),
            ("is_disabled",            "INTEGER DEFAULT 0"),
            ("ui_template_slug",       "TEXT"),
            ("ui_custom_mode",         "TEXT"),
            ("ui_custom_script",       "TEXT"),
            ("bcoins",                 "INTEGER DEFAULT 0"),
            ("total_bcoins_earned",    "INTEGER DEFAULT 0"),
            ("last_wheel_spin_at",     "DATETIME"),
            ("equipped_avatar_item_id",     "INTEGER"),
            ("equipped_background_item_id", "INTEGER"),
            ("equipped_deco_item_id",       "INTEGER"),
        ]:
            _add_col_if_missing(cols, "user", col, definition)

        if "rank" not in cols:
            db.session.execute(text("ALTER TABLE user ADD COLUMN rank TEXT DEFAULT 'member'"))
            db.session.commit()
            try:
                db.session.execute(text("UPDATE user SET rank='admin' WHERE is_admin=1"))
                db.session.execute(text("UPDATE user SET rank='mod' WHERE is_admin=0 AND is_mod=1"))
                db.session.execute(text("UPDATE user SET rank='member' WHERE rank IS NULL OR TRIM(rank)=''"))
                db.session.commit()
            except Exception:
                _rollback()

        db.session.execute(text("CREATE INDEX IF NOT EXISTS idx_user_username ON user(username)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS idx_user_last_seen ON user(last_seen_at)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS idx_user_points ON user(points)"))
        db.session.commit()
    except Exception:
        _rollback()

    # ── map_pack ───────────────────────────────────────────────────────────
    try:
        info = db.session.execute(text("PRAGMA table_info(map_pack)")).fetchall()
        if info:
            cols = [row[1] for row in info]
            _add_col_if_missing(cols, "map_pack", "thumbnail_filename", "TEXT")
            db.session.execute(text("CREATE INDEX IF NOT EXISTS idx_map_pack_created_at ON map_pack(created_at)"))
            db.session.commit()
    except Exception:
        _rollback()

    # ── clan ───────────────────────────────────────────────────────────────
    try:
        info = db.session.execute(text("PRAGMA table_info(clan)")).fetchall()
        if info:
            cols = [row[1] for row in info]
            if "owner_user_id" not in cols:
                db.session.execute(text("ALTER TABLE clan ADD COLUMN owner_user_id INTEGER"))
                db.session.commit()
                try:
                    db.session.execute(text(
                        "UPDATE clan SET owner_user_id=("
                        "  SELECT MIN(id) FROM user WHERE user.clan_id=clan.id"
                        ") WHERE owner_user_id IS NULL"))
                    db.session.commit()
                except Exception:
                    _rollback()
    except Exception:
        _rollback()

    # ── clan_join_request ──────────────────────────────────────────────────
    try:
        info = db.session.execute(text("PRAGMA table_info(clan_join_request)")).fetchall()
        if not info:
            db.create_all()
    except Exception:
        pass

    # ── level ──────────────────────────────────────────────────────────────
    try:
        info = db.session.execute(text("PRAGMA table_info(level)")).fetchall()
        cols = [row[1] for row in info]
        if "created_at" not in cols:
            db.session.execute(text("ALTER TABLE level ADD COLUMN created_at DATETIME"))
            db.session.commit()
            try:
                db.session.execute(text("UPDATE level SET created_at=CURRENT_TIMESTAMP WHERE created_at IS NULL"))
                db.session.commit()
            except Exception:
                _rollback()
        for col, definition in [
            ("categories",         "TEXT"),
            ("difficulty",         "TEXT"),
            ("thumbnail_filename", "TEXT"),
        ]:
            _add_col_if_missing(cols, "level", col, definition)
        db.session.execute(text("CREATE INDEX IF NOT EXISTS idx_level_list_type_position ON level(list_type, position)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS idx_level_verified_list_type ON level(is_verified, list_type)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS idx_level_created_at ON level(created_at)"))
        db.session.commit()
    except Exception:
        _rollback()

    # ── page ───────────────────────────────────────────────────────────────
    try:
        info = db.session.execute(text("PRAGMA table_info(page)")).fetchall()
        if not info:
            db.create_all()
        else:
            cols = [row[1] for row in info]
            _add_col_if_missing(cols, "page", "is_html", "INTEGER DEFAULT 0")
            _add_col_if_missing(cols, "page", "is_published", "INTEGER DEFAULT 1")
            db.session.commit()
    except Exception:
        _rollback()

    # ── event ──────────────────────────────────────────────────────────────
    try:
        info = db.session.execute(text("PRAGMA table_info(event)")).fetchall()
        if not info:
            db.create_all()
    except Exception:
        pass

    # ── level_submission ───────────────────────────────────────────────────
    try:
        info = db.session.execute(text("PRAGMA table_info(level_submission)")).fetchall()
        if info:
            cols = [row[1] for row in info]
            if "list_type" not in cols:
                db.session.execute(text("ALTER TABLE level_submission ADD COLUMN list_type TEXT DEFAULT 'main'"))
                db.session.commit()
                try:
                    db.session.execute(text(
                        "UPDATE level_submission SET list_type='main' WHERE list_type IS NULL OR TRIM(list_type)=''"))
                    db.session.commit()
                except Exception:
                    _rollback()
            for col, definition in [
                ("gd_level_id",        "TEXT"),
                ("proposed_position",  "INTEGER"),
                ("is_verified",        "INTEGER DEFAULT 0"),
                ("verified_video_url", "TEXT"),
                ("collaborators",      "TEXT"),
                ("thumbnail_filename", "TEXT"),
            ]:
                _add_col_if_missing(cols, "level_submission", col, definition)
            db.session.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_level_submission_status_submitted_at "
                "ON level_submission(status, submitted_at)"))
            db.session.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_level_submission_user_status "
                "ON level_submission(user_id, status)"))
            db.session.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_level_submission_list_type "
                "ON level_submission(list_type)"))
            db.session.commit()
    except Exception:
        _rollback()

    # ── site_log ───────────────────────────────────────────────────────────
    try:
        info = db.session.execute(text("PRAGMA table_info(site_log)")).fetchall()
        if info:
            cols = [row[1] for row in info]
            if "kind" not in cols:
                db.session.execute(text("ALTER TABLE site_log ADD COLUMN kind TEXT DEFAULT ''"))
                if "event_type" in cols:
                    db.session.execute(text("UPDATE site_log SET kind=event_type WHERE kind IS NULL OR kind=''"))
                db.session.commit()
    except Exception:
        _rollback()

    # ── user_block ────────────────────────────────────────────────────────
    try:
        info = db.session.execute(text("PRAGMA table_info(user_block)")).fetchall()
        if info:
            cols = [row[1] for row in info]
            _add_col_if_missing(cols, "user_block", "block_options", "TEXT DEFAULT '{}'")
            db.session.commit()
    except Exception:
        _rollback()


# ── helpers ────────────────────────────────────────────────────────────────────

def _add_col_if_missing(cols: list, table: str, column: str, definition: str):
    if column not in cols:
        try:
            db.session.execute(
                text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}"))
            db.session.commit()
        except Exception:
            _rollback()


def _rollback():
    try:
        db.session.rollback()
    except Exception:
        pass
