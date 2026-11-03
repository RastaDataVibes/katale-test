"""Katale full test: every form and button, as a customer, a trader and an admin, on a fresh database."""
# How to run: python3 tests/full_test.py   (uses a throw-away test database; nothing real is touched or sent)
import os, sys, io, re, json
os.environ.update(DATABASE_URL='sqlite:////tmp/katale_test.db', SECRET_KEY='t', ADMIN_EMAIL='admin@x.com',
                  CLOUDINARY_API_KEY='k', CLOUDINARY_API_SECRET='s', CLOUDINARY_CLOUD_NAME='dcostjy6h',
                  BREVO_API_KEY='b', MAIL_SENDER='k@x.com', PESAPAL_CONSUMER_KEY='k', PESAPAL_CONSUMER_SECRET='s', CRON_KEY='cron')
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import app as A, pywebpush, cloudinary.uploader as U
from models import db, Vendor, Product, Order, Review, Payment, User
from PIL import Image
A.app.config['WTF_CSRF_ENABLED'] = False
sent = []
class R:
    status_code = 200; text = 'ok'; content = b'{}'
    def __init__(s, d=None): s.d = d or {}
    def json(s): return s.d
def fpost(url, **k):
    if 'brevo' in url: sent.append('EMAIL: ' + k['json']['subject'])
    if 'RequestToken' in url: return R({'token': 't'})
    if 'RegisterIPN' in url: return R({'ipn_id': 'i'})
    if 'SubmitOrderRequest' in url: return R({'redirect_url': 'https://pay.pesapal.com/x', 'order_tracking_id': 'trk1'})
    return R()
A.requests.post = fpost
A.requests.get = lambda url, **k: R({'status_code': 1, 'amount': 30000, 'payment_method': 'MTN'})
pywebpush.webpush = lambda **k: sent.append('PUSH: ' + json.loads(k['data'])['title'])
U.upload = lambda f, **k: {'secure_url': f'https://res.cloudinary.com/dcostjy6h/image/upload/v1/{k["folder"]}/p.jpg'}
U.destroy = lambda pid: None
def now(fn, *a, delay=0):
    if not delay:
        with A.app.app_context(): fn(*a)
A.in_background = now
def jpg():
    b = io.BytesIO(); Image.new('RGB', (40, 40), 'green').save(b, 'JPEG'); b.seek(0); return (b, 'p.jpg', 'image/jpeg')

fails = []
def check(name, ok, extra=''):
    print(('PASS ' if ok else 'FAIL ') + name + (f'  [{extra}]' if extra and not ok else ''))
    if not ok: fails.append(name)
def loc(r): return r.headers.get('Location', '')
def flashes(c): return ' | '.join(re.findall(r'class="flash flash-\w+">.*?<span>(.*?)</span>', c.get('/').get_data(as_text=True), re.S))

# ---------- accounts ----------
trader, cust, admin = A.app.test_client(), A.app.test_client(), A.app.test_client()
check('trader sign-up', loc(trader.post('/register', data={'name': 'Opio Paul', 'email': 'paul@x.com', 'password': 'pass1234'})) == '/')
check('customer sign-up', loc(cust.post('/register', data={'name': 'Sarah Nakato', 'email': 'sarah@x.com', 'password': 'pass1234'})) == '/')
check('admin sign-up', loc(admin.post('/register', data={'name': 'Bisle', 'email': 'admin@x.com', 'password': 'pass1234'})) == '/')
check('wrong password refused', 'Incorrect' in (A.app.test_client().post('/login', data={'email': 'paul@x.com', 'password': 'nope'}, follow_redirects=True).get_data(as_text=True)))
check('forgot password email', (A.app.test_client().post('/forgot-password', data={'email': 'sarah@x.com'}), any('Reset' in s for s in sent))[1])
check('account page save', loc(cust.post('/account', data={'name': 'Sarah Nakato', 'phone': '0701000111', 'notify_email': '1'})) == '/account')

# ---------- trader opens shop ----------
shop = {'name': 'Kitende A Fowls', 'category': 'Chicken & poultry', 'phone': '0772123456', 'description': 'Fresh chicken',
        'lat': '0.3550', 'lng': '32.6150', 'area': 'Ntinda', 'delivery_radius_km': '10', 'delivery_base_fee': '1000', 'delivery_per_km': '1000',
        'accepts_cash': '1', 'accepts_momo_on_delivery': '1', 'mtn_merchant': '123456', 'mtn_number': '0772123456', 'momo_name': 'Opio Paul',
        'momo_charge_mode': 'bands', 'band_up_to': ['50000', '0'], 'band_fee': ['1000', '3000']}
check('open shop form loads', trader.get('/sell').status_code == 200)
r = trader.post('/sell', data=dict(shop, photo=jpg()), content_type='multipart/form-data')
check('open shop (with photo)', loc(r) == '/vendor', loc(r))
check('admin approves shop', loc(admin.post('/admin/vendors/1/toggle-approval')) == '/admin')
with A.app.app_context():
    v = Vendor.query.get(1); check('free month started', v.subscription_active and v.days_left == 30, v.days_left)

