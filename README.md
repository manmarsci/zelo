# ZELO LIVE BOUTIQUE

A minimal, elegant, mobile-first ecommerce website for a Pakistan-based boutique selling brand leftover, surplus & overstock clothing for ladies and girls.

## Tech Stack

- **Backend:** Flask (Python)
- **Database:** MotherDuck (cloud-hosted DuckDB)
- **Frontend:** Vanilla HTML / CSS / JS (no frameworks — fast & lightweight)
- **Design:** Minimal Pakistani fashion boutique aesthetic — cream background, gold accents, elegant serif typography

## Features

### Customer Storefront
- ✅ Homepage with hero, new arrivals, popular products, sale
- ✅ Product catalog with filters (category, brand, price, availability, size)
- ✅ Sorting (newest, price, popular, best-selling, discount)
- ✅ Product detail with image gallery, size/color selection, size guide
- ✅ Shopping cart (session-based for guests, user-based for logged in)
- ✅ Checkout with guest support, saved addresses, coupon codes
- ✅ Cash on Delivery + Online payment placeholder
- ✅ User registration, login, profile, saved addresses
- ✅ Order history & order detail with status tracking
- ✅ Mobile-first responsive design

### Admin Dashboard
- ✅ Secure admin login
- ✅ Dashboard with stats (products, orders, customers, revenue)
- ✅ Add / edit / delete products with multiple images
- ✅ Set brand, category, prices, stock, sizes, colors
- ✅ Mark products as New Arrival / Popular / Sale / Active
- ✅ Manage orders with status updates (Pending → Confirmed → Shipped → Delivered → Cancelled)
- ✅ View customers
- ✅ Low stock alerts

### Database (MotherDuck)
- Users, addresses, categories, brands, products, product_images
- Orders, order_items, coupons, cart
- Automatic stock decrease on order placement
- Sales & view tracking per product

## Setup

### 1. Get a MotherDuck token
1. Sign up at https://motherduck.com
2. Create a database called `zelo_boutique`
3. Get your service token from the dashboard

### 2. Install dependencies
```bash
pip install -r requirements.txt

qwen is dumb