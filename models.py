import math
import json
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta

db = SQLAlchemy()

def distance_km(lat1, lng1, lat2, lng2):
    """Straight-line distance in km between two points on the map (the 'haversine' formula)."""
    if None in (lat1, lng1, lat2, lng2):
        return None
    r = 6371.0  # Earth's radius in km
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


STATUS_LABELS = {
    'placed': 'Order placed',
    'accepted': 'Accepted',
    'preparing': 'Being prepared',
    'out_for_delivery': 'Out for delivery',
    'delivered': 'Delivered',
    'cancelled': 'Cancelled',
}


class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    phone = db.Column(db.String(30), nullable=True)
    # Empty for people who only ever sign in with Google
    password_hash = db.Column(db.String(255), nullable=True)
    # Google's permanent number for this person (set when they use "Continue with Google")
    google_id = db.Column(db.String(64), unique=True, nullable=True)
    avatar_url = db.Column(db.String(500), nullable=True)
    # How this person wants order alerts
    notify_whatsapp = db.Column(db.Boolean, default=True, nullable=False)
    notify_email = db.Column(db.Boolean, default=True, nullable=False)
    default_address = db.Column(db.String(300), nullable=True)
    # Last delivery spot this person used (so we remember it next time)
    default_lat = db.Column(db.Float, nullable=True)
    default_lng = db.Column(db.Float, nullable=True)
    # 'customer' / 'vendor' / 'admin'
    role = db.Column(db.String(20), default='customer', nullable=False)
    date_joined = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def is_admin(self):
        return self.role == 'admin'