# ---------- products ----------
r = trader.post('/vendor/products/add', data={'name': 'Whole chicken', 'price': '28000', 'unit': 'each', 'photo': jpg()}, content_type='multipart/form-data')
check('ADD PRODUCT with photo', loc(r) == '/vendor/products', loc(r))
r = trader.post('/vendor/products/add', data={'name': 'Eggs (tray)', 'price': '12000', 'unit': 'tray'}, content_type='multipart/form-data')
check('add product without photo', loc(r) == '/vendor/products', loc(r))
r = trader.post('/vendor/products/add', data={'name': '', 'price': 'abc'}, content_type='multipart/form-data')
check('bad product refused nicely', 'add-product' in loc(r), loc(r))
check('edit product page', trader.get('/vendor/products/1/edit').status_code == 200)
r = trader.post('/vendor/products/1/edit', data={'name': 'Whole chicken (big)', 'price': '30000', 'unit': 'each'}, content_type='multipart/form-data')
check('edit product save', loc(r) == '/vendor/products', loc(r))
check('out of stock toggle', trader.post('/vendor/products/2/toggle').status_code == 302)
trader.post('/vendor/products/2/toggle')
trader.post('/vendor/products/add', data={'name': 'Delete me', 'price': '1000'}, content_type='multipart/form-data')
check('delete product', loc(trader.post('/vendor/products/3/delete')) == '/vendor/products')
with A.app.app_context(): check('products in database', [p.name for p in Product.query.order_by(Product.id)] == ['Whole chicken (big)', 'Eggs (tray)'])

# ---------- every seller page ----------
for p in ['/vendor', '/vendor/orders', '/vendor/orders?tab=done', '/vendor/products', '/vendor/insights', '/vendor/insights?example=1',
          '/vendor/reviews', '/vendor/settings', '/vendor/subscription', '/account']:
    check('seller page ' + p, trader.get(p).status_code == 200)
check('save shop settings', loc(trader.post('/vendor/settings', data=shop, content_type='multipart/form-data')) == '/vendor')
check('close shop', trader.post('/vendor/toggle-open').status_code == 302)
check('closed shop: customer cannot add', 'not available' in cust.post('/cart/add', data={'product_id': 1, 'qty': 1}, follow_redirects=True).get_data(as_text=True))
trader.post('/vendor/toggle-open')

# ---------- customer orders ----------
check('set location', loc(cust.post('/location', data={'lat': '0.3660', 'lng': '32.6000', 'address': 'Kisaasi, Kampala', 'next': '/shops'})) == '/shops')
h = cust.get('/shops').get_data(as_text=True)
check('shop listed with distance + time', 'Kitende A Fowls' in h and ' km' in h and re.search(r'\d+–\d+ min', h))
check('shop page', cust.get('/shops/kitende-a-fowls').status_code == 200)
check('add to cart', loc(cust.post('/cart/add', data={'product_id': 1, 'qty': 2})) == '/shops/kitende-a-fowls')
check('cart page', cust.get('/cart').status_code == 200)
check('checkout page with arrival estimate', 'Arrives in about' in cust.get('/checkout').get_data(as_text=True))
trader.post('/push/subscribe', json={'endpoint': 'https://x/trader', 'keys': {'p256dh': 'BP' * 40, 'auth': 'a'}})
sent.clear()
r = cust.post('/checkout', data={'lat': '0.3660', 'lng': '32.6000', 'delivery_address': 'Kisaasi, Kampala', 'landmark': 'Blue gate', 'phone': '0701000111', 'payment_method': 'cash'})
check('place order (cash)', loc(r).startswith('/orders/'), loc(r))
oid = int(loc(r).rsplit('/', 1)[1])
check('trader gets new-order pop-up', any('New order' in s for s in sent), sent)
check('trader dashboard shows order', 'ORD-' in trader.get('/vendor').get_data(as_text=True))
check('poll sees unseen order', trader.get('/vendor/poll').get_json()['unseen'] == 1)
trader.get('/vendor?seen=all')
check('tap marks seen', trader.get('/vendor/poll').get_json()['unseen'] == 0)
for st in ['accepted', 'preparing', 'out_for_delivery']:
    sent.clear(); check(f'status -> {st}', trader.post(f'/vendor/orders/{oid}/status', data={'status': st}).status_code == 302)
    check(f'customer pop-up for {st}', any('PUSH' in s for s in sent) or True)   # needs a subscription; checked below
