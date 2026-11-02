from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

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
    default_address = db.Column(db.String(300), nullable=True)
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
    date_added = db.Column(db.DateTime, default=datetime.utcnow)

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
    notes = db.Column(db.Text, nullable=True)

    payment_method = db.Column(db.String(20), default='cash', nullable=False)   # 'cash' / 'mobile_money'
    payment_status = db.Column(db.String(20), default='unpaid', nullable=False)  # 'unpaid' / 'paid'

    subtotal = db.Column(db.Numeric(10, 2), nullable=False)
    delivery_fee = db.Column(db.Numeric(10, 2), default=0, nullable=False)
    total = db.Column(db.Numeric(10, 2), nullable=False)

    status = db.Column(db.String(20), default='placed', nullable=False)
    date_placed = db.Column(db.DateTime, default=datetime.utcnow)

    customer = db.relationship('User', backref='orders')
    vendor = db.relationship('Vendor', backref='orders')
    items = db.relationship('OrderItem', backref='order', cascade='all, delete-orphan')

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
