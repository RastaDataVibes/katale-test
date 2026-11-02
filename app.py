import os
import re
import json
import time
import threading
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
from flask_wtf.csrf import CSRFProtect, CSRFError
import cloudinary
import cloudinary.uploader
from sqlalchemy import inspect, text
from models import db, User, Vendor, Product, Order, OrderItem, Payment, Setting, PushSubscription, STATUS_LABELS, distance_km

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

# ── Form protection (CSRF) ───────────────────────────────────
# Every form carries a secret code that only Katale can make, so other
# websites can't secretly submit forms (orders, settings) on someone's behalf.
app.config['WTF_CSRF_TIME_LIMIT'] = None          # codes last as long as the login does
csrf = CSRFProtect(app)


@app.errorhandler(CSRFError)
def csrf_failed(e):
    flash('That page had been open for a while, so we refreshed it. Please try again.', 'error')
    return redirect(request.referrer or url_for('home'))


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
# One or more admin emails, separated by commas: e.g. "me@gmail.com, partner@gmail.com"
ADMIN_EMAILS = {e.strip().lower() for e in (os.environ.get('ADMIN_EMAIL') or '').split(',') if e.strip()}
DELIVERY_FEE = 5000  # UGX, only used for shops that have not pinned their location yet
KAMPALA = (0.3476, 32.5825)  # where maps start if we know nothing else
TRADER_MONTHLY_FEE = 30000  # UGX for 30 days
TRIAL_DAYS = 30             # free days a new shop gets when it is approved
SUBSCRIPTION_DAYS = 30      # days each payment adds

VENDOR_CATEGORIES = ['Chicken & poultry', 'Groceries', 'Fresh produce', 'Meat & fish', 'Grains & cereals',
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
    'Chicken & poultry': 'v1793605606/pexels-laudiatsr-10886018_xlxrz7',   # packed whole chickens
    'Meat & fish':      'v1793605626/pexels-kenzero14-32228060_vooskg',     # sausages
    'Grains & cereals': 'v1793605589/pexels-macshamim-30689829_hcdbu3',
}


# ── Trader picture uploads (Cloudinary) ──────────────────────
# Render's Environment has either:
#   CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET  (same as Carcraaft)
#   or one CLOUDINARY_URL = cloudinary://KEY:SECRET@dcostjy6h (the library reads that by itself)
if os.environ.get('CLOUDINARY_API_KEY') and os.environ.get('CLOUDINARY_API_SECRET'):
    cloudinary.config(
        cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME', 'dcostjy6h').strip(),
        api_key=os.environ['CLOUDINARY_API_KEY'].strip(),
        api_secret=os.environ['CLOUDINARY_API_SECRET'].strip(),
        secure=True,
    )
CLOUDINARY_ON = bool(os.environ.get('CLOUDINARY_URL') or
                     (os.environ.get('CLOUDINARY_API_KEY') and os.environ.get('CLOUDINARY_API_SECRET')))
app.config['MAX_CONTENT_LENGTH'] = 12 * 1024 * 1024   # refuse uploads bigger than 12 MB
ALLOWED_IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/webp', 'image/heic', 'image/heif', 'image/gif'}


class UploadError(Exception):
    pass


def upload_image(file, folder):
    """Send one picture to Cloudinary, shrunk to at most 1200 px, and return its web address.
    Returns None if no file was chosen. Raises UploadError with a friendly message if it fails."""
    if not file or not file.filename:
        return None
    if file.mimetype not in ALLOWED_IMAGE_TYPES:
        raise UploadError('That file is not a photo. Please choose a JPG or PNG picture.')
    if not CLOUDINARY_ON:
        raise UploadError('Photo uploads are not switched on yet (CLOUDINARY_URL is missing in Render).')
    try:
        result = cloudinary.uploader.upload(
            file, folder=f'katale/{folder}', resource_type='image',
            transformation=[{'width': 1200, 'height': 1200, 'crop': 'limit', 'quality': 'auto:good'}],
        )
    except Exception:
        app.logger.exception('Cloudinary upload failed')
        raise UploadError('The photo could not be uploaded. Please try again, or try a smaller picture.')
    return result.get('secure_url')


def delete_image(url):
    """Remove an old Katale upload from Cloudinary (only ever touches the katale/ folder)."""
    if not (CLOUDINARY_ON and url and '/upload/' in url and '/katale/' in url):
        return
    public_id = url.split('/upload/', 1)[1]
    public_id = re.sub(r'^v\d+/', '', public_id).rsplit('.', 1)[0]
    try:
        cloudinary.uploader.destroy(public_id)
    except Exception:
        app.logger.warning('Could not delete old image %s', public_id)


def sized(url, width=600, height=None):
    """Ask Cloudinary for a smaller copy of an uploaded picture, cut to fit (for cards and thumbnails)."""
    if not url or 'res.cloudinary.com' not in url or '/upload/' not in url:
        return url
    size = f'w_{width},h_{height},c_fill' if height else f'w_{width},c_limit'
    return url.replace('/upload/', f'/upload/{size},q_auto,f_auto/', 1)


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


# New columns added after the app first went live: (table, column, type).
# When a later step adds a column to models.py, it also gets one line here.
NEW_COLUMNS = [
    ('users', 'google_id', 'VARCHAR(64)'),
    ('users', 'avatar_url', 'VARCHAR(500)'),
    ('users', 'default_lat', 'FLOAT'),
    ('users', 'default_lng', 'FLOAT'),
    ('vendors', 'lat', 'FLOAT'),
    ('vendors', 'lng', 'FLOAT'),
    ('vendors', 'delivery_radius_km', 'FLOAT NOT NULL DEFAULT 5'),
    ('vendors', 'delivery_base_fee', 'INTEGER NOT NULL DEFAULT 2000'),
    ('vendors', 'delivery_per_km', 'INTEGER NOT NULL DEFAULT 1000'),
    ('orders', 'delivery_lat', 'FLOAT'),
    ('orders', 'delivery_lng', 'FLOAT'),
    ('orders', 'delivery_landmark', 'VARCHAR(200)'),
    ('orders', 'distance_km', 'FLOAT'),
    ('vendors', 'trial_ends', 'TIMESTAMP'),
    ('vendors', 'paid_until', 'TIMESTAMP'),
    ('vendors', 'last_reminder', 'VARCHAR(40)'),
    ('users', 'notify_whatsapp', 'BOOLEAN NOT NULL DEFAULT TRUE'),
    ('users', 'notify_email', 'BOOLEAN NOT NULL DEFAULT TRUE'),
    ('orders', 'seen_at', 'TIMESTAMP'),
]


