"""
blueprints/shop.py – Bcoin shop, inventory, and wheel of fortune.
"""
import random
from datetime import datetime, timezone

from flask import (Blueprint, render_template, redirect, url_for, flash, request, abort)
from flask_login import login_required, current_user

from sqlalchemy.orm import joinedload

from config import db
from models import ShopItem, UserPurchase, MarketListing

shop_bp = Blueprint('shop', __name__)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Shop ──────────────────────────────────────────────────────────────────────

@shop_bp.route('/shop')
def shop_list():
    now = utcnow()
    items = ShopItem.query.filter_by(is_available=True).order_by(ShopItem.price.asc()).all()
    items = [i for i in items if not i.is_expired]
    owned_ids = set()
    if current_user.is_authenticated:
        owned_ids = set(p.item_id for p in UserPurchase.query.filter_by(user_id=current_user.id).all())
    return render_template('shop.html', items=items, owned_ids=owned_ids, now=now)


@shop_bp.route('/shop/buy/<int:item_id>', methods=['POST'])
@login_required
def shop_buy(item_id):
    item = ShopItem.query.get_or_404(item_id)
    if not item.is_available:
        flash('This item is no longer available.', 'danger')
        return redirect(url_for('shop.shop_list'))
    existing = UserPurchase.query.filter_by(user_id=current_user.id, item_id=item.id).first()
    if existing:
        flash('You already own this item.', 'warning')
        return redirect(url_for('shop.shop_list'))
    # Atomic debit: prevents double-spend / negative balance under concurrency
    from sqlalchemy import text
    debited = db.session.execute(
        text("UPDATE user SET bcoins = bcoins - :p WHERE id = :id AND bcoins >= :p"),
        {'p': item.price, 'id': current_user.id}
    )
    if debited.rowcount != 1:
        db.session.rollback()
        flash(f'Not enough bcoins! You need {item.price} but have {current_user.bcoins or 0}.', 'danger')
        return redirect(url_for('shop.shop_list'))
    db.session.add(UserPurchase(user_id=current_user.id, item_id=item.id))
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash('Purchase failed (duplicate or race). Please retry.', 'danger')
        return redirect(url_for('shop.shop_list'))
    flash(f'Purchased "{item.name}" for {item.price} bcoins!', 'success')
    return redirect(url_for('shop.shop_list'))


# ── Inventory ─────────────────────────────────────────────────────────────────

@shop_bp.route('/inventory')
@login_required
def inventory():
    purchases = UserPurchase.query.filter_by(user_id=current_user.id).order_by(UserPurchase.purchased_at.desc()).all()
    total_spent = sum(p.item.price for p in purchases if p.item)
    return render_template('inventory.html', purchases=purchases, total_spent=total_spent)


# ── Wheel of Fortune ──────────────────────────────────────────────────────────

_WHEEL_SLOTS = [
    {'label': '500 bcoins', 'reward_type': 'bcoins', 'amount': 500, 'weight': 10},
    {'label': 'Nothing', 'reward_type': 'nothing', 'amount': 0, 'weight': 25},
    {'label': 'Nothing', 'reward_type': 'nothing', 'amount': 0, 'weight': 25},
    {'label': 'Nothing', 'reward_type': 'nothing', 'amount': 0, 'weight': 25},
    {'label': 'Nothing', 'reward_type': 'nothing', 'amount': 0, 'weight': 25},
    {'label': '100 bcoins', 'reward_type': 'bcoins', 'amount': 100, 'weight': 20},
    {'label': '50 bcoins', 'reward_type': 'bcoins', 'amount': 50, 'weight': 30},
    {'label': 'Rare Item!', 'reward_type': 'rare_item', 'amount': 0, 'weight': 5},
]


