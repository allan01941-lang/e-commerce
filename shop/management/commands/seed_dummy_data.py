from decimal import Decimal
from io import BytesIO
import random

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from PIL import Image, ImageDraw, ImageFont

from shop.inventory import sync_product_stock
from shop.models import (
    Banner,
    Category,
    Order,
    OrderItem,
    Product,
    ProductImage,
    ProductVariant,
    SiteSettings,
    Size,
    Color,
    Stock,
)


def build_test_image(filename, color=(66, 153, 225)):
    image = Image.new('RGB', (800, 800), color=color)
    buffer = BytesIO()
    image.save(buffer, format='PNG')
    buffer.seek(0)
    return SimpleUploadedFile(name=filename, content=buffer.read(), content_type='image/png')


def build_product_image(filename, slug, variant=0):
    backgrounds = [(239, 246, 255), (240, 253, 250), (255, 247, 237), (250, 245, 255)]
    accents = [(37, 99, 235), (13, 148, 136), (234, 88, 12), (124, 58, 237)]
    background = backgrounds[variant % len(backgrounds)]
    accent = accents[variant % len(accents)]
    image = Image.new('RGB', (800, 800), background)
    draw = ImageDraw.Draw(image)
    draw.ellipse((155, 560, 645, 650), fill=(215, 222, 232))
    white = (255, 255, 255)
    dark = (35, 45, 65)

    if 'earbuds' in slug:
        draw.rounded_rectangle((235, 365, 565, 550), radius=70, fill=dark)
        draw.rounded_rectangle((280, 225, 360, 420), radius=38, fill=white)
        draw.rounded_rectangle((440, 225, 520, 420), radius=38, fill=white)
        draw.ellipse((302, 250, 338, 286), fill=accent)
        draw.ellipse((462, 250, 498, 286), fill=accent)
        draw.rounded_rectangle((370, 405, 430, 435), radius=15, fill=accent)
    elif 'watch' in slug:
        draw.rounded_rectangle((335, 130, 465, 670), radius=55, fill=dark)
        draw.rounded_rectangle((250, 275, 550, 525), radius=85, fill=accent)
        draw.rounded_rectangle((275, 300, 525, 500), radius=65, fill=dark)
        draw.line((400, 340, 400, 400, 450, 430), fill=white, width=14)
    elif 'speaker' in slug:
        draw.rounded_rectangle((280, 155, 520, 595), radius=105, fill=dark)
        draw.rounded_rectangle((305, 180, 495, 570), radius=90, outline=white, width=8)
        for y, radius in ((300, 55), (455, 35)):
            draw.ellipse((400 - radius, y - radius, 400 + radius, y + radius), fill=accent, outline=white, width=8)
    elif 'lamp' in slug:
        draw.polygon([(250, 205), (550, 205), (490, 345), (310, 345)], fill=accent)
        draw.rectangle((385, 345, 415, 545), fill=dark)
        draw.ellipse((310, 520, 490, 580), fill=dark)
    elif 'coffee' in slug:
        for x in (255, 425):
            draw.rounded_rectangle((x, 290, x + 135, 495), radius=24, fill=white, outline=accent, width=10)
            draw.arc((x + 105, 330, x + 205, 445), start=270, end=90, fill=accent, width=15)
            draw.arc((x + 35, 245, x + 95, 295), start=180, end=360, fill=(170, 130, 95), width=9)
    elif 'shirt' in slug:
        draw.polygon([(300, 190), (365, 145), (435, 145), (500, 190), (600, 300), (515, 385), (475, 340), (475, 575), (325, 575), (325, 340), (285, 385), (200, 300)], fill=accent)
        draw.polygon([(365, 145), (400, 205), (435, 145)], fill=white)
    elif 'serum' in slug:
        draw.rounded_rectangle((320, 265, 480, 575), radius=35, fill=(209, 151, 84), outline=white, width=12)
        draw.rectangle((365, 190, 435, 265), fill=dark)
        draw.rounded_rectangle((380, 130, 420, 205), radius=18, fill=white)
        draw.rounded_rectangle((340, 390, 460, 485), radius=15, fill=white)
        draw.ellipse((370, 415, 430, 465), fill=accent)
    else:
        draw.rounded_rectangle((245, 265, 390, 560), radius=28, fill=white, outline=accent, width=10)
        draw.rounded_rectangle((410, 315, 555, 560), radius=28, fill=accent)
        draw.rounded_rectangle((285, 205, 350, 275), radius=18, fill=dark)
        draw.ellipse((450, 365, 515, 430), fill=white)

    font = ImageFont.load_default(size=30)
    title = slug.replace('-', ' ').title()
    draw.text((400, 710), title, fill=(55, 65, 81), font=font, anchor='mm')

    buffer = BytesIO()
    image.save(buffer, format='PNG')
    buffer.seek(0)
    return SimpleUploadedFile(name=filename, content=buffer.read(), content_type='image/png')


PRODUCT_LONG_DESCRIPTIONS = {
    'wireless-earbuds-pro': '<h2>Clear sound for every day</h2><p>Enjoy calls, playlists, and podcasts with comfortable wireless earbuds designed for everyday listening. The compact charging case slips easily into a pocket or bag.</p><ul><li>Comfortable in-ear fit for commuting and daily use</li><li>Compact rechargeable case</li><li>Simple wireless pairing with compatible devices</li></ul><p>Wipe clean with a soft, dry cloth and store in the case when not in use.</p>',
    'smart-fitness-watch': '<h2>Your everyday activity companion</h2><p>Keep useful activity and notification information close at hand with a lightweight watch and an easy-to-read display. Its adjustable strap is designed for comfortable all-day wear.</p><ul><li>Readable digital display</li><li>Activity and wellness tracking features</li><li>Adjustable, comfortable strap</li></ul><p>Feature availability may vary by connected phone and companion app.</p>',
    'portable-bluetooth-speaker': '<h2>Bring your music along</h2><p>This compact wireless speaker is made for music at home, at work, or while travelling. Its portable shape fits easily on a desk or shelf.</p><ul><li>Wireless audio connection</li><li>Compact portable design</li><li>Easy-to-use playback controls</li></ul><p>For best performance, keep the speaker dry and charge it using a compatible power source.</p>',
    'minimal-desk-lamp': '<h2>A simple light for focused spaces</h2><p>Add practical task lighting to a desk, bedside table, or reading corner. The minimal profile pairs easily with modern and classic interiors.</p><ul><li>Clean, space-saving silhouette</li><li>Suitable for desks and bedside tables</li><li>Easy to coordinate with home decor</li></ul><p>Use with a compatible bulb and keep away from moisture.</p>',
    'ceramic-coffee-set': '<h2>Make coffee moments special</h2><p>Serve coffee or tea in a coordinated ceramic set that looks at home on a breakfast table or when hosting guests. The smooth glazed finish is easy to care for.</p><ul><li>Coordinated cups with comfortable handles</li><li>Timeless cream color</li><li>Suitable for everyday serving</li></ul><p>Handle carefully and follow the care instructions supplied with the set.</p>',
    'cotton-casual-shirt': '<h2>Easy comfort, everyday style</h2><p>This casual cotton shirt is a versatile choice for everyday outfits. The classic navy color pairs easily with jeans, chinos, and relaxed weekend wear.</p><ul><li>Soft-feel cotton fabric</li><li>Easy-to-style everyday design</li><li>Available in size L</li></ul><p>For best results, follow the garment care label when washing.</p>',
    'hydrating-face-serum': '<h2>A lightweight step in your skincare routine</h2><p>Add a hydrating step to your daily routine with this easy-to-apply face serum. The compact 50ml bottle is convenient for keeping on a vanity or packing for travel.</p><ul><li>Lightweight serum texture</li><li>Convenient 50ml bottle</li><li>Easy to layer into a skincare routine</li></ul><p>Apply as directed on the product packaging. Avoid contact with eyes and discontinue use if irritation occurs.</p>',
    'daily-skin-care-combo': '<h2>A convenient daily care set</h2><p>Bring everyday skincare essentials together in one convenient combo. The coordinated set is easy to keep on your bathroom shelf or give as a thoughtful gift.</p><ul><li>Multiple essentials in one set</li><li>Simple to add to a daily care routine</li><li>Thoughtful option for gifting</li></ul><p>Review each item label before use, especially if you have sensitive skin.</p>',
    'usb-c-power-bank': '<h2>Power for days on the go</h2><p>Keep a compact backup battery close for travel, commuting, or busy days away from an outlet. Its simple design fits easily into a backpack or work bag.</p><ul><li>Portable rechargeable battery</li><li>USB-C charging connection</li><li>Useful for everyday travel</li></ul><p>Charging speed and device compatibility depend on the connected device and cable.</p>',
    'mechanical-keyboard': '<h2>A comfortable typing setup</h2><p>Give your desk a practical upgrade with a compact mechanical-style keyboard for work, study, and everyday computer use.</p><ul><li>Full-size key layout</li><li>Wired USB connection</li><li>Suitable for home or office desks</li></ul><p>Check your computer ports and operating system before purchase.</p>',
    'wireless-mouse': '<h2>Simple wireless control</h2><p>This lightweight wireless mouse is an easy companion for a laptop or desktop, with a clean shape designed for everyday use.</p><ul><li>Wireless receiver included</li><li>Comfortable ambidextrous shape</li><li>Compact for travel</li></ul><p>Requires a compatible USB port and battery.</p>',
    'wireless-headphones': '<h2>Listen in comfort</h2><p>Enjoy music, calls, and podcasts with cushioned over-ear headphones made for everyday listening at home or while travelling.</p><ul><li>Adjustable padded headband</li><li>Wireless audio connection</li><li>Foldable ear cups for storage</li></ul><p>Battery life varies with volume and usage.</p>',
    'insulated-water-bottle': '<h2>Bring a drink along</h2><p>A reusable insulated bottle for keeping water close at work, school, or on a day trip. The leak-resistant lid helps make it easy to carry.</p><ul><li>Stainless steel body</li><li>Reusable everyday design</li><li>Easy-grip finish</li></ul><p>Hand wash for best care; not intended for carbonated drinks.</p>',
    'cotton-cushion-cover': '<h2>A quick refresh for your room</h2><p>Add a soft accent to a sofa, chair, or bed with this simple cotton cushion cover in a versatile neutral shade.</p><ul><li>Soft cotton fabric</li><li>Hidden zipper closure</li><li>Designed for standard 45 cm cushions</li></ul><p>Cushion insert is not included.</p>',
    'ceramic-planter': '<h2>A home for small plants</h2><p>Display a favorite small plant on a shelf or windowsill with a clean-lined ceramic planter that suits many interiors.</p><ul><li>Glazed ceramic finish</li><li>Suitable for small indoor plants</li><li>Neutral color for easy styling</li></ul><p>Use a liner or protective tray to protect delicate surfaces.</p>',
    'everyday-canvas-tote': '<h2>Carry everyday essentials</h2><p>A reusable canvas tote for errands, books, and daily essentials. Its simple shape folds down easily when not in use.</p><ul><li>Durable cotton canvas</li><li>Comfortable shoulder straps</li><li>Reusable alternative for shopping</li></ul><p>Spot clean as needed and air dry.</p>',
    'daily-sunscreen-spf50': '<h2>Everyday sun-care essential</h2><p>A lightweight sunscreen for adding sun protection to a daily routine. The compact tube is easy to keep in a bag.</p><ul><li>SPF 50 broad-spectrum protection</li><li>Convenient 50 ml size</li><li>Designed for daily use</li></ul><p>Apply as directed on the label and reapply as recommended. Avoid contact with eyes.</p>',
    'travel-grooming-kit': '<h2>Practical grooming on the go</h2><p>Keep everyday grooming essentials together in a compact travel kit that fits neatly in luggage or a gym bag.</p><ul><li>Compact zip case</li><li>Organized storage compartments</li><li>Useful for travel and daily routines</li></ul><p>Personal care items shown in sample imagery are not included.</p>',
}