def add_missing_columns():
    """create_all() makes new tables but never changes old ones.
    This adds any new columns to tables that already exist, so the live
    database keeps up with models.py without losing any data."""
    insp = inspect(db.engine)
    is_pg = db.engine.dialect.name == 'postgresql'
    existing = {tbl: {c['name']: c for c in insp.get_columns(tbl)} for tbl in ('users', 'vendors', 'orders')}
    with db.engine.begin() as conn:
        for table, column, coltype in NEW_COLUMNS:
            if column not in existing[table]:
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {coltype}'))
        conn.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ix_users_google_id ON users (google_id)'))
        if is_pg and not existing['users']['password_hash'].get('nullable', True):
            conn.execute(text('ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL'))


def start_trials_for_existing_shops():
    """Shops approved before subscriptions existed get their free month from today."""
    for v in Vendor.query.filter(Vendor.is_approved.is_(True), Vendor.trial_ends.is_(None)).all():
        v.trial_ends = datetime.utcnow() + timedelta(days=TRIAL_DAYS)
    db.session.commit()


with app.app_context():
    db.create_all()
    add_missing_columns()
    start_trials_for_existing_shops()


# ── Helpers ──────────────────────────────────────────────────
def get_current_user():
    if 'user' not in g:
        uid = session.get('user_id')
        g.user = User.query.get(uid) if uid else None
        # Anyone whose email is in ADMIN_EMAIL becomes admin, even if they signed up before it was set
        if g.user and g.user.email in ADMIN_EMAILS and g.user.role != 'admin':
            g.user.role = 'admin'
            db.session.commit()
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


# ── Customer location ────────────────────────────────────────
# Kept in the browser session, so it works before people log in.
# Logged-in customers also get it saved on their account for next time.
def get_location():
    loc = session.get('loc')
    if not loc:
        user = get_current_user()
        if user and user.default_lat is not None:
            loc = {'lat': user.default_lat, 'lng': user.default_lng,
                   'label': short_label(user.default_address) or 'Saved location'}
            session['loc'] = loc
    return loc


def short_label(text_):
    """'Ntinda Road, Ntinda, Kampala, Uganda' -> 'Ntinda Road, Ntinda'"""
    if not text_:
        return ''
    parts = [p.strip() for p in text_.split(',') if p.strip()]
    return ', '.join(parts[:2])[:60]


def parse_point(lat, lng):
    """Turn two form values into numbers, only if they are a real spot in/near Uganda."""
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    if -2.0 <= lat <= 5.0 and 28.5 <= lng <= 36.0:
        return lat, lng
    return None


def delivery_quote(vendor, loc):
    """How far, how much, and whether this shop can deliver to the customer.
    Returns a dict the pages can show."""
    if not loc:
        return {'known': False, 'reaches': True, 'km': None, 'fee': None}
    if not vendor.has_location:
        return {'known': False, 'reaches': True, 'km': None, 'fee': DELIVERY_FEE}
    km = vendor.distance_to(loc['lat'], loc['lng'])
    return {'known': True, 'km': km, 'reaches': km <= vendor.delivery_radius_km,
            'fee': vendor.delivery_fee_for(km)}


def shops_near(query, loc):
    """Attach distance + fee to each shop. If we know where the customer is,
    hide shops that can't reach them and put the nearest first."""
    vendors = [v for v in query.all() if v.subscription_active]   # unpaid shops are hidden
    for v in vendors:
        v.quote = delivery_quote(v, loc)
    if not loc:
        return vendors, 0
    reachable = [v for v in vendors if v.quote['reaches']]
    too_far = len(vendors) - len(reachable)
    reachable.sort(key=lambda v: (not v.is_open, v.quote['km'] if v.quote['km'] is not None else 9999))
    return reachable, too_far


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
    quote = delivery_quote(vendor, get_location()) if vendor else None
    return {'vendor': vendor, 'lines': lines, 'subtotal': subtotal, 'quote': quote}


def whatsapp_number(phone):
    """'0772 123456' -> '256772123456' for wa.me links."""
    digits = re.sub(r'\D', '', phone or '')
    if digits.startswith('0'):
        digits = '256' + digits[1:]
    elif len(digits) == 9:
        digits = '256' + digits
    return digits


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


@app.template_filter('local')
def local_time(value, fmt='%d %b %Y, %H:%M'):
    """Times are saved in UTC; show them in Kampala time (UTC+3)."""
    if not value:
        return ''
    return (value + timedelta(hours=3)).strftime(fmt)


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
        sized=sized,
        uploads_on=CLOUDINARY_ON,
        customer_loc=get_location(),
        whatsapp_number=whatsapp_number,
        kampala=KAMPALA,
    )