def _pick_wheel_reward():
    import secrets as _secrets
    total_weight = sum(s['weight'] for s in _WHEEL_SLOTS)
    roll = _secrets.randbelow(total_weight) + 1
    cumulative = 0
    for slot in _WHEEL_SLOTS:
        cumulative += slot['weight']
        if roll <= cumulative:
            return dict(slot)
    return dict(_WHEEL_SLOTS[-1])


@shop_bp.route('/wheel')
@login_required
def wheel():
    can_spin = True
    next_spin_at = None
    if current_user.last_wheel_spin_at:
        from datetime import timedelta
        next_spin = current_user.last_wheel_spin_at + timedelta(days=1)
        if utcnow() < next_spin:
            can_spin = False
            next_spin_at = next_spin
    return render_template('wheel.html', can_spin=can_spin, next_spin_at=next_spin_at)


@shop_bp.route('/wheel/spin', methods=['POST'])
@login_required
def wheel_spin():
    from datetime import timedelta
    from sqlalchemy import text
    # Atomic cooldown claim: only the first concurrent request wins
    claimed = db.session.execute(
        text("UPDATE user SET last_wheel_spin_at = :now WHERE id = :id AND (last_wheel_spin_at IS NULL OR last_wheel_spin_at <= :cutoff)"),
        {'now': utcnow(), 'id': current_user.id, 'cutoff': utcnow() - timedelta(days=1)}
    )
    if claimed.rowcount != 1:
        db.session.rollback()
        flash('You can only spin once per day!', 'danger')
        return redirect(url_for('shop.wheel'))
    db.session.commit()
    reward = _pick_wheel_reward()
    current_user.last_wheel_spin_at = utcnow()
    if reward['reward_type'] == 'bcoins':
        amt = reward['amount']
        current_user.bcoins = (current_user.bcoins or 0) + amt
        current_user.total_bcoins_earned = (current_user.total_bcoins_earned or 0) + amt
        db.session.commit()
        flash(f'You won {amt} bcoins!', 'success')
    elif reward['reward_type'] == 'rare_item':
        rare_items = ShopItem.query.filter_by(is_available=True).order_by(ShopItem.price.desc()).limit(3).all()
        if rare_items:
            chosen = random.choice(rare_items)
            existing = UserPurchase.query.filter_by(user_id=current_user.id, item_id=chosen.id).first()
            if not existing:
                db.session.add(UserPurchase(user_id=current_user.id, item_id=chosen.id))
                db.session.commit()
                flash(f'You won a rare item: "{chosen.name}"!', 'success')
            else:
                amt = 200
                current_user.bcoins = (current_user.bcoins or 0) + amt
                current_user.total_bcoins_earned = (current_user.total_bcoins_earned or 0) + amt
                db.session.commit()
                flash(f'You already own that rare item, so here is {amt} bcoins instead!', 'success')
        else:
            amt = 100
            current_user.bcoins = (current_user.bcoins or 0) + amt
            current_user.total_bcoins_earned = (current_user.total_bcoins_earned or 0) + amt
            db.session.commit()
            flash(f'No rare items in shop, you got {amt} bcoins instead!', 'success')
    else:
        db.session.commit()
        flash('Nothing this time. Try again tomorrow!', 'info')
    return redirect(url_for('shop.wheel'))


# ── Equip / Unequip ───────────────────────────────────────────────────────────

_EQUIP_TYPE_MAP = {
    'animated_avatar': 'equipped_avatar_item_id',
    'background': 'equipped_background_item_id',
    'profile_deco': 'equipped_deco_item_id',
}

@shop_bp.route('/shop/equip/<int:purchase_id>', methods=['POST'])
@login_required
def equip_item(purchase_id):
    purchase = UserPurchase.query.options(joinedload(UserPurchase.item)).filter_by(id=purchase_id, user_id=current_user.id).first()
    if not purchase:
        flash('Purchase not found.', 'danger')
        return redirect(url_for('shop.inventory'))
    item = purchase.item
    if not item:
        flash('Item no longer exists.', 'danger')
        return redirect(url_for('shop.inventory'))
    col = _EQUIP_TYPE_MAP.get(item.item_type)
    if not col:
        flash('This item type cannot be equipped.', 'warning')
        return redirect(url_for('shop.inventory'))
    setattr(current_user, col, item.id)
    db.session.commit()
    flash(f'Equipped "{item.name}"!', 'success')
    return redirect(url_for('shop.inventory'))