class Vendor(db.Model):
    """A shop, restaurant, farm stall or butcher that sells on the platform."""
    __tablename__ = 'vendors'
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, unique=True)
    name = db.Column(db.String(150), nullable=False)
    slug = db.Column(db.String(160), nullable=False, unique=True)
    category = db.Column(db.String(60), nullable=True)
    description = db.Column(db.Text, nullable=True)
    area = db.Column(db.String(120), nullable=True)          # e.g. "Ntinda, Kampala"
    phone = db.Column(db.String(30), nullable=True)
    image_url = db.Column(db.String(500), nullable=True)
    is_approved = db.Column(db.Boolean, default=False, nullable=False)  # admin must approve
    is_open = db.Column(db.Boolean, default=True, nullable=False)       # vendor toggles this

    # Where the shop is, and how it delivers (traders deliver themselves for now)
    lat = db.Column(db.Float, nullable=True)
    lng = db.Column(db.Float, nullable=True)
    delivery_radius_km = db.Column(db.Float, default=5, nullable=False)    # how far they deliver
    delivery_base_fee = db.Column(db.Integer, default=2000, nullable=False)  # UGX, starting fee
    delivery_per_km = db.Column(db.Integer, default=1000, nullable=False)    # UGX for every km

    # How customers can pay this shop (money goes straight to the trader, never through Katale)
    accepts_cash = db.Column(db.Boolean, default=True, nullable=False)            # cash on delivery
    accepts_momo_on_delivery = db.Column(db.Boolean, default=True, nullable=False)  # mobile money at the door
    mtn_merchant = db.Column(db.String(20), nullable=True)      # MTN MoMoPay merchant code
    airtel_merchant = db.Column(db.String(20), nullable=True)   # Airtel Money Pay merchant code
    mtn_number = db.Column(db.String(20), nullable=True)        # MTN Mobile Money number (send money)
    airtel_number = db.Column(db.String(20), nullable=True)     # Airtel Money number (send money)
    momo_name = db.Column(db.String(80), nullable=True)         # name registered on those accounts

    # Mobile money charges the customer adds when paying to a NUMBER (so the trader isn't short when cashing out)
    momo_charge_mode = db.Column(db.String(10), default='none', nullable=False)   # none / bands / percent
    momo_charge_bands = db.Column(db.Text, nullable=True)       # JSON: [[up_to, charge], ...], up_to 0 = "more than that"
    momo_charge_percent = db.Column(db.Float, nullable=True)    # e.g. 2.0 means 2%
    momo_charge_min = db.Column(db.Integer, nullable=True)      # smallest charge when using a percentage

    @property
    def has_merchant(self):
        return bool(self.mtn_merchant or self.airtel_merchant)

    @property
    def has_momo_number(self):
        return bool(self.mtn_number or self.airtel_number)

    @property
    def accepts_pay_now(self):
        return self.has_merchant or self.has_momo_number

    @property
    def payment_choices(self):
        """The payment options this shop offers, in the order customers see them."""
        out = []
        if self.has_merchant:
            out.append('momo_merchant')      # MoMoPay / Airtel Money Pay code: no charge
        if self.has_momo_number:
            out.append('momo_number')        # send to number now: charge may apply
        if self.accepts_momo_on_delivery:
            out.append('mobile_money')       # mobile money at the door (to a number): charge may apply
        if self.accepts_cash or not out:
            out.append('cash')
        return out

    @property
    def charge_bands(self):
        try:
            rows = json.loads(self.momo_charge_bands or '[]')
            return [(int(a), int(b)) for a, b in rows]
        except (ValueError, TypeError):
            return []

    def momo_charge_for(self, amount, method):
        """Extra UGX the customer adds for this payment method on an order of this amount."""
        if method not in ('momo_number', 'mobile_money', 'momo_now'):
            return 0
        amount = float(amount or 0)
        if self.momo_charge_mode == 'percent' and self.momo_charge_percent:
            fee = max(amount * self.momo_charge_percent / 100.0, self.momo_charge_min or 0)
            return int(math.ceil(fee / 100.0) * 100)        # round up to the next UGX 100
        if self.momo_charge_mode == 'bands':
            bands = self.charge_bands
            for up_to, fee in sorted([b for b in bands if b[0] > 0]):
                if amount <= up_to:
                    return fee
            above = [fee for up_to, fee in bands if up_to == 0]
            return above[0] if above else (max([fee for _, fee in bands]) if bands else 0)
        return 0

    # Subscription: free month from approval, then paid 30 days at a time
    trial_ends = db.Column(db.DateTime, nullable=True)
    paid_until = db.Column(db.DateTime, nullable=True)
    last_reminder = db.Column(db.String(40), nullable=True)   # stops us sending the same reminder twice
    date_added = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def access_until(self):
        """The last moment the shop is visible to customers (end of trial or of paid time)."""
        dates = [d for d in (self.trial_ends, self.paid_until) if d]
        return max(dates) if dates else None

    @property
    def subscription_active(self):
        until = self.access_until
        return until is not None and until > datetime.utcnow()

    @property
    def on_trial(self):
        return bool(self.trial_ends and self.trial_ends > datetime.utcnow()
                    and (not self.paid_until or self.paid_until <= self.trial_ends))

    @property
    def days_left(self):
        until = self.access_until
        if not until:
            return 0
        return max(0, (until - datetime.utcnow()).days + (1 if (until - datetime.utcnow()).seconds else 0))

    @property
    def visible_to_customers(self):
        return self.is_approved and self.subscription_active

    def add_days(self, days):
        """Add paid days on top of whatever time the shop already has left."""
        start = max(self.access_until or datetime.utcnow(), datetime.utcnow())
        self.paid_until = start + timedelta(days=days)
        self.last_reminder = None

    @property
    def has_location(self):
        return self.lat is not None and self.lng is not None

    def distance_to(self, lat, lng):
        if not self.has_location:
            return None
        return distance_km(self.lat, self.lng, lat, lng)

    def delivers_to(self, lat, lng):
        d = self.distance_to(lat, lng)
        return d is not None and d <= (self.delivery_radius_km or 0)

    def delivery_fee_for(self, km):
        """Starting fee + price per km, rounded up to the nearest UGX 500."""
        fee = (self.delivery_base_fee or 0) + (self.delivery_per_km or 0) * km
        return int(math.ceil(fee / 500.0) * 500)

    owner = db.relationship('User', backref=db.backref('vendor', uselist=False))
    products = db.relationship('Product', backref='vendor', cascade='all, delete-orphan',
                               order_by='Product.name')


class Product(db.Model):
    __tablename__ = 'products'
    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('vendors.id'), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, nullable=True)
    price = db.Column(db.Numeric(10, 2), nullable=False)
    unit = db.Column(db.String(40), nullable=True)           # "kg", "bunch", "plate"
    image_url = db.Column(db.String(500), nullable=True)
    in_stock = db.Column(db.Boolean, default=True, nullable=False)
    date_added = db.Column(db.DateTime, default=datetime.utcnow)