# ── Public pages ─────────────────────────────────────────────
# ── Installable app (PWA) ────────────────────────────────────
@app.route('/manifest.webmanifest')
def manifest():
    data = {
        'name': 'Katale - your market, delivered', 'short_name': 'Katale',
        'description': 'Order chicken, groceries, fresh produce and more from shops near you.',
        'start_url': '/', 'scope': '/', 'display': 'standalone',
        'background_color': '#faf8f4', 'theme_color': '#1a3d2b',
        'icons': [
            {'src': '/static/img/gc-192.png', 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
            {'src': '/static/img/gc-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
            {'src': '/static/img/gc-maskable-192.png', 'sizes': '192x192', 'type': 'image/png', 'purpose': 'maskable'},
            {'src': '/static/img/gc-maskable-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'},
        ],
    }
    return app.response_class(json.dumps(data), mimetype='application/manifest+json')


@app.route('/sw.js')
def service_worker():
    # Served from the top of the site so it can work for every page
    resp = app.send_static_file('sw.js')
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['Service-Worker-Allowed'] = '/'
    return resp


@app.route('/offline')
def offline():
    return render_template('offline.html')


@app.route('/health')
def health():
    return 'ok', 200


@app.route('/')
def home():
    query = Vendor.query.filter_by(is_approved=True, is_open=True).order_by(Vendor.date_added.desc())
    featured, too_far = shops_near(query, get_location())
    return render_template('home.html', featured=featured[:6], too_far=too_far)


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
    vendors, too_far = shops_near(query.order_by(Vendor.is_open.desc(), Vendor.name), get_location())
    return render_template('shops.html', vendors=vendors, q=q, category=category, too_far=too_far)


@app.route('/shops/<slug>')
def shop_detail(slug):
    vendor = Vendor.query.filter_by(slug=slug).first_or_404()
    user = get_current_user()
    can_preview = user and (user.id == vendor.owner_id or user.is_admin)
    if not vendor.visible_to_customers and not can_preview:
        abort(404)
    products = Product.query.filter_by(vendor_id=vendor.id).order_by(Product.in_stock.desc(), Product.name).all()
    return render_template('shop_detail.html', vendor=vendor, products=products,
                           quote=delivery_quote(vendor, get_location()))


# ── Delivery location ("Delivering to ...") ──────────────────
@app.route('/location', methods=['GET', 'POST'])
def set_location():
    nxt = safe_next(request.values.get('next')) or url_for('home')
    if request.method == 'POST':
        point = parse_point(request.form.get('lat'), request.form.get('lng'))
        if not point:
            flash('Please choose your spot on the map first.', 'error')
            return redirect(url_for('set_location', next=nxt))
        address = request.form.get('address', '').strip()[:300]
        session['loc'] = {'lat': point[0], 'lng': point[1], 'label': short_label(address) or 'Pinned location'}
        user = get_current_user()
        if user:
            user.default_lat, user.default_lng = point
            if address:
                user.default_address = address
            db.session.commit()
        flash('Delivery location saved. Showing shops that deliver to you.', 'success')
        return redirect(nxt)
    return render_template('location.html', next=nxt)


# ── Accounts ─────────────────────────────────────────────────
def log_in(user, message=None):
    """Remember this person in their browser for 30 days."""
    session.pop('user_id', None)
    session['user_id'] = user.id
    session.permanent = True
    flash(message or f'Welcome back, {user.name.split()[0]}!', 'success')


def role_for(email):
    return 'admin' if email in ADMIN_EMAILS else 'customer'


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


# ── Login limits ─────────────────────────────────────────────
# After 5 wrong passwords for the same email (or from the same network)
# within 15 minutes, logging in is paused for 15 minutes.
FAILED_LOGINS = {}          # key -> list of times
MAX_TRIES, WINDOW = 5, timedelta(minutes=15)


def client_ip():
    return (request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0]).strip()


def too_many_tries(*keys):
    now = datetime.utcnow()
    for k in keys:
        FAILED_LOGINS[k] = [t for t in FAILED_LOGINS.get(k, []) if now - t < WINDOW]
        if len(FAILED_LOGINS[k]) >= MAX_TRIES:
            return True
    return False


def note_failed_try(*keys):
    for k in keys:
        FAILED_LOGINS.setdefault(k, []).append(datetime.utcnow())


@app.route('/login', methods=['GET', 'POST'])
def login():
    if get_current_user():
        return redirect(safe_next(request.args.get('next')) or url_for('home'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        keys = ('email:' + email, 'ip:' + client_ip())
        if too_many_tries(*keys):
            flash('Too many wrong tries. Please wait 15 minutes, or use "Forgot password?".', 'error')
            return redirect(url_for('login', next=request.args.get('next')))
        user = User.query.filter_by(email=email).first()
        if user and not user.password_hash:
            flash('This account uses Google. Tap "Continue with Google" to sign in.', 'error')
            return redirect(url_for('login', next=request.args.get('next')))
        if user and check_password_hash(user.password_hash, password):
            log_in(user)
            return redirect(safe_next(request.args.get('next')) or url_for('home'))
        note_failed_try(*keys)
        flash('Incorrect email or password.', 'error')
        return redirect(url_for('login', next=request.args.get('next')))
    return render_template('login.html', google_on=bool(GOOGLE_CLIENT_ID))


@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'success')
    return redirect(url_for('home'))


@app.route('/account', methods=['GET', 'POST'])
@login_required
def account():
    user = get_current_user()
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if name:
            user.name = name[:120]
        user.phone = request.form.get('phone', '').strip()[:30] or None
        if user.vendor:
            user.notify_whatsapp = bool(request.form.get('notify_whatsapp'))
        user.notify_email = bool(request.form.get('notify_email'))
        db.session.commit()
        flash('Saved.', 'success')
        return redirect(url_for('account'))
    return render_template('account.html')


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


# ── Order alerts ─────────────────────────────────────────────
# Like Glovo: free PUSH NOTIFICATIONS are the main alert (phone/computer pop-ups).
#   Trader:   push for every new order. If nobody has opened it after 3 minutes,
#             ONE WhatsApp message as a backup (the only WhatsApp Katale sends).
#   Customer: push at each step + the live tracker on the order page.
#             Email receipt when the order is delivered. No WhatsApp.

# Push notification keys ("VAPID"). Katale makes its own pair the first time it
# starts and saves them in the database, so there is nothing to set up in Render.
_vapid = {}


def vapid_keys():
    if not _vapid:
        priv = Setting.query.get('vapid_private')
        pub = Setting.query.get('vapid_public')
        if not (priv and pub):
            from py_vapid import Vapid01
            from cryptography.hazmat.primitives import serialization
            import base64
            v = Vapid01()
            v.generate_keys()
            priv_pem = v.private_pem().decode()
            raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
            pub_b64 = base64.urlsafe_b64encode(raw).decode().rstrip('=')
            priv, pub = Setting(key='vapid_private', value=priv_pem), Setting(key='vapid_public', value=pub_b64)
            db.session.add_all([priv, pub])
            db.session.commit()
        _vapid.update(private=priv.value, public=pub.value)
    return _vapid


def push_to_user(user_id, title, body, url='/', tag=None):
    """Send a pop-up to every phone/computer this person turned notifications on for."""
    from pywebpush import webpush, WebPushException
    keys = vapid_keys()
    payload = json.dumps({'title': title, 'body': body, 'url': url, 'tag': tag})
    sent = 0
    for sub_ in PushSubscription.query.filter_by(user_id=user_id).all():
        try:
            webpush(subscription_info={'endpoint': sub_.endpoint, 'keys': {'p256dh': sub_.p256dh, 'auth': sub_.auth}},
                    data=payload, vapid_private_key=keys['private'],
                    vapid_claims={'sub': f'mailto:{MAIL_SENDER or "support@katale.app"}'}, ttl=3600)
            sent += 1
        except WebPushException as ex:
            if ex.response is not None and ex.response.status_code in (404, 410):
                db.session.delete(sub_)          # that browser turned notifications off: forget it
        except Exception:
            app.logger.exception('Push failed')
    db.session.commit()
    return sent


# WhatsApp (backup for traders only). Switches on when these are in Render:
#   WHATSAPP_TOKEN, WHATSAPP_PHONE_ID   (from Meta's WhatsApp Business Platform)
# Uses one Meta-approved template: katale_new_order
#   {{1}} order ref, {{2}} customer name, {{3}} items, {{4}} total, {{5}} area + km, {{6}} link
WHATSAPP_TOKEN = os.environ.get('WHATSAPP_TOKEN', '').strip()
WHATSAPP_PHONE_ID = os.environ.get('WHATSAPP_PHONE_ID', '').strip()
WHATSAPP_ON = bool(WHATSAPP_TOKEN and WHATSAPP_PHONE_ID)
WHATSAPP_BACKUP_AFTER = 180   # seconds a new order can wait unseen before the WhatsApp backup goes out


def send_whatsapp_new_order(phone, params):
    number = whatsapp_number(phone)
    if not (WHATSAPP_ON and number and len(number) >= 11):
        return False
    body = {'messaging_product': 'whatsapp', 'to': number, 'type': 'template',
            'template': {'name': 'katale_new_order', 'language': {'code': 'en'},
                         'components': [{'type': 'body', 'parameters': [
                             {'type': 'text', 'text': str(p)[:900] or '-'} for p in params]}]}}
    try:
        r = requests.post(f'https://graph.facebook.com/v21.0/{WHATSAPP_PHONE_ID}/messages', timeout=20,
                          headers={'Authorization': f'Bearer {WHATSAPP_TOKEN}'}, json=body)
        if r.status_code >= 300:
            app.logger.warning('WhatsApp send failed (%s): %s', r.status_code, r.text[:300])
        return r.status_code < 300
    except requests.RequestException:
        app.logger.exception('WhatsApp send failed')
        return False


def in_background(fn, *args, delay=0):
    """Run a slow job (sending messages) without making the person wait."""
    def run():
        if delay:
            time.sleep(delay)
        with app.app_context():
            try:
                fn(*args)
            except Exception:
                app.logger.exception('Background job failed')
    threading.Thread(target=run, daemon=True).start()


def items_summary(order, limit=4):
    parts = [f'{i.quantity} x {i.name}' for i in order.items[:limit]]
    if len(order.items) > limit:
        parts.append(f'+{len(order.items) - limit} more')
    return ', '.join(parts)


def _push_new_order(order_id, link):
    order = Order.query.get(order_id)
    push_to_user(order.vendor.owner_id, f'New order {order.reference} - UGX {order.total:,.0f}',
                 f'{order.customer.name}: {items_summary(order)}. Tap to accept.', link, tag=f'order-{order.id}')


def _whatsapp_backup(order_id, link):
    """3 minutes later: if the trader still hasn't opened the order, send ONE WhatsApp."""
    order = Order.query.get(order_id)
    if not order or order.status != 'placed' or order.seen_at:
        return
    vendor, owner = order.vendor, order.vendor.owner
    if not (owner.notify_whatsapp and vendor.phone):
        return
    area = short_label(order.delivery_address) + (f' ({order.distance_km:.1f} km)' if order.distance_km else '')
    send_whatsapp_new_order(vendor.phone, [order.reference, order.customer.name, items_summary(order),
                                           f'UGX {order.total:,.0f}', area, link])


STATUS_PUSH = {
    'accepted': ('Order accepted', '{shop} has accepted your order.'),
    'preparing': ('Being prepared', '{shop} is getting your order ready.'),
    'out_for_delivery': ('On the way', 'Your order from {shop} is on the way. Keep your phone near you.'),
    'delivered': ('Delivered', 'Enjoy! Your receipt is in your email.'),
    'cancelled': ('Order cancelled', '{shop} could not take your order. You have not been charged.'),
}


def _alert_status(order_id, link):
    order = Order.query.get(order_id)
    title, body = STATUS_PUSH.get(order.status, (order.status_label, ''))
    push_to_user(order.customer_id, f'{title} - {order.reference}', body.format(shop=order.vendor.name), link,
                 tag=f'order-{order.id}')
    if order.status == 'delivered' and order.customer.notify_email:
        send_email(order.customer.email, order.customer.name, f'Your receipt for {order.reference}',
                   render_template('email_order_receipt.html', order=order, link=link))


def _ring_again(order_id, link, n):
    """Re-send the new-order pop-up (it rings again) if nobody has opened the order yet."""
    order = Order.query.get(order_id)
    if order and order.status == 'placed' and not order.seen_at:
        push_to_user(order.vendor.owner_id, f'Waiting: {order.reference} - UGX {order.total:,.0f}',
                     f'{order.customer.name} is waiting for you to accept ({n} min). Tap to open.', link,
                     tag=f'order-{order.id}')


def alert_new_order(order):
    # Tapping the pop-up opens My shop and marks the order as seen (which stops the reminders)
    link = url_for('vendor_dashboard', seen='all', _external=True)
    in_background(_push_new_order, order.id, link)
    in_background(_ring_again, order.id, link, 1, delay=60)
    in_background(_ring_again, order.id, link, 2, delay=120)
    in_background(_whatsapp_backup, order.id, link, delay=WHATSAPP_BACKUP_AFTER)


def alert_status(order):
    in_background(_alert_status, order.id, url_for('order_detail', order_id=order.id, _external=True))


@app.route('/push/key')
def push_key():
    return {'key': vapid_keys()['public']}


@app.route('/push/subscribe', methods=['POST'])
@login_required
def push_subscribe():
    data = request.get_json(silent=True) or {}
    keys = data.get('keys') or {}
    if not (data.get('endpoint') and keys.get('p256dh') and keys.get('auth')):
        return {'ok': False}, 400
    sub_ = PushSubscription.query.filter_by(endpoint=data['endpoint']).first() or PushSubscription(endpoint=data['endpoint'])
    sub_.user_id, sub_.p256dh, sub_.auth = get_current_user().id, keys['p256dh'], keys['auth']
    db.session.add(sub_)
    db.session.commit()
    return {'ok': True}


@app.route('/push/test', methods=['POST'])
@login_required
def push_test():
    n = push_to_user(get_current_user().id, 'Katale notifications are on',
                     'You will get a pop-up like this for your orders.', url_for('home'))
    return {'sent': n}


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        if too_many_tries('reset:' + email, 'resetip:' + client_ip()):
            flash('We already sent several reset links. Please check your inbox and spam, or wait 15 minutes.', 'error')
            return redirect(url_for('login'))
        note_failed_try('reset:' + email, 'resetip:' + client_ip())
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

    if not vendor.visible_to_customers or not vendor.is_open or not product.in_stock:
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
    if not vendor or not vendor.visible_to_customers or not vendor.is_open:
        flash('That shop is not taking orders right now.', 'error')
        return redirect(url_for('cart'))

    unavailable = [l['product'].name for l in details['lines'] if not l['product'].in_stock]
    if unavailable:
        flash('No longer in stock: ' + ', '.join(unavailable) + '. Please remove them to continue.', 'error')
        return redirect(url_for('cart'))

    if request.method == 'POST':
        point = parse_point(request.form.get('lat'), request.form.get('lng'))
        address = request.form.get('delivery_address', '').strip()
        phone = request.form.get('phone', '').strip()
        landmark = request.form.get('landmark', '').strip()[:200]
        if not point:
            flash('Please drop the pin on the map where you want your order delivered.', 'error')
            return redirect(url_for('checkout'))
        if not address or not phone:
            flash('Please give a delivery address and a phone number.', 'error')
            return redirect(url_for('checkout'))

        # Work out the fee again here on the server, from the exact pin,
        # so nobody can change the price in their browser.
        loc = {'lat': point[0], 'lng': point[1], 'label': short_label(address)}
        quote = delivery_quote(vendor, loc)
        if not quote['reaches']:
            flash(f'{vendor.name} only delivers up to {vendor.delivery_radius_km:g} km away, and your pin is '
                  f'{quote["km"]:.1f} km away. Move the pin or choose a shop nearer to you.', 'error')
            return redirect(url_for('checkout'))
        fee = quote['fee']

        method = request.form.get('payment_method', 'cash')
        if method not in PAYMENT_METHODS:
            method = 'cash'

        order = Order(
            customer_id=user.id, vendor_id=vendor.id,
            delivery_address=address[:300], phone=phone,
            delivery_lat=point[0], delivery_lng=point[1], delivery_landmark=landmark,
            distance_km=round(quote['km'], 2) if quote['km'] is not None else None,
            notes=request.form.get('notes', '').strip(),
            payment_method=method,
            subtotal=details['subtotal'], delivery_fee=fee,
            total=details['subtotal'] + fee,
        )
        db.session.add(order)
        db.session.flush()  # gives us order.id before commit
        for line in details['lines']:
            db.session.add(OrderItem(
                order_id=order.id, product_id=line['product'].id,
                name=line['product'].name, unit_price=line['product'].price,
                quantity=line['qty'],
            ))
        # Remember this spot for next time
        user.default_address, user.default_lat, user.default_lng = address[:300], point[0], point[1]
        if not user.phone:
            user.phone = phone
        session['loc'] = loc
        db.session.commit()

        session.pop('cart', None)
        alert_new_order(order)
        flash('Order placed. The shop will confirm it shortly.', 'success')
        return redirect(url_for('order_detail', order_id=order.id))

    return render_template('checkout.html', cart=details, user=user, loc=get_location())


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
    if order.vendor.owner_id == user.id and not order.seen_at:
        order.seen_at = datetime.utcnow()
        db.session.commit()
    return render_template('order_detail.html', order=order)


@app.route('/orders/<int:order_id>/status.json')
@login_required
def order_status_json(order_id):
    """The order page asks this every 20 seconds to update the progress bar."""
    user = get_current_user()
    order = Order.query.get_or_404(order_id)
    if not (order.customer_id == user.id or order.vendor.owner_id == user.id or user.is_admin):
        abort(404)
    return {'status': order.status, 'label': order.status_label}


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
        in_background(push_to_user, order.vendor.owner_id, f'{order.reference} was cancelled',
                      f'{user.name} cancelled their order.', url_for('vendor_dashboard', _external=True))
        flash('Your order has been cancelled.', 'success')
    return redirect(url_for('order_detail', order_id=order.id))


# ── Vendor side ──────────────────────────────────────────────
def read_shop_form(vendor, fallback_phone=None):
    """Copy the shop form (details, map pin, delivery settings) onto a vendor.
    Returns an error message, or None if everything is fine."""
    name = request.form.get('name', '').strip()
    if not name:
        return 'Please give your shop a name.'
    point = parse_point(request.form.get('lat'), request.form.get('lng'))
    if not point:
        return 'Please put your shop on the map: tap "Use my location" or drag the pin to your shop.'

    def number(field, default, low, high):
        try:
            value = float(request.form.get(field, default))
        except (TypeError, ValueError):
            value = default
        return max(low, min(high, value))

    vendor.name = name
    vendor.category = request.form.get('category', '')
    vendor.description = request.form.get('description', '').strip()
    vendor.area = request.form.get('area', '').strip()[:120]
    vendor.phone = request.form.get('phone', '').strip() or fallback_phone
    vendor.lat, vendor.lng = point
    vendor.delivery_radius_km = number('delivery_radius_km', 5, 0.5, 30)
    vendor.delivery_base_fee = int(number('delivery_base_fee', 2000, 0, 50000))
    vendor.delivery_per_km = int(number('delivery_per_km', 1000, 0, 10000))

    # Shop photo: a new upload replaces the old one; "remove" clears it
    try:
        new_url = upload_image(request.files.get('photo'), 'shops')
    except UploadError as e:
        return str(e)
    if new_url:
        delete_image(vendor.image_url)
        vendor.image_url = new_url
    elif request.form.get('remove_photo'):
        delete_image(vendor.image_url)
        vendor.image_url = None
    return None


@app.route('/sell', methods=['GET', 'POST'])
@login_required
def vendor_apply():
    user = get_current_user()
    if user.vendor:
        return redirect(url_for('vendor_dashboard'))

    if request.method == 'POST':
        vendor = Vendor()   # not saved yet
        error = read_shop_form(vendor, user.phone)
        if error:
            flash(error, 'error')
            return render_template('vendor_apply.html', form=request.form)
        vendor.slug = unique_vendor_slug(vendor.name)
        vendor.owner_id = user.id
        if user.role == 'customer':
            user.role = 'vendor'
        db.session.add(vendor)
        db.session.commit()
        flash('Your shop has been submitted. You can add products now; it goes live once approved.', 'success')
        return redirect(url_for('vendor_dashboard'))

    return render_template('vendor_apply.html', form={})


@app.route('/vendor/settings', methods=['GET', 'POST'])
@vendor_required
def vendor_settings():
    vendor = get_current_user().vendor
    if request.method == 'POST':
        error = read_shop_form(vendor, vendor.owner.phone)
        if error:
            db.session.rollback()
            flash(error, 'error')
            return redirect(url_for('vendor_settings'))
        db.session.commit()
        flash('Shop settings saved.', 'success')
        return redirect(url_for('vendor_dashboard'))
    return render_template('vendor_settings.html', vendor=vendor)


@app.route('/vendor')
@vendor_required
def vendor_dashboard():
    vendor = get_current_user().vendor
    orders = Order.query.filter_by(vendor_id=vendor.id).order_by(Order.date_placed.desc()).all()
    # An order only counts as "seen" when someone actually taps (the pop-up, or "Show new order"),
    # not just because the page is open on a phone nobody is looking at.
    if request.args.get('seen') == 'all':
        for o in orders:
            if o.status == 'placed' and not o.seen_at:
                o.seen_at = datetime.utcnow()
        db.session.commit()
        return redirect(url_for('vendor_dashboard'))
    active = [o for o in orders if o.status not in ('delivered', 'cancelled')]
    past = [o for o in orders if o.status in ('delivered', 'cancelled')][:10]
    return render_template('vendor_dashboard.html', vendor=vendor, active=active, past=past,
                           next_statuses=VENDOR_NEXT_STATUS)


def read_product_form(product):
    """Copy the product form onto a product. Returns an error message or None."""
    name = request.form.get('name', '').strip()
    try:
        price = float(request.form.get('price', ''))
    except ValueError:
        price = -1
    if not name or price < 0:
        return 'Please give the product a name and a valid price.'
    product.name = name[:150]
    product.price = price
    product.unit = request.form.get('unit', '').strip()[:40]
    product.description = request.form.get('description', '').strip()
    try:
        new_url = upload_image(request.files.get('photo'), 'products')
    except UploadError as e:
        return str(e)
    if new_url:
        delete_image(product.image_url)
        product.image_url = new_url
    elif request.form.get('remove_photo'):
        delete_image(product.image_url)
        product.image_url = None
    return None


@app.route('/vendor/poll')
@vendor_required
def vendor_poll():
    """My shop asks this every 30 seconds: is there a newer order than the one I'm showing?"""
    vendor = get_current_user().vendor
    unseen = Order.query.filter_by(vendor_id=vendor.id, status='placed', seen_at=None).count()
    return {'unseen': unseen}


@app.route('/vendor/products/add', methods=['POST'])
@vendor_required
def vendor_add_product():
    vendor = get_current_user().vendor
    product = Product(vendor_id=vendor.id)
    error = read_product_form(product)
    if error:
        flash(error, 'error')
        return redirect(url_for('vendor_dashboard') + '#add-product')
    db.session.add(product)
    db.session.commit()
    flash(f'"{product.name}" added.', 'success')
    return redirect(url_for('vendor_dashboard') + '#products')


@app.route('/vendor/products/<int:product_id>/edit', methods=['GET', 'POST'])
@vendor_required
def vendor_edit_product(product_id):
    vendor = get_current_user().vendor
    product = Product.query.filter_by(id=product_id, vendor_id=vendor.id).first_or_404()
    if request.method == 'POST':
        error = read_product_form(product)
        if error:
            db.session.rollback()
            flash(error, 'error')
            return redirect(url_for('vendor_edit_product', product_id=product.id))
        db.session.commit()
        flash(f'"{product.name}" saved.', 'success')
        return redirect(url_for('vendor_dashboard') + '#products')
    return render_template('vendor_product.html', product=product)


@app.route('/vendor/products/<int:product_id>/delete', methods=['POST'])
@vendor_required
def vendor_delete_product(product_id):
    vendor = get_current_user().vendor
    product = Product.query.filter_by(id=product_id, vendor_id=vendor.id).first_or_404()
    # Old orders keep their own copy of the name and price, so we just unlink them
    OrderItem.query.filter_by(product_id=product.id).update({'product_id': None})
    delete_image(product.image_url)
    name = product.name
    db.session.delete(product)
    db.session.commit()
    # Take it out of anyone's cart too
    cart = _get_cart()
    if str(product_id) in cart['items']:
        cart['items'].pop(str(product_id))
        session['cart'] = cart
    flash(f'"{name}" deleted.', 'success')
    return redirect(url_for('vendor_dashboard') + '#products')


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
    order.seen_at = order.seen_at or datetime.utcnow()
    if new_status == 'delivered':
        order.payment_status = 'paid'
    db.session.commit()
    alert_status(order)
    flash(f'{order.reference} marked as {order.status_label.lower()}.', 'success')
    return redirect(url_for('vendor_dashboard'))


# ── Trader subscriptions (Pesapal) ───────────────────────────
# Same Gridcraft Pesapal account as Carcraaft. In Render's Environment:
#   PESAPAL_CONSUMER_KEY, PESAPAL_CONSUMER_SECRET   (copied from Carcraaft)
#   PESAPAL_ENV = live  (or "sandbox" for testing)
#   PESAPAL_IPN_ID      (optional; if missing we register our IPN address ourselves)
PESAPAL_KEY = os.environ.get('PESAPAL_CONSUMER_KEY', '').strip()
PESAPAL_SECRET = os.environ.get('PESAPAL_CONSUMER_SECRET', '').strip()
PESAPAL_BASE = ('https://cybqa.pesapal.com/pesapalv3' if os.environ.get('PESAPAL_ENV', 'live').lower() == 'sandbox'
                else 'https://pay.pesapal.com/v3')
_pesapal_ipn_id = os.environ.get('PESAPAL_IPN_ID', '').strip()


class PesapalError(Exception):
    pass


def pesapal_token():
    if not (PESAPAL_KEY and PESAPAL_SECRET):
        raise PesapalError('Payments are not switched on yet (Pesapal keys are missing in Render).')
    r = requests.post(f'{PESAPAL_BASE}/api/Auth/RequestToken', timeout=20,
                      headers={'Accept': 'application/json', 'Content-Type': 'application/json'},
                      json={'consumer_key': PESAPAL_KEY, 'consumer_secret': PESAPAL_SECRET})
    data = r.json() if r.content else {}
    if not data.get('token'):
        app.logger.error('Pesapal token failed: %s', data)
        raise PesapalError('Could not connect to Pesapal. Please try again in a minute.')
    return data['token']


def pesapal_headers(token):
    return {'Accept': 'application/json', 'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'}


def pesapal_ipn_id(token):
    """Pesapal needs to know the address it should ping when a payment changes.
    We register it once and remember the id."""
    global _pesapal_ipn_id
    if _pesapal_ipn_id:
        return _pesapal_ipn_id
    r = requests.post(f'{PESAPAL_BASE}/api/URLSetup/RegisterIPN', timeout=20, headers=pesapal_headers(token),
                      json={'url': url_for('pesapal_ipn', _external=True), 'ipn_notification_type': 'GET'})
    data = r.json() if r.content else {}
    if not data.get('ipn_id'):
        app.logger.error('Pesapal IPN registration failed: %s', data)
        raise PesapalError('Could not set up payments with Pesapal. Please try again.')
    _pesapal_ipn_id = data['ipn_id']
    app.logger.info('Pesapal IPN registered: %s (you can save this as PESAPAL_IPN_ID in Render)', _pesapal_ipn_id)
    return _pesapal_ipn_id


def pesapal_start_payment(payment, user):
    """Ask Pesapal for a payment page. Returns the web address to send the trader to."""
    token = pesapal_token()
    first, _, last = (user.name or 'Katale trader').partition(' ')
    body = {
        'id': payment.reference,
        'currency': 'UGX',
        'amount': payment.amount,
        'description': f'Katale shop subscription - {payment.vendor.name} - {payment.days} days'[:100],
        'callback_url': url_for('pesapal_callback', _external=True),
        'notification_id': pesapal_ipn_id(token),
        'billing_address': {'email_address': user.email, 'phone_number': payment.vendor.phone or user.phone or '',
                            'country_code': 'UG', 'first_name': first, 'last_name': last},
    }
    r = requests.post(f'{PESAPAL_BASE}/api/Transactions/SubmitOrderRequest', timeout=25,
                      headers=pesapal_headers(token), json=body)
    data = r.json() if r.content else {}
    if not data.get('redirect_url'):
        app.logger.error('Pesapal order failed: %s', data)
        raise PesapalError('Pesapal could not start the payment. Please try again.')
    payment.tracking_id = data.get('order_tracking_id')
    return data['redirect_url']


def pesapal_check(payment):
    """Ask Pesapal whether this payment went through, and give the shop its days if it did.
    Safe to call many times: days are only added once."""
    if payment.status == 'completed' or not payment.tracking_id:
        return payment.status
    token = pesapal_token()
    r = requests.get(f'{PESAPAL_BASE}/api/Transactions/GetTransactionStatus', timeout=20,
                     headers=pesapal_headers(token), params={'orderTrackingId': payment.tracking_id})
    data = r.json() if r.content else {}
    code = data.get('status_code')           # 1 = paid, 2 = failed, 3 = reversed, 0 = not finished
    payment.method = data.get('payment_method') or payment.method
    payment.confirmation_code = data.get('confirmation_code') or payment.confirmation_code
    if code == 1 and int(float(data.get('amount') or 0)) >= payment.amount:
        payment.status = 'completed'
        payment.completed_at = datetime.utcnow()
        payment.vendor.add_days(payment.days)
        db.session.commit()
        owner = payment.vendor.owner
        send_email(owner.email, owner.name, f'Payment received - your {SITE_NAME} shop is active',
                   render_template('email_receipt.html', payment=payment, vendor=payment.vendor))
    elif code in (2, 3):
        payment.status = 'failed'
        db.session.commit()
    else:
        db.session.commit()
    return payment.status


@app.route('/vendor/subscription')
@vendor_required
def vendor_subscription():
    vendor = get_current_user().vendor
    return render_template('vendor_subscription.html', vendor=vendor,
                           payments=[p for p in vendor.payments][:12],
                           payments_on=bool(PESAPAL_KEY and PESAPAL_SECRET))


@app.route('/vendor/subscription/pay', methods=['POST'])
@vendor_required
def vendor_pay():
    user = get_current_user()
    vendor = user.vendor
    if not vendor.is_approved:
        flash('You can pay once your shop has been approved. Your free month starts then.', 'error')
        return redirect(url_for('vendor_subscription'))
    payment = Payment(vendor=vendor, amount=TRADER_MONTHLY_FEE, days=SUBSCRIPTION_DAYS,
                      reference=f'KAT-{vendor.id}-{int(datetime.utcnow().timestamp())}')
    db.session.add(payment)
    try:
        pay_url = pesapal_start_payment(payment, user)
    except (PesapalError, requests.RequestException) as e:
        db.session.rollback()
        flash(str(e) if isinstance(e, PesapalError) else 'Could not reach Pesapal. Please try again.', 'error')
        return redirect(url_for('vendor_subscription'))
    db.session.commit()
    return redirect(pay_url)


@app.route('/payments/pesapal/callback')
def pesapal_callback():
    """Pesapal sends the trader back here after paying."""
    payment = Payment.query.filter_by(tracking_id=request.args.get('OrderTrackingId')).first()
    if not payment:
        flash('We could not find that payment. If money left your account, please contact us.', 'error')
        return redirect(url_for('vendor_subscription') if get_current_user() else url_for('home'))
    try:
        status = pesapal_check(payment)
    except (PesapalError, requests.RequestException):
        status = payment.status
    if status == 'completed':
        flash(f'Payment received. Your shop is active until {payment.vendor.access_until:%d %b %Y}.', 'success')
    elif status == 'failed':
        flash('The payment did not go through. No money was taken. Please try again.', 'error')
    else:
        flash('Your payment is being confirmed. This page will update in a minute or two.', 'info')
    return redirect(url_for('vendor_subscription'))


@app.route('/payments/pesapal/ipn')
def pesapal_ipn():
    """Pesapal pings this address (even if the trader closed the page) when a payment changes."""
    tracking = request.args.get('OrderTrackingId', '')
    reference = request.args.get('OrderMerchantReference', '')
    payment = Payment.query.filter_by(tracking_id=tracking).first() or Payment.query.filter_by(reference=reference).first()
    status = 500
    if payment:
        try:
            if not payment.tracking_id:
                payment.tracking_id = tracking
            pesapal_check(payment)
            status = 200
        except (PesapalError, requests.RequestException):
            app.logger.exception('Pesapal IPN check failed')
    return {'orderNotificationType': request.args.get('OrderNotificationType', 'IPNCHANGE'),
            'orderTrackingId': tracking, 'orderMerchantReference': reference, 'status': status}


# ── Daily jobs (reminders) ───────────────────────────────────
# GitHub Actions calls this once a day with the secret key (see .github/workflows/daily.yml).
CRON_SECRET = (os.environ.get('CRON_KEY') or os.environ.get('CRON_SECRET') or '').strip()   # same name as Carcraaft


@app.route('/tasks/daily', methods=['GET', 'POST'])
@csrf.exempt
def daily_tasks():
    if not CRON_SECRET or request.args.get('key') != CRON_SECRET:
        abort(404)
    sent = 0
    for v in Vendor.query.filter_by(is_approved=True).all():
        until = v.access_until
        if not until:
            continue
        days = (until - datetime.utcnow()).total_seconds() / 86400
        tag = None
        if 0 < days <= 1:
            tag = '1d'
        elif 1 < days <= 7:
            tag = '7d'
        elif -1 < days <= 0:
            tag = 'ended'
        if not tag:
            continue
        key = f'{tag}:{until:%Y-%m-%d}'
        if v.last_reminder == key:
            continue          # already sent this one
        ok = send_email(v.owner.email, v.owner.name,
                        {'7d': f'Your {SITE_NAME} shop: 7 days left',
                         '1d': f'Your {SITE_NAME} shop ends tomorrow',
                         'ended': f'Your {SITE_NAME} shop is now hidden'}[tag],
                        render_template('email_reminder.html', vendor=v, tag=tag,
                                        pay_link=url_for('vendor_subscription', _external=True)))
        if ok:
            v.last_reminder = key
            sent += 1
    # Check payments that were started but never confirmed (e.g. Pesapal ping missed)
    for p in Payment.query.filter_by(status='pending').filter(
            Payment.created_at > datetime.utcnow() - timedelta(days=3)).all():
        try:
            pesapal_check(p)
        except Exception:
            pass
    db.session.commit()
    return {'ok': True, 'reminders_sent': sent}


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
    if vendor.is_approved and not vendor.trial_ends:
        vendor.trial_ends = datetime.utcnow() + timedelta(days=TRIAL_DAYS)   # free month starts now
    db.session.commit()
    flash(f'{vendor.name} is now {"live" if vendor.is_approved else "hidden"}.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/vendors/<int:vendor_id>/free-days', methods=['POST'])
@admin_required
def admin_free_days(vendor_id):
    """Give a shop free days by hand (e.g. family, a promotion, or a payment made outside Pesapal)."""
    vendor = Vendor.query.get_or_404(vendor_id)
    try:
        days = max(1, min(366, int(request.form.get('days', 30))))
    except ValueError:
        days = 30
    vendor.add_days(days)
    db.session.add(Payment(vendor=vendor, amount=0, days=days, status='completed', completed_at=datetime.utcnow(),
                           reference=f'FREE-{vendor.id}-{int(datetime.utcnow().timestamp())}',
                           note=f'{days} free days given by admin'))
    db.session.commit()
    flash(f'{vendor.name} got {days} free days. Active until {vendor.access_until:%d %b %Y}.', 'success')
    return redirect(url_for('admin_dashboard'))


if __name__ == '__main__':
    app.run(debug=True)
