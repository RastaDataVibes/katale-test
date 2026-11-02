import os
import re
from functools import wraps
from datetime import datetime, timedelta

from dotenv import load_dotenv
load_dotenv()

import requests
from flask import Flask, render_template, request, redirect, url_for, session, flash, abort, g
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from authlib.integrations.flask_client import OAuth
from sqlalchemy import inspect, text
from models import db, User, Vendor, Product, Order, OrderItem, STATUS_LABELS

app = Flask(__name__)
# Render sits in front of the app; this lets Flask know the real address is https://
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# ── Config (all in one place, no separate config file) ───────
database_url = os.environ.get('DATABASE_URL', '')
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql+psycopg2://', 1)
elif database_url.startswith('postgresql://'):
    database_url = database_url.replace('postgresql://', 'postgresql+psycopg2://', 1)
if not database_url:
    database_url = 'sqlite:///paul.db'   # local development fallback only

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True}
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-only-change-me')
# Stay logged in for 30 days, like modern apps
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = bool(os.environ.get('RENDER'))  # https only on the live site (Render sets RENDER)

# ── Google sign-in ───────────────────────────────────────────
# The two keys come from Google Cloud and are stored in Render's Environment settings.
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '').strip()
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET', '').strip()
oauth = OAuth(app)
if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
    oauth.register(
        name='google',
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid email profile'},
    )

# ── Emails (password reset) through Brevo, same as Carcraaft ─
BREVO_API_KEY = os.environ.get('BREVO_API_KEY', '').strip()
MAIL_SENDER = os.environ.get('MAIL_SENDER', '').strip()          # e.g. katale@outlook.com (verified in Brevo)

SITE_NAME = 'Katale'
ADMIN_EMAIL = (os.environ.get('ADMIN_EMAIL') or '').strip().lower()
DELIVERY_FEE = 5000  # UGX, flat for now (step 3 will work it out from the distance)
TRADER_MONTHLY_FEE = 30000  # UGX per month, first month free (step 5 adds the payments)

VENDOR_CATEGORIES = ['Groceries', 'Fresh produce', 'Meat & fish', 'Grains & cereals',
                     'Restaurant', 'Pharmacy', 'Household', 'Other']

# ── Photos (stored on Cloudinary) ────────────────────────────
# photo('key', width) gives a link to the photo, already shrunk to that width,
# so pages stay fast on mobile data.
CLOUDINARY_BASE = 'https://res.cloudinary.com/dcostjy6h/image/upload'
PHOTOS = {
    'hero':        'v1793605614/pexels-kindelmedia-6868967_y1s5ku',       # rider on a bicycle
    'bag':         'v1793605566/pexels-rdne-7363163_ocu6uf',              # "we deliver" shopping bag
    'phone_order': 'v1793605579/pexels-n-voitkevich-8939513_ennhoe',      # ordering on a phone
    'shopfront':   'v1793605635/pexels-hoteles-sublimes-655848650-26309839_lsur0z',  # shop shelves
    'Groceries':        'v1793605635/pexels-hoteles-sublimes-655848650-26309839_lsur0z',
    'Fresh produce':    'v1793605597/pexels-liza-sigareva-2149951107-34066399_fp8b1u',
    'Meat & fish':      'v1793605606/pexels-laudiatsr-10886018_xlxrz7',
    'Grains & cereals': 'v1793605589/pexels-macshamim-30689829_hcdbu3',
    'Restaurant':       'v1793605626/pexels-kenzero14-32228060_vooskg',
}


def photo(key, width=800, height=None):
    public_id = PHOTOS.get(key)
    if not public_id:
        return None
    size = f'w_{width},h_{height},c_fill' if height else f'w_{width},c_limit'
    return f'{CLOUDINARY_BASE}/{size},q_auto,f_auto/{public_id}.jpg'
PAYMENT_METHODS = {'cash': 'Cash on delivery', 'mobile_money': 'Mobile money on delivery'}

# What a vendor is allowed to move an order to, from each status
VENDOR_NEXT_STATUS = {
    'placed': ['accepted', 'cancelled'],
    'accepted': ['preparing', 'cancelled'],
    'preparing': ['out_for_delivery'],
    'out_for_delivery': ['delivered'],
}

db.init_app(app)


def add_missing_columns():
    """create_all() makes new tables but never changes old ones.
    This adds any new columns to tables that already exist, so the live
    database keeps up with models.py without losing any data."""
    insp = inspect(db.engine)
    is_pg = db.engine.dialect.name == 'postgresql'
    user_cols = {c['name']: c for c in insp.get_columns('users')}
    with db.engine.begin() as conn:
        if 'google_id' not in user_cols:
            conn.execute(text('ALTER TABLE users ADD COLUMN google_id VARCHAR(64)'))
            conn.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ix_users_google_id ON users (google_id)'))
        if 'avatar_url' not in user_cols:
            conn.execute(text('ALTER TABLE users ADD COLUMN avatar_url VARCHAR(500)'))
        if is_pg and not user_cols['password_hash'].get('nullable', True):
            conn.execute(text('ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL'))


with app.app_context():
    db.create_all()
    add_missing_columns()


# ── Helpers ──────────────────────────────────────────────────
def get_current_user():
    if 'user' not in g:
        uid = session.get('user_id')
        g.user = User.query.get(uid) if uid else None
    return g.user


def slugify(text):
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-') or 'shop'


def unique_vendor_slug(name):
    base = slugify(name)
    slug, n = base, 2
    while Vendor.query.filter_by(slug=slug).first():
        slug = f'{base}-{n}'
        n += 1
    return slug


def safe_next(target):
    """Only allow redirects to paths on this site."""
    if target and target.startswith('/') and not target.startswith('//'):
        return target
    return None


def _get_cart():
    return session.get('cart') or {'vendor_id': None, 'items': {}}


def cart_details():
    cart = _get_cart()
    vendor = Vendor.query.get(cart['vendor_id']) if cart['vendor_id'] else None
    lines, subtotal = [], 0
    for pid, qty in cart['items'].items():
        product = Product.query.get(int(pid))
        if not product:
            continue
        total = product.price * qty
        subtotal += total
        lines.append({'product': product, 'qty': qty, 'total': total})
    return {'vendor': vendor, 'lines': lines, 'subtotal': subtotal}


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not get_current_user():
            flash('Please log in to continue.', 'error')
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if not user:
            flash('Please log in to continue.', 'error')
            return redirect(url_for('login', next=request.path))
        if not user.is_admin:
            flash('You do not have permission to open that page.', 'error')
            return redirect(url_for('home'))
        return f(*args, **kwargs)
    return wrapper


def vendor_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if not user:
            flash('Please log in to continue.', 'error')
            return redirect(url_for('login', next=request.path))
        if not user.vendor:
            return redirect(url_for('vendor_apply'))
        return f(*args, **kwargs)
    return wrapper


# ── Template filters + globals ───────────────────────────────
@app.template_filter('ugx')
def ugx_filter(value):
    try:
        return 'UGX {:,.0f}'.format(float(value or 0))
    except (TypeError, ValueError):
        return 'UGX 0'


@app.context_processor
def inject_globals():
    cart = _get_cart()
    return dict(
        current_user=get_current_user(),
        cart_count=sum(cart['items'].values()),
        site_name=SITE_NAME,
        current_year=datetime.utcnow().year,
        status_labels=STATUS_LABELS,
        payment_methods=PAYMENT_METHODS,
        vendor_categories=VENDOR_CATEGORIES,
        delivery_fee=DELIVERY_FEE,
        trader_fee=TRADER_MONTHLY_FEE,
        photo=photo,
    )


# ── Public pages ─────────────────────────────────────────────
@app.route('/health')
def health():
    return 'ok', 200


@app.route('/')
def home():
    featured = Vendor.query.filter_by(is_approved=True, is_open=True).order_by(Vendor.date_added.desc()).limit(6).all()
    return render_template('home.html', featured=featured)


@app.route('/shops')
def shops():
    q = request.args.get('q', '').strip()
    category = request.args.get('category', '').strip()

    query = Vendor.query.filter_by(is_approved=True)
    if category:
        query = query.filter(Vendor.category == category)
    if q:
        pattern = f'%{q}%'
        query = query.filter(db.or_(Vendor.name.ilike(pattern),
                                    Vendor.description.ilike(pattern),
                                    Vendor.area.ilike(pattern)))
    vendors = query.order_by(Vendor.is_open.desc(), Vendor.name).all()
    return render_template('shops.html', vendors=vendors, q=q, category=category)