class Order(db.Model):
    """One order = one customer buying from one vendor."""
    __tablename__ = 'orders'
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    vendor_id = db.Column(db.Integer, db.ForeignKey('vendors.id'), nullable=False)

    delivery_address = db.Column(db.String(300), nullable=False)
    phone = db.Column(db.String(30), nullable=False)
    delivery_lat = db.Column(db.Float, nullable=True)
    delivery_lng = db.Column(db.Float, nullable=True)
    delivery_landmark = db.Column(db.String(200), nullable=True)   # "blue gate opposite Shell"
    distance_km = db.Column(db.Float, nullable=True)
    notes = db.Column(db.Text, nullable=True)

    payment_method = db.Column(db.String(20), default='cash', nullable=False)   # 'cash' / 'mobile_money'
    payment_status = db.Column(db.String(20), default='unpaid', nullable=False)  # unpaid / claimed / paid / not_received
    payment_ref = db.Column(db.String(60), nullable=True)        # transaction ID the customer typed from their SMS
    payment_claimed_at = db.Column(db.DateTime, nullable=True)   # when the customer tapped "I've paid"

    subtotal = db.Column(db.Numeric(10, 2), nullable=False)
    delivery_fee = db.Column(db.Numeric(10, 2), default=0, nullable=False)
    momo_charge = db.Column(db.Numeric(10, 2), default=0, nullable=False)   # mobile money charges the customer added
    total = db.Column(db.Numeric(10, 2), nullable=False)

    status = db.Column(db.String(20), default='placed', nullable=False)
    date_placed = db.Column(db.DateTime, default=datetime.utcnow)
    seen_at = db.Column(db.DateTime, nullable=True)   # when the trader first saw it (stops the WhatsApp backup)
    accepted_at = db.Column(db.DateTime, nullable=True)    # step times, used by Insights
    delivered_at = db.Column(db.DateTime, nullable=True)
    cancelled_at = db.Column(db.DateTime, nullable=True)

    customer = db.relationship('User', backref='orders')
    vendor = db.relationship('Vendor', backref='orders')
    items = db.relationship('OrderItem', backref='order', cascade='all, delete-orphan')

    @property
    def maps_link(self):
        """Google Maps directions to the customer's pin (opens the app on phones)."""
        if self.delivery_lat is None:
            return None
        return f'https://www.google.com/maps/dir/?api=1&destination={self.delivery_lat},{self.delivery_lng}'

    @property
    def reference(self):
        return f'ORD-{self.id:05d}'

    @property
    def status_label(self):
        return STATUS_LABELS.get(self.status, self.status)


class OrderItem(db.Model):
    __tablename__ = 'order_items'
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True)
    # Snapshots, so old orders stay correct even if the product changes later
    name = db.Column(db.String(150), nullable=False)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)

    @property
    def line_total(self):
        return self.unit_price * self.quantity


class Payment(db.Model):
    """One subscription payment through Pesapal."""
    __tablename__ = 'payments'
    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('vendors.id'), nullable=False)
    reference = db.Column(db.String(60), unique=True, nullable=False)     # our own number, e.g. KAT-12-1730541234
    tracking_id = db.Column(db.String(80), unique=True, nullable=True)    # Pesapal's number for it
    amount = db.Column(db.Integer, nullable=False)                       # UGX
    days = db.Column(db.Integer, default=30, nullable=False)
    status = db.Column(db.String(20), default='pending', nullable=False)  # pending / completed / failed
    method = db.Column(db.String(60), nullable=True)                     # e.g. MTN Mobile Money
    confirmation_code = db.Column(db.String(80), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)
    note = db.Column(db.String(200), nullable=True)                      # e.g. "Free days given by admin"

    vendor = db.relationship('Vendor', backref=db.backref('payments', order_by='Payment.created_at.desc()'))


class Setting(db.Model):
    """Small saved values the app makes for itself (e.g. its push notification keys)."""
    __tablename__ = 'settings'
    key = db.Column(db.String(60), primary_key=True)
    value = db.Column(db.Text, nullable=False)


class PushSubscription(db.Model):
    """One phone or computer that agreed to receive Katale notifications."""
    __tablename__ = 'push_subscriptions'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    endpoint = db.Column(db.Text, unique=True, nullable=False)
    p256dh = db.Column(db.String(200), nullable=False)
    auth = db.Column(db.String(100), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