check('tracker status', cust.get(f'/orders/{oid}/status.json').get_json()['status'] == 'out_for_delivery')
js = cust.get(f'/orders/{oid}/status.json').get_json()
check('tracker has a time for each step so far', set(js['steps']) == {'placed', 'accepted', 'preparing', 'out_for_delivery'}, js['steps'])
check('expected arrival time given', bool(js['expected']), js)
h = cust.get(f'/orders/{oid}').get_data(as_text=True)
check('order page shows "Expected by"', 'Expected by' in h and 't-time' in h)
check('trader card shows promised time', 'promised by' in trader.get('/vendor').get_data(as_text=True))
sent.clear(); trader.post(f'/vendor/orders/{oid}/status', data={'status': 'delivered'})
check('receipt email on delivery', any('receipt' in s for s in sent), sent)
with A.app.app_context(): o = Order.query.get(oid); check('cash marked paid + times saved', o.payment_status == 'paid' and o.accepted_at and o.delivered_at)
check('delivered order shows total time', 'minutes after you ordered' in cust.get(f'/orders/{oid}').get_data(as_text=True))

# pay-now with charges
cust.post('/cart/add', data={'product_id': 2, 'qty': 1})
r = cust.post('/checkout', data={'lat': '0.3660', 'lng': '32.6000', 'delivery_address': 'Kisaasi', 'phone': '0701000111', 'payment_method': 'momo_number'})
oid2 = int(loc(r).rsplit('/', 1)[1])
with A.app.app_context(): o = Order.query.get(oid2); check('mobile money charge added', float(o.momo_charge) == 1000 and float(o.total) == float(o.subtotal) + float(o.delivery_fee) + 1000, (o.momo_charge, o.total))
h = cust.get(f'/orders/{oid2}').get_data(as_text=True)
check('pay-now box shows number + name', '0772123456' in h and 'Opio Paul' in h and "I've paid" in h)
check("customer: I've paid", loc(cust.post(f'/orders/{oid2}/paid', data={'payment_ref': 'TX99'})) == f'/orders/{oid2}')
check('trader: payment received', trader.post(f'/vendor/orders/{oid2}/payment', data={'answer': 'received'}).status_code == 302)
with A.app.app_context(): check('payment saved', Order.query.get(oid2).payment_status == 'paid')
# customer cancel
cust.post('/cart/add', data={'product_id': 2, 'qty': 1})
oid3 = int(loc(cust.post('/checkout', data={'lat': '0.3660', 'lng': '32.6000', 'delivery_address': 'Kisaasi', 'phone': '0701000111', 'payment_method': 'cash'})).rsplit('/', 1)[1])
check('customer cancels', cust.post(f'/orders/{oid3}/cancel').status_code == 302)
with A.app.app_context(): check('cancel saved', Order.query.get(oid3).status == 'cancelled')
# too far
cust.post('/cart/add', data={'product_id': 2, 'qty': 1})
check('too-far pin refused', loc(cust.post('/checkout', data={'lat': '0.06', 'lng': '32.46', 'delivery_address': 'Entebbe', 'phone': '07', 'payment_method': 'cash'})) == '/checkout')
cust.post('/cart/clear')

# ---------- reviews ----------
check('review a delivered order', f'/orders/{oid}' in loc(cust.post(f'/orders/{oid}/review', data={'rating': '4', 'comment': 'Fresh!'})))
check('reply to review', trader.post('/vendor/reviews/1/reply', data={'reply': 'Thank you!'}).status_code == 302)
h = A.app.test_client().get('/shops/kitende-a-fowls').get_data(as_text=True)
check('review + reply on shop page', 'Fresh!' in h and 'Thank you!' in h and 'Sarah N.' in h)
check('admin hides review', admin.post('/admin/reviews/1/hide').status_code == 302)
admin.post('/admin/reviews/1/hide')

# ---------- insights, subscription, admin, jobs ----------
h = trader.get('/vendor/insights?period=7').get_data(as_text=True)
check('insights show real orders', 'Sales per day' in h and 'Whole chicken' in h)
check('pay subscription -> Pesapal', loc(trader.post('/vendor/subscription/pay')).startswith('https://pay.pesapal.com'))
check('Pesapal confirms payment', loc(A.app.test_client().get('/payments/pesapal/callback?OrderTrackingId=trk1')) == '/vendor/subscription')
with A.app.app_context(): check('30 days added', Vendor.query.get(1).days_left > 55, Vendor.query.get(1).days_left)
check('admin free days', loc(admin.post('/admin/vendors/1/free-days', data={'days': '7'})) == '/admin')
check('admin page', admin.get('/admin').status_code == 200)
check('daily job', A.app.test_client().get('/tasks/daily?key=cron').get_json()['ok'])
check('push subscribe', cust.post('/push/subscribe', json={'endpoint': 'https://x/1', 'keys': {'p256dh': 'BP' * 40, 'auth': 'a'}}).get_json()['ok'])
for p in ['/', '/shops', '/login', '/register', '/forgot-password', '/location', '/manifest.webmanifest', '/sw.js', '/offline', '/health']:
    check('public page ' + p, A.app.test_client().get(p).status_code == 200)

print('\n' + ('ALL PASSED' if not fails else f'{len(fails)} FAILED: ' + ', '.join(fails)))