@app.route('/shops/<slug>')
def shop_detail(slug):
    vendor = Vendor.query.filter_by(slug=slug).first_or_404()
    user = get_current_user()
    can_preview = user and (user.id == vendor.owner_id or user.is_admin)
    if not vendor.is_approved and not can_preview:
        abort(404)
    products = Product.query.filter_by(vendor_id=vendor.id).order_by(Product.in_stock.desc(), Product.name).all()
    return render_template('shop_detail.html', vendor=vendor, products=products)


# ── Accounts ─────────────────────────────────────────────────
def log_in(user, message=None):
    """Remember this person in their browser for 30 days."""
    session.pop('user_id', None)
    session['user_id'] = user.id
    session.permanent = True
    flash(message or f'Welcome back, {user.name.split()[0]}!', 'success')


def role_for(email):
    return 'admin' if ADMIN_EMAIL and email == ADMIN_EMAIL else 'customer'


@app.route('/register', methods=['GET', 'POST'])
def register():
    if get_current_user():
        return redirect(url_for('home'))
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        phone = request.form.get('phone', '').strip()
        password = request.form.get('password', '')

        if not name or not email or not password:
            flash('Please fill in your name, email and password.', 'error')
            return redirect(url_for('register', next=request.args.get('next')))
        if len(password) < 8:
            flash('Your password must be at least 8 characters.', 'error')
            return redirect(url_for('register', next=request.args.get('next')))
        existing = User.query.filter_by(email=email).first()
        if existing:
            if existing.google_id and not existing.password_hash:
                flash('This email already has an account through Google. Tap "Continue with Google" to sign in.', 'error')
            else:
                flash('An account with this email already exists. Try logging in.', 'error')
            return redirect(url_for('login', next=request.args.get('next')))

        user = User(name=name, email=email, phone=phone,
                    password_hash=generate_password_hash(password), role=role_for(email))
        db.session.add(user)
        db.session.commit()
        log_in(user, f'Welcome to {SITE_NAME}, {user.name.split()[0]}!')
        return redirect(safe_next(request.args.get('next')) or url_for('home'))

    return render_template('register.html', google_on=bool(GOOGLE_CLIENT_ID))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if get_current_user():
        return redirect(safe_next(request.args.get('next')) or url_for('home'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user = User.query.filter_by(email=email).first()
        if user and not user.password_hash:
            flash('This account uses Google. Tap "Continue with Google" to sign in.', 'error')
            return redirect(url_for('login', next=request.args.get('next')))
        if user and check_password_hash(user.password_hash, password):
            log_in(user)
            return redirect(safe_next(request.args.get('next')) or url_for('home'))
        flash('Incorrect email or password.', 'error')
        return redirect(url_for('login', next=request.args.get('next')))
    return render_template('login.html', google_on=bool(GOOGLE_CLIENT_ID))


@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'success')
    return redirect(url_for('home'))


# ── "Continue with Google" ───────────────────────────────────
# 1. /auth/google sends the person to Google to pick their account.
# 2. Google sends them back to /auth/google/callback with their name and email.
# 3. We find their Katale account (or make one) and log them in.
@app.route('/auth/google')
def google_login():
    if not GOOGLE_CLIENT_ID:
        flash('Google sign-in is not switched on yet. Please use your email for now.', 'error')
        return redirect(url_for('login'))
    session['next_after_login'] = safe_next(request.args.get('next'))
    callback = url_for('google_callback', _external=True)   # follows whatever domain the site is on
    return oauth.google.authorize_redirect(callback, prompt='select_account')


@app.route('/auth/google/callback')
def google_callback():
    if not GOOGLE_CLIENT_ID:
        return redirect(url_for('login'))
    try:
        token = oauth.google.authorize_access_token()
    except Exception:
        flash('Google sign-in was cancelled or did not finish. Please try again.', 'error')
        return redirect(url_for('login'))

    info = token.get('userinfo') or {}
    email = (info.get('email') or '').strip().lower()
    if not email or not info.get('email_verified'):
        flash('Your Google account needs a confirmed email address to sign in.', 'error')
        return redirect(url_for('login'))

    google_id = info.get('sub')
    user = User.query.filter_by(google_id=google_id).first()
    is_new = False
    if not user:
        # Same email signed up before with a password? Use that same account.
        user = User.query.filter_by(email=email).first()
        if user:
            user.google_id = google_id
        else:
            user = User(name=info.get('name') or email.split('@')[0], email=email,
                        google_id=google_id, role=role_for(email))
            db.session.add(user)
            is_new = True
    if info.get('picture'):
        user.avatar_url = info['picture']
    db.session.commit()

    log_in(user, f'Welcome to {SITE_NAME}, {user.name.split()[0]}!' if is_new else None)
    return redirect(session.pop('next_after_login', None) or url_for('home'))


# ── Forgot password ──────────────────────────────────────────
# We email a link that works for 1 hour. The link stops working
# as soon as the password is changed, so it can only be used once.
def reset_serializer():
    return URLSafeTimedSerializer(app.config['SECRET_KEY'], salt='password-reset')


def make_reset_token(user):
    return reset_serializer().dumps({'id': user.id, 'h': (user.password_hash or '')[-12:]})


def user_from_reset_token(token):
    try:
        data = reset_serializer().loads(token, max_age=3600)
    except (BadSignature, SignatureExpired):
        return None
    user = User.query.get(data.get('id'))
    if not user or (user.password_hash or '')[-12:] != data.get('h'):
        return None
    return user


def send_email(to_email, to_name, subject, html):
    """Send one email through Brevo. Returns True if Brevo accepted it."""
    if not (BREVO_API_KEY and MAIL_SENDER):
        app.logger.warning('Email not sent: BREVO_API_KEY or MAIL_SENDER is missing')
        return False
    try:
        r = requests.post('https://api.brevo.com/v3/smtp/email', timeout=15,
                          headers={'api-key': BREVO_API_KEY, 'accept': 'application/json',
                                   'content-type': 'application/json'},
                          json={'sender': {'name': SITE_NAME, 'email': MAIL_SENDER},
                                'to': [{'email': to_email, 'name': to_name}],
                                'subject': subject, 'htmlContent': html})
        return r.status_code < 300
    except requests.RequestException:
        app.logger.exception('Brevo email failed')
        return False


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        user = User.query.filter_by(email=email).first()
        if user:
            link = url_for('reset_password', token=make_reset_token(user), _external=True)
            send_email(user.email, user.name, f'Reset your {SITE_NAME} password', render_template(
                'email_reset.html', user=user, link=link))
        # Same message either way, so nobody can use this page to check who has an account
        flash('If that email has a Katale account, we have sent a reset link. Check your inbox and spam folder.', 'success')
        return redirect(url_for('login'))
    return render_template('forgot_password.html')


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    user = user_from_reset_token(token)
    if not user:
        flash('That reset link has expired or was already used. Please ask for a new one.', 'error')
        return redirect(url_for('forgot_password'))
    if request.method == 'POST':
        password = request.form.get('password', '')
        if len(password) < 8:
            flash('Your password must be at least 8 characters.', 'error')
            return redirect(url_for('reset_password', token=token))
        user.password_hash = generate_password_hash(password)
        db.session.commit()
        log_in(user, 'Your password has been changed. You are now logged in.')
        return redirect(url_for('home'))
    return render_template('reset_password.html', user=user)


# ── Cart ─────────────────────────────────────────────────────
@app.route('/cart')
def cart():
    return render_template('cart.html', cart=cart_details())


@app.route('/cart/add', methods=['POST'])
def cart_add():
    product = Product.query.get_or_404(request.form.get('product_id', type=int))
    vendor = product.vendor
    qty = max(1, min(request.form.get('qty', 1, type=int), 50))

    if not vendor.is_approved or not vendor.is_open or not product.in_stock:
        flash(f'{product.name} is not available right now.', 'error')
        return redirect(url_for('shop_detail', slug=vendor.slug))

    current = _get_cart()
    if current['items'] and current['vendor_id'] != vendor.id:
        flash('Your cart has items from another shop. Clear it first to order from this one.', 'error')
        return redirect(url_for('cart'))

    current['vendor_id'] = vendor.id
    key = str(product.id)
    current['items'][key] = min(current['items'].get(key, 0) + qty, 50)
    session['cart'] = current
    flash(f'Added {product.name} to your cart.', 'success')
    return redirect(url_for('shop_detail', slug=vendor.slug))


@app.route('/cart/update', methods=['POST'])
def cart_update():
    current = _get_cart()
    key = str(request.form.get('product_id', type=int))
    qty = request.form.get('qty', 0, type=int)
    if request.form.get('action') == 'remove' or qty <= 0:
        current['items'].pop(key, None)
    elif key in current['items']:
        current['items'][key] = min(qty, 50)
    if not current['items']:
        current['vendor_id'] = None
    session['cart'] = current
    return redirect(url_for('cart'))


@app.route('/cart/clear', methods=['POST'])
def cart_clear():
    session.pop('cart', None)
    flash('Your cart is now empty.', 'success')
    return redirect(url_for('cart'))


# ── Checkout + orders ────────────────────────────────────────
@app.route('/checkout', methods=['GET', 'POST'])
@login_required
def checkout():
    user = get_current_user()
    details = cart_details()

    if not details['lines']:
        flash('Your cart is empty.', 'error')
        return redirect(url_for('shops'))

    vendor = details['vendor']
    if not vendor or not vendor.is_approved or not vendor.is_open:
        flash('That shop is not taking orders right now.', 'error')
        return redirect(url_for('cart'))

    unavailable = [l['product'].name for l in details['lines'] if not l['product'].in_stock]
    if unavailable:
        flash('No longer in stock: ' + ', '.join(unavailable) + '. Please remove them to continue.', 'error')
        return redirect(url_for('cart'))

    if request.method == 'POST':
        address = request.form.get('delivery_address', '').strip()
        phone = request.form.get('phone', '').strip()
        if not address or not phone:
            flash('Please give a delivery address and a phone number.', 'error')
            return redirect(url_for('checkout'))

        method = request.form.get('payment_method', 'cash')
        if method not in PAYMENT_METHODS:
            method = 'cash'

        order = Order(
            customer_id=user.id, vendor_id=vendor.id,
            delivery_address=address, phone=phone,
            notes=request.form.get('notes', '').strip(),
            payment_method=method,
            subtotal=details['subtotal'], delivery_fee=DELIVERY_FEE,
            total=details['subtotal'] + DELIVERY_FEE,
        )
        db.session.add(order)
        db.session.flush()  # gives us order.id before commit
        for line in details['lines']:
            db.session.add(OrderItem(
                order_id=order.id, product_id=line['product'].id,
                name=line['product'].name, unit_price=line['product'].price,
                quantity=line['qty'],
            ))
        if not user.default_address:
            user.default_address = address
        db.session.commit()

        session.pop('cart', None)
        flash('Order placed. The shop will confirm it shortly.', 'success')
        return redirect(url_for('order_detail', order_id=order.id))

    return render_template('checkout.html', cart=details, user=user)


@app.route('/orders')
@login_required
def my_orders():
    user = get_current_user()
    orders = Order.query.filter_by(customer_id=user.id).order_by(Order.date_placed.desc()).all()
    return render_template('my_orders.html', orders=orders)


@app.route('/orders/<int:order_id>')
@login_required
def order_detail(order_id):
    user = get_current_user()
    order = Order.query.get_or_404(order_id)
    allowed = (order.customer_id == user.id or order.vendor.owner_id == user.id or user.is_admin)
    if not allowed:
        abort(404)
    return render_template('order_detail.html', order=order)


@app.route('/orders/<int:order_id>/cancel', methods=['POST'])
@login_required
def cancel_order(order_id):
    user = get_current_user()
    order = Order.query.get_or_404(order_id)
    if order.customer_id != user.id:
        abort(404)
    if order.status != 'placed':
        flash('This order has already been accepted, so it cannot be cancelled here. Please call the shop.', 'error')
    else:
        order.status = 'cancelled'
        db.session.commit()
        flash('Your order has been cancelled.', 'success')
    return redirect(url_for('order_detail', order_id=order.id))


# ── Vendor side ──────────────────────────────────────────────
@app.route('/sell', methods=['GET', 'POST'])
@login_required
def vendor_apply():
    user = get_current_user()
    if user.vendor:
        return redirect(url_for('vendor_dashboard'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            flash('Please give your shop a name.', 'error')
            return redirect(url_for('vendor_apply'))
        vendor = Vendor(
            owner_id=user.id, name=name, slug=unique_vendor_slug(name),
            category=request.form.get('category', ''),
            description=request.form.get('description', '').strip(),
            area=request.form.get('area', '').strip(),
            phone=request.form.get('phone', '').strip() or user.phone,
            image_url=request.form.get('image_url', '').strip(),
        )
        if user.role == 'customer':
            user.role = 'vendor'
        db.session.add(vendor)
        db.session.commit()
        flash('Your shop has been submitted. You can add products now; it goes live once approved.', 'success')
        return redirect(url_for('vendor_dashboard'))

    return render_template('vendor_apply.html')


@app.route('/vendor')
@vendor_required
def vendor_dashboard():
    vendor = get_current_user().vendor
    orders = Order.query.filter_by(vendor_id=vendor.id).order_by(Order.date_placed.desc()).all()
    active = [o for o in orders if o.status not in ('delivered', 'cancelled')]
    past = [o for o in orders if o.status in ('delivered', 'cancelled')][:10]
    return render_template('vendor_dashboard.html', vendor=vendor, active=active, past=past,
                           next_statuses=VENDOR_NEXT_STATUS)


@app.route('/vendor/products/add', methods=['POST'])
@vendor_required
def vendor_add_product():
    vendor = get_current_user().vendor
    name = request.form.get('name', '').strip()
    try:
        price = float(request.form.get('price', ''))
    except ValueError:
        price = -1
    if not name or price < 0:
        flash('Please give the product a name and a valid price.', 'error')
        return redirect(url_for('vendor_dashboard'))
    db.session.add(Product(
        vendor_id=vendor.id, name=name, price=price,
        unit=request.form.get('unit', '').strip(),
        description=request.form.get('description', '').strip(),
        image_url=request.form.get('image_url', '').strip(),
    ))
    db.session.commit()
    flash(f'"{name}" added.', 'success')
    return redirect(url_for('vendor_dashboard'))


@app.route('/vendor/products/<int:product_id>/toggle', methods=['POST'])
@vendor_required
def vendor_toggle_product(product_id):
    vendor = get_current_user().vendor
    product = Product.query.filter_by(id=product_id, vendor_id=vendor.id).first_or_404()
    product.in_stock = not product.in_stock
    db.session.commit()
    return redirect(url_for('vendor_dashboard'))


@app.route('/vendor/toggle-open', methods=['POST'])
@vendor_required
def vendor_toggle_open():
    vendor = get_current_user().vendor
    vendor.is_open = not vendor.is_open
    db.session.commit()
    flash('Your shop is now open for orders.' if vendor.is_open else 'Your shop is now closed.', 'success')
    return redirect(url_for('vendor_dashboard'))


@app.route('/vendor/orders/<int:order_id>/status', methods=['POST'])
@vendor_required
def vendor_order_status(order_id):
    vendor = get_current_user().vendor
    order = Order.query.filter_by(id=order_id, vendor_id=vendor.id).first_or_404()
    new_status = request.form.get('status', '')
    if new_status not in VENDOR_NEXT_STATUS.get(order.status, []):
        flash('That status change is not allowed.', 'error')
        return redirect(url_for('vendor_dashboard'))
    order.status = new_status
    if new_status == 'delivered':
        order.payment_status = 'paid'
    db.session.commit()
    flash(f'{order.reference} marked as {order.status_label.lower()}.', 'success')
    return redirect(url_for('vendor_dashboard'))


# ── Admin ────────────────────────────────────────────────────
@app.route('/admin')
@admin_required
def admin_dashboard():
    vendors = Vendor.query.order_by(Vendor.is_approved, Vendor.date_added.desc()).all()
    recent_orders = Order.query.order_by(Order.date_placed.desc()).limit(15).all()
    return render_template('admin_dashboard.html', vendors=vendors, recent_orders=recent_orders)


@app.route('/admin/vendors/<int:vendor_id>/toggle-approval', methods=['POST'])
@admin_required
def admin_toggle_vendor(vendor_id):
    vendor = Vendor.query.get_or_404(vendor_id)
    vendor.is_approved = not vendor.is_approved
    db.session.commit()
    flash(f'{vendor.name} is now {"live" if vendor.is_approved else "hidden"}.', 'success')
    return redirect(url_for('admin_dashboard'))


if __name__ == '__main__':
    app.run(debug=True)