class Command(BaseCommand):
    help = 'Creates sample data for all shop models.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--no-clear',
            action='store_true',
            help='Keep existing model data and create new entries without clearing tables.',
        )
        parser.add_argument(
            '--refresh-demo-products',
            action='store_true',
            help='Update only the built-in demo products, square gallery images, and long descriptions.',
        )

    def handle(self, *args, **options):
        refresh_demo_products = options['refresh_demo_products']
        if not options['no_clear']:
            if refresh_demo_products:
                raise CommandError('--refresh-demo-products cannot be combined with the default clearing mode; use --no-clear.')
            OrderItem.objects.all().delete()
            Order.objects.all().delete()
            ProductImage.objects.all().delete()
            Product.objects.all().delete()
            Category.objects.all().delete()
            Banner.objects.all().delete()
            SiteSettings.objects.all().delete()

        site_settings = None
        if not refresh_demo_products:
            site_settings = SiteSettings.objects.create(
                brand_name='BDBazaar',
                brand_area='Dhaka, Bangladesh',
                inside_dhaka_charge=Decimal('60.00'),
                outside_dhaka_charge=Decimal('120.00'),
                nav_bg_color='#ffffff',
                nav_text_color='#111827',
                accent_color='#2563eb',
                search_placeholder='Search gadgets and essentials...',
                meta_description='Shop quality electronics, home essentials, fashion, and beauty products online in Bangladesh.',
                og_title='BDBazaar | Bangladesh Ecommerce Store',
                og_description='Shop trusted home essentials, electronics, and lifestyle products.',
                canonical_url='',
                ga_measurement_id='G-XXXXXXXXXX',
                facebook_pixel_id='FB-XXXXXXXXXX',
                about_text='Your one-stop destination for everyday essentials, gadgets, and home upgrades across Bangladesh.',
                phone='+880 1700-000000',
                email='support@bdbazaar.com',
                address='Dhaka, Bangladesh',
                copyright_text='© 2026 BDBazaar. All rights reserved.',
                facebook_url='https://facebook.com/bdbazaar',
                instagram_url='https://instagram.com/bdbazaar',
                twitter_url='https://twitter.com/bdbazaar',
                youtube_url='https://youtube.com/@bdbazaar',
            )

        category_data = [
            ('Electronics', 'electronics', 'Smart gadgets and tech accessories'),
            ('Home & Living', 'home-living', 'Everyday household essentials and decor'),
            ('Fashion', 'fashion', 'Comfortable and modern essentials for everyday wear'),
            ('Beauty & Care', 'beauty-care', 'Skin, body, and personal care essentials'),
        ]

        categories = []
        for name, slug, description in category_data:
            category, _ = Category.objects.get_or_create(
                slug=slug,
                defaults={'name': name, 'description': description, 'image': build_test_image(f'{slug}.png', color=(45, 55, 72))},
            )
            if not category.image:
                category.image = build_test_image(f'{slug}.png', color=(45, 55, 72))
                category.save(update_fields=['image'])
            categories.append(category)

        products = []
        product_specs = [
            ('Wireless Earbuds Pro', 'wireless-earbuds-pro', categories[0], Decimal('2490.00'), Decimal('3290.00'), 'Black', 'M', 35, True, True),
            ('Smart Fitness Watch', 'smart-fitness-watch', categories[0], Decimal('4590.00'), Decimal('5990.00'), 'Silver', 'L', 18, True, True),
            ('Portable Bluetooth Speaker', 'portable-bluetooth-speaker', categories[0], Decimal('1890.00'), Decimal('2490.00'), 'Blue', 'M', 22, False, False),
            ('Minimal Desk Lamp', 'minimal-desk-lamp', categories[1], Decimal('1290.00'), Decimal('1790.00'), 'White', 'Standard', 40, True, False),
            ('Ceramic Coffee Set', 'ceramic-coffee-set', categories[1], Decimal('1550.00'), Decimal('1990.00'), 'Cream', 'Set', 26, True, True),
            ('Cotton Casual Shirt', 'cotton-casual-shirt', categories[2], Decimal('990.00'), Decimal('1490.00'), 'Navy', 'L', 54, False, True),
            ('Hydrating Face Serum', 'hydrating-face-serum', categories[3], Decimal('620.00'), Decimal('890.00'), 'Amber', '50ml', 60, True, False),
            ('Daily Skin Care Combo', 'daily-skin-care-combo', categories[3], Decimal('1750.00'), Decimal('2290.00'), 'Rose', 'Combo', 17, True, True),
            ('USB-C Power Bank', 'usb-c-power-bank', categories[0], Decimal('2190.00'), Decimal('2790.00'), 'Black', '10000mAh', 32, True, False),
            ('Mechanical Keyboard', 'mechanical-keyboard', categories[0], Decimal('3890.00'), Decimal('4690.00'), 'White', 'Standard', 16, False, True),
            ('Wireless Mouse', 'wireless-mouse', categories[0], Decimal('890.00'), Decimal('1190.00'), 'Blue', 'Standard', 45, False, False),
            ('Wireless Headphones', 'wireless-headphones', categories[0], Decimal('3290.00'), Decimal('3990.00'), 'Black', 'Standard', 21, True, True),
            ('Insulated Water Bottle', 'insulated-water-bottle', categories[1], Decimal('790.00'), Decimal('1090.00'), 'Green', '750ml', 38, False, False),
            ('Cotton Cushion Cover', 'cotton-cushion-cover', categories[1], Decimal('450.00'), Decimal('650.00'), 'Beige', '45cm', 29, False, False),
            ('Ceramic Planter', 'ceramic-planter', categories[1], Decimal('690.00'), Decimal('950.00'), 'Terracotta', 'Small', 24, False, True),
            ('Everyday Canvas Tote', 'everyday-canvas-tote', categories[2], Decimal('590.00'), Decimal('790.00'), 'Natural', 'Standard', 41, False, False),
            ('Daily Sunscreen SPF 50', 'daily-sunscreen-spf50', categories[3], Decimal('550.00'), Decimal('750.00'), 'Yellow', '50ml', 34, True, True),
            ('Travel Grooming Kit', 'travel-grooming-kit', categories[3], Decimal('1290.00'), Decimal('1690.00'), 'Navy', 'Standard', 19, True, False),
        ]

        for name, slug, category, price, compare_at, color, size, stock, free_shipping, featured in product_specs:
            product, _ = Product.objects.update_or_create(
                slug=slug,
                defaults={
                    'category': category,
                    'name': name,
                    'sku': f'BDB-{slug.upper()[:8]}',
                    'short_description': f'{name} for everyday comfort, quality, and reliable performance.',
                    'long_description': PRODUCT_LONG_DESCRIPTIONS[slug],
                    'free_shipping': free_shipping,
                    'is_active': True,
                    'is_featured': featured,
                },
            )
            Size.objects.get_or_create(name=size)
            Color.objects.get_or_create(name=color)
            ProductVariant.objects.update_or_create(
                product=product,
                size=size,
                color=color,
                defaults={
                    'price': price,
                    'compare_at_price': compare_at,
                    'quantity': stock,
                    'opening_quantity': stock,
                    'restocked_quantity': 0,
                },
            )
            Stock.objects.update_or_create(
                product=product,
                defaults={
                    'quantity': stock,
                    'opening_quantity': stock,
                    'restocked_quantity': 0,
                },
            )
            sync_product_stock(product.pk)
            products.append(product)

            if refresh_demo_products:
                image_records = list(ProductImage.objects.filter(product=product).order_by('ordering', 'created_at'))
                if not image_records:
                    for idx in range(2):
                        ProductImage.objects.create(
                            product=product,
                            image=build_product_image(f'{slug}-square-{idx + 1}.png', slug, idx),
                            caption=f'{product.name} gallery image {idx + 1}',
                            is_primary=(idx == 0),
                            ordering=idx,
                        )
                else:
                    for idx, product_image in enumerate(image_records):
                        filename = f'{slug}-square-{product_image.pk}.png'
                        if product_image.image.name.rsplit('/', 1)[-1] != filename:
                            product_image.image = build_product_image(filename, slug, idx)
                            product_image.save(update_fields=['image'])
            else:
                ProductImage.objects.filter(product=product).delete()
                for idx in range(2):
                    ProductImage.objects.create(
                        product=product,
                        image=build_product_image(f'{slug}-{idx + 1}.png', slug, idx),
                        caption=f'{product.name} gallery image {idx + 1}',
                        is_primary=(idx == 0),
                        ordering=idx,
                    )

        if refresh_demo_products:
            self.stdout.write(self.style.SUCCESS(
                f'Refreshed demo product data: {len(products)} products, '
                f'{ProductImage.objects.filter(product__in=products).count()} square gallery images, '
                'and detailed descriptions. Existing orders and store settings were left unchanged.'
            ))
            return

        banners = [
            ('New Tech Essentials', 'Upgrade your everyday setup with smart and reliable gadgets.', 'Shop now', '/shop/', True, 0),
            ('Fresh Home Finds', 'Comfort-driven essentials for a better daily routine.', 'Explore deals', '/shop/', True, 1),
        ]

        for title, subtitle, button_text, button_url, is_active, ordering in banners:
            Banner.objects.get_or_create(
                title=title,
                defaults={
                    'subtitle': subtitle,
                    'button_text': button_text,
                    'button_url': button_url,
                    'image': build_test_image(f'{title.lower().replace(" ", "-")}.png', color=(34, 197, 94)),
                    'is_active': is_active,
                    'ordering': ordering,
                },
            )

        customer_names = [
            ('Rafi Rahman', 'rafi@example.com', '01710000001', 'Dhaka', 'Dhanmondi', 'Mirpur', 'inside_dhaka'),
            ('Nusrat Jahan', 'nusrat@example.com', '01710000002', 'Chattogram', 'Pahartali', 'Chattogram', 'outside_dhaka'),
            ('Sajid Hossain', 'sajid@example.com', '01710000003', 'Khulna', 'Daulatpur', 'Khulna', 'outside_dhaka'),
            ('Ayesha Akter', 'ayesha@example.com', '01710000004', 'Sylhet', 'Ambarkhana', 'Sylhet', 'outside_dhaka'),
        ]

        for index, (full_name, email, phone, city, thana, zilla, delivery_area) in enumerate(customer_names, start=1):
            order = Order.objects.create(
                full_name=full_name,
                phone=phone,
                email=email,
                address=f'{index} {thana} Road',
                thana=thana,
                zilla=zilla,
                city=city,
                delivery_area=delivery_area,
                payment_method='cod',
                note='Gift wrap requested.' if index == 1 else '',
                subtotal=Decimal('0.00'),
                shipping_charge=Decimal('0.00'),
                total=Decimal('0.00'),
                status=['pending', 'confirmed', 'processing', 'shipped'][index % 4],
            )

            order_items = random.sample(products, 2)
            subtotal = Decimal('0.00')
            for item_index, product in enumerate(order_items, start=1):
                variant = product.starting_variant
                if variant is None:
                    raise CommandError(
                        f'Product "{product.name}" must have a priced variant before sample orders can be generated.'
                    )
                quantity = item_index if item_index == 1 else random.randint(1, 2)
                unit_price = variant.price
                line_total = unit_price * quantity
                subtotal += line_total
                OrderItem.objects.create(
                    order=order,
                    product=product,
                    variant=variant,
                    product_name=product.name,
                    quantity=quantity,
                    unit_price=unit_price,
                    line_total=line_total,
                    selected_size=variant.size,
                    selected_color=variant.color,
                )

            shipping_charge = site_settings.inside_dhaka_charge if order.delivery_area == 'inside_dhaka' else site_settings.outside_dhaka_charge
            if any(product.free_shipping for product in order_items):
                shipping_charge = Decimal('0.00')

            order.subtotal = subtotal
            order.shipping_charge = shipping_charge
            order.total = subtotal + shipping_charge
            order.save(update_fields=['subtotal', 'shipping_charge', 'total'])

        self.stdout.write(
            self.style.SUCCESS(
                f'Seeded dummy data successfully: '
                f'{Category.objects.count()} categories, '
                f'{Product.objects.count()} products, '
                f'{ProductImage.objects.count()} product images, '
                f'{Order.objects.count()} orders, '
                f'{OrderItem.objects.count()} order items, '
                f'{Banner.objects.count()} banners, '
                f'{SiteSettings.objects.count()} site settings.'
            )
        )