@shop_bp.route('/shop/unequip/<int:purchase_id>', methods=['POST'])
@login_required
def unequip_item(purchase_id):
    purchase = UserPurchase.query.options(joinedload(UserPurchase.item)).filter_by(id=purchase_id, user_id=current_user.id).first()
    if not purchase:
        flash('Purchase not found.', 'danger')
        return redirect(url_for('shop.inventory'))
    item = purchase.item
    if not item:
        flash('Item no longer exists.', 'danger')
        return redirect(url_for('shop.inventory'))
    col = _EQUIP_TYPE_MAP.get(item.item_type)
    if not col:
        flash('This item type cannot be unequipped.', 'warning')
        return redirect(url_for('shop.inventory'))
    if getattr(current_user, col) == item.id:
        setattr(current_user, col, None)
        db.session.commit()
        flash(f'Unequipped "{item.name}".', 'success')
    else:
        flash('That item is not currently equipped.', 'info')
    return redirect(url_for('shop.inventory'))


# ── Black Market ───────────────────────────────────────────────────────────────


def is_market_unlocked():
    return current_user.is_authenticated


@shop_bp.route('/black-market')
def black_market():
    unlocked = is_market_unlocked()
    listings = MarketListing.query.filter_by(is_sold=False).order_by(MarketListing.created_at.desc()).all()
    return render_template('black_market.html', listings=listings, unlocked=unlocked)


@shop_bp.route('/black-market/sell/<int:purchase_id>', methods=['GET', 'POST'])
@login_required
def black_market_sell(purchase_id):
    if not is_market_unlocked():
        flash('The black market is not yet unlocked.', 'danger')
        return redirect(url_for('shop.black_market'))
    purchase = UserPurchase.query.options(joinedload(UserPurchase.item)).filter_by(id=purchase_id, user_id=current_user.id).first()
    if not purchase:
        flash('Purchase not found.', 'danger')
        return redirect(url_for('shop.inventory'))
    item = purchase.item
    if not item:
        flash('Item no longer exists.', 'danger')
        return redirect(url_for('shop.inventory'))
    if not item.is_limited:
        flash('Only limited items can be sold on the black market.', 'warning')
        return redirect(url_for('shop.inventory'))

    existing = MarketListing.query.filter_by(item_id=item.id, seller_id=current_user.id, is_sold=False).first()
    if existing:
        flash('You already have this item listed for sale.', 'warning')
        return redirect(url_for('shop.black_market'))

    if request.method == 'POST':
        try:
            price = int(request.form.get('price', 0))
        except (TypeError, ValueError):
            flash('Invalid price.', 'danger')
            return render_template('black_market_sell.html', purchase=purchase, item=item)
        if price < 1000 or price > 10_000_000:
            flash('Minimum listing price is 1000 bcoins.', 'danger')
            return render_template('black_market_sell.html', purchase=purchase, item=item)
        listing = MarketListing(item_id=item.id, seller_id=current_user.id, price=price)
        db.session.add(listing)
        db.session.commit()
        flash(f'Listed "{item.name}" for {price} bcoins.', 'success')
        return redirect(url_for('shop.black_market'))

    return render_template('black_market_sell.html', purchase=purchase, item=item)


@shop_bp.route('/black-market/buy/<int:listing_id>', methods=['POST'])
@login_required
def black_market_buy(listing_id):
    if not is_market_unlocked():
        flash('The black market is not yet unlocked.', 'danger')
        return redirect(url_for('shop.black_market'))
    listing = MarketListing.query.options(joinedload(MarketListing.item), joinedload(MarketListing.seller)).filter_by(id=listing_id, is_sold=False).first()
    if not listing:
        flash('Listing not found or already sold.', 'danger')
        return redirect(url_for('shop.black_market'))
    if listing.seller_id == current_user.id:
        flash('You cannot buy your own listing.', 'danger')
        return redirect(url_for('shop.black_market'))
    existing = UserPurchase.query.filter_by(user_id=current_user.id, item_id=listing.item_id).first()
    if existing:
        flash('You already own this item.', 'warning')
        return redirect(url_for('shop.black_market'))
    if (current_user.bcoins or 0) < listing.price:
        flash(f'Not enough bcoins! You need {listing.price} but have {current_user.bcoins or 0}.', 'danger')
        return redirect(url_for('shop.black_market'))

    from sqlalchemy import text
    # Atomically claim the listing: exactly one concurrent buyer can win
    claimed = db.session.execute(
        text("UPDATE market_listing SET is_sold = 1 WHERE id = :lid AND is_sold = 0"),
        {'lid': listing.id}
    )
    if claimed.rowcount != 1:
        db.session.rollback()
        flash('Listing not found or already sold.', 'danger')
        return redirect(url_for('shop.black_market'))
    # Atomic debit: prevents negative balances under concurrency
    debited = db.session.execute(
        text("UPDATE user SET bcoins = bcoins - :p WHERE id = :id AND bcoins >= :p"),
        {'p': listing.price, 'id': current_user.id}
    )
    if debited.rowcount != 1:
        db.session.rollback()
        flash(f'Not enough bcoins! You need {listing.price} but have {current_user.bcoins or 0}.', 'danger')
        return redirect(url_for('shop.black_market'))
    # Atomic credit to seller
    credited = db.session.execute(
        text("UPDATE user SET bcoins = bcoins + :p WHERE id = :sid"),
        {'p': listing.price, 'sid': listing.seller_id}
    )
    if credited.rowcount != 1:
        db.session.rollback()
        flash('Seller account no longer exists; purchase cancelled.', 'danger')
        return redirect(url_for('shop.black_market'))
    # OWNERSHIP TRANSFER: remove the seller's purchase row, grant a new one to the buyer
    seller_purchase = UserPurchase.query.filter_by(user_id=listing.seller_id, item_id=listing.item_id).first()
    if seller_purchase:
        _EQUIP_COLS = ('equipped_avatar_item_id', 'equipped_background_item_id', 'equipped_deco_item_id')
        seller = listing.seller
        for col in _EQUIP_COLS:
            if getattr(seller, col, None) == listing.item_id:
                setattr(seller, col, None)
        db.session.delete(seller_purchase)
    db.session.add(UserPurchase(user_id=current_user.id, item_id=listing.item_id))
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash('Purchase failed due to a conflict. Please retry.', 'danger')
        return redirect(url_for('shop.black_market'))
    flash(f'Purchased "{listing.item.name}" for {listing.price} bcoins!', 'success')
    return redirect(url_for('shop.black_market'))


@shop_bp.route('/black-market/my-listings')
@login_required
def black_market_my_listings():
    listings = MarketListing.query.filter_by(seller_id=current_user.id).order_by(MarketListing.created_at.desc()).all()
    return render_template('black_market_my_listings.html', listings=listings)


@shop_bp.route('/black-market/cancel/<int:listing_id>', methods=['POST'])
@login_required
def black_market_cancel(listing_id):
    listing = MarketListing.query.filter_by(id=listing_id, seller_id=current_user.id, is_sold=False).first()
    if not listing:
        flash('Listing not found or already sold.', 'danger')
        return redirect(url_for('shop.black_market_my_listings'))
    db.session.delete(listing)
    db.session.commit()
    flash('Listing cancelled.', 'success')
    return redirect(url_for('shop.black_market_my_listings'))
