"""Tests du parcours de paiement et des protections de securite (python manage.py test studio)."""
import os
import shutil
import tempfile

import numpy as np
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings

from studio import security, views
from studio.models import DigitalCanvas, EmailCode, Order

MEDIA = tempfile.mkdtemp(prefix="paintit-test-")


def make_order(uid="ABC12345-DEF0", status=Order.PENDING, total=49.9):
    return Order.objects.create(
        uid=uid, status=status, format_label="40 x 50 cm", orientation="portrait", width_cm=40, height_cm=50,
        colors=24, price=total, total=total, customer_name="Jane Doe", customer_email="jane@example.com",
        address1="1 rue X", postal_code="75001", city="Paris", country="FR")


def fake_fulfill(o, shipping):
    Order.objects.filter(uid=o["uid"]).update(status=Order.FULFILLED)
    return o, {}, {"status": "ok"}


@override_settings(MEDIA_ROOT=MEDIA, STRIPE_SECRET_KEY="sk_test_x", DEBUG=False,
                   EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
@mock.patch.object(views, "_fulfill", side_effect=fake_fulfill)
class PaymentTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_pay_success_without_stripe_proof_does_not_fulfill(self, ful):
        make_order()
        r = self.client.get("/order/confirmed/?uid=ABC12345-DEF0")
        self.assertEqual(r.status_code, 302)
        self.assertFalse(ful.called)
        self.assertEqual(Order.objects.get().status, Order.PENDING)
        self.assertNotIn("verified_email", self.client.session)

    def test_pay_success_with_forged_sid_does_not_fulfill(self, ful):
        make_order()
        with mock.patch("studio.payments.paid_session", return_value=None):
            self.client.get("/order/confirmed/?uid=ABC12345-DEF0&sid=cs_fake")
        self.assertFalse(ful.called)

    def test_pay_success_with_paid_session_fulfills_once(self, ful):
        make_order()
        with mock.patch("studio.payments.paid_session", return_value={"payment_status": "paid"}):
            r1 = self.client.get("/order/confirmed/?uid=ABC12345-DEF0&sid=cs_ok")
            r2 = self.client.get("/order/confirmed/?uid=ABC12345-DEF0&sid=cs_ok")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(ful.call_count, 1)
        self.assertContains(r1, security.referral_code_for("ABC12345-DEF0"))
        self.assertNotContains(r1, "ABC12345-DEF0-R")

    def test_shipped_order_is_never_refulfilled(self, ful):
        make_order(status=Order.SHIPPED)
        with mock.patch("studio.payments.paid_session", return_value={"payment_status": "paid"}):
            self.client.get("/order/confirmed/?uid=ABC12345-DEF0&sid=cs_ok")
        self.assertFalse(ful.called)
        self.assertEqual(Order.objects.get().status, Order.SHIPPED)

    def _webhook(self, event):
        with mock.patch("studio.payments.verify_webhook", return_value=event):
            return self.client.post("/webhooks/stripe/", data=b"{}", content_type="application/json")

    def test_webhook_unpaid_session_ignored(self, ful):
        make_order()
        self._webhook({"type": "checkout.session.completed", "data": {"object": {
            "payment_status": "unpaid", "metadata": {"uid": "ABC12345-DEF0", "kind": "kit"}, "amount_total": 4990}}})
        self.assertFalse(ful.called)

    def test_webhook_underpaid_is_flagged_not_fulfilled(self, ful):
        make_order(total=49.9)
        self._webhook({"type": "checkout.session.completed", "data": {"object": {
            "payment_status": "paid", "metadata": {"uid": "ABC12345-DEF0", "kind": "kit"}, "amount_total": 100}}})
        self.assertFalse(ful.called)
        self.assertIn("ALERTE", Order.objects.get().notes)

    def test_webhook_paid_fulfills_and_duplicate_is_noop(self, ful):
        make_order()
        ev = {"type": "checkout.session.completed", "data": {"object": {
            "payment_status": "paid", "metadata": {"uid": "ABC12345-DEF0", "kind": "kit"}, "amount_total": 4990}}}
        self._webhook(ev)
        self._webhook(ev)
        self.assertEqual(ful.call_count, 1)

    def test_webhook_digital_purchase_is_credited(self, ful):
        self._webhook({"type": "checkout.session.completed", "data": {"object": {
            "payment_status": "paid", "metadata": {"uid": "XYZ", "kind": "digital", "email": "a@b.co"}}}})
        self.assertTrue(DigitalCanvas.objects.filter(uid="XYZ", email="a@b.co").exists())

    def test_webhook_refund_is_logged(self, ful):
        make_order(status=Order.FULFILLED)
        self._webhook({"type": "charge.refunded", "data": {"object": {
            "metadata": {"uid": "ABC12345-DEF0"}, "amount_refunded": 4990}}})
        self.assertIn("Remboursement", Order.objects.get().notes)


@override_settings(MEDIA_ROOT=MEDIA, STRIPE_SECRET_KEY="", DEBUG=False)
class NoDemoInProdTests(TestCase):
    def test_paint_confirm_refuses_without_stripe(self):
        r = self.client.post("/play/XYZ/confirm/")
        self.assertEqual(r.status_code, 503)
        self.assertFalse(DigitalCanvas.objects.exists())

    def test_paint_pay_no_longer_grants_credit(self):
        self.client.get("/paint/pay/")
        self.assertFalse(self.client.session.get("paid_credits"))


@override_settings(MEDIA_ROOT=MEDIA, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class EmailCodeTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_code_is_single_use_and_bruteforce_limited(self):
        self.client.post("/paint/send-code/", {"email": "a@b.co"})
        code = EmailCode.objects.get(email="a@b.co").code
        for _ in range(5):
            self.client.post("/paint/verify/", {"email": "a@b.co", "code": "000000" if code != "000000" else "111111"})
        r = self.client.post("/paint/verify/", {"email": "a@b.co", "code": code})
        self.assertEqual(r.status_code, 429)              # 6e essai bloque, code grille
        self.assertFalse(EmailCode.objects.filter(email="a@b.co").exists())

    def test_valid_code_logs_in_once(self):
        self.client.post("/paint/send-code/", {"email": "c@d.co"})
        code = EmailCode.objects.get(email="c@d.co").code
        self.assertEqual(self.client.post("/paint/verify/", {"email": "c@d.co", "code": code}).status_code, 200)
        self.assertEqual(self.client.post("/paint/verify/", {"email": "c@d.co", "code": code}).status_code, 400)

    def test_send_code_rate_limited(self):
        codes = [self.client.post("/paint/send-code/", {"email": "e@f.co"}).status_code for _ in range(4)]
        self.assertEqual(codes[-1], 429)


@override_settings(MEDIA_ROOT=MEDIA)
class FileAccessTests(TestCase):
    def setUp(self):
        d = os.path.join(MEDIA, "orders", "ABC12345-DEF0")
        os.makedirs(d, exist_ok=True)
        for n in ("ABC12345-DEF0_order.json", "ABC12345-DEF0_source_x.jpg"):
            open(os.path.join(d, n), "w").write("{}")

    def test_order_file_requires_signature(self):
        self.assertEqual(self.client.get("/files/ABC12345-DEF0/ABC12345-DEF0_order.json").status_code, 404)
        self.assertEqual(self.client.get("/files/ABC12345-DEF0/ABC12345-DEF0_order.json?t=bad").status_code, 404)
        url = security.file_url("ABC12345-DEF0", "ABC12345-DEF0_order.json", absolute=False)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_customer_source_photo_not_public(self):
        self.assertEqual(self.client.get("/preview/img/ABC12345-DEF0/source/").status_code, 404)


@override_settings(MEDIA_ROOT=MEDIA)
class PurgeTests(TestCase):
    def test_purge_keeps_paid_orders_gallery_and_mes_toiles(self):
        from datetime import timedelta
        from django.core.management import call_command
        from django.utils import timezone
        old = timezone.now() - timedelta(days=90)
        make_order("PAID0001-AAAA", Order.DELIVERED)
        make_order("PEND0001-AAAA", Order.PENDING)
        Order.objects.update(created_at=old)
        DigitalCanvas.objects.create(email="x@y.z", uid="MINE0001-AAAA", colors=24)
        for u in ("PAID0001-AAAA", "PEND0001-AAAA", "gal-cat", "MINE0001-AAAA", "ORPH0001-AAAA"):
            p = os.path.join(MEDIA, "orders", u)
            os.makedirs(p, exist_ok=True)
            os.utime(p, (old.timestamp(), old.timestamp()))
        call_command("purge_old_data", stdout=open(os.devnull, "w"))
        self.assertTrue(Order.objects.filter(uid="PAID0001-AAAA").exists())
        self.assertFalse(Order.objects.filter(uid="PEND0001-AAAA").exists())
        left = set(os.listdir(os.path.join(MEDIA, "orders")))
        self.assertTrue({"PAID0001-AAAA", "gal-cat", "MINE0001-AAAA"} <= left)
        self.assertFalse({"PEND0001-AAAA", "ORPH0001-AAAA"} & left)


class LegalInvoiceTests(TestCase):
    def test_legal_pages_render(self):
        for u in ("/mentions-legales/", "/cgv/", "/privacy/", "/en/cgv/"):
            self.assertEqual(self.client.get(u).status_code, 200, u)

    def test_invoice_numbers_are_sequential_and_stable(self):
        a, b = make_order("INV00001-AAAA"), make_order("INV00002-AAAA")
        n1 = a.assign_invoice_number(); n2 = b.assign_invoice_number()
        self.assertTrue(n1.endswith("-00001") and n2.endswith("-00002"), (n1, n2))
        self.assertEqual(a.assign_invoice_number(), n1)            # jamais renumerotee

    def test_invoice_pdf_builds(self):
        from studio.receipts import build_receipt
        o = make_order(); o.assign_invoice_number()
        self.assertTrue(build_receipt(Order.objects.get(pk=o.pk)).startswith(b"%PDF"))

    def test_checkout_requires_cgv(self):
        s = self.client.session
        s["order"] = {"uid": "CGV00001-AAAA", "price": 30, "format_label": "40 x 50 cm", "orientation": "portrait",
                      "width_cm": 40, "height_cm": 50, "colors": 24}
        s["shipping"] = {"full_name": "J", "email": "j@x.fr", "address1": "1 rue", "postal_code": "75001",
                         "city": "Paris", "country": "FR"}
        s.save()
        with mock.patch("studio.address.validate", return_value=None):
            r = self.client.post("/checkout/place/")
        self.assertRedirects(r, "/checkout/", fetch_redirect_response=False)
        self.assertFalse(Order.objects.filter(uid="CGV00001-AAAA").exists())


class AntiSpamTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_contact_honeypot_drops_message(self):
        from studio.models import ContactMessage
        self.client.post("/contact/", {"name": "Bot", "email": "b@b.co", "subject": "x", "message": "spam",
                                        "website": "http://spam"})
        self.assertFalse(ContactMessage.objects.exists())


@override_settings(MEDIA_ROOT=MEDIA)
class BackupTests(TestCase):
    def test_backup_creates_files(self):
        from django.core.management import call_command
        out = tempfile.mkdtemp()
        try:
            call_command("backup_data", "--dir", out, "--no-media", stdout=open(os.devnull, "w"))
        except Exception as exc:   # base de test en memoire : sqlite ':memory:' non sauvegardable
            self.skipTest(str(exc))
        self.assertTrue(any(f.startswith("db-") for f in os.listdir(out)))
        shutil.rmtree(out, ignore_errors=True)


class AdminSecurityTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        cache.clear()
        self.u = User.objects.create_superuser("boss", "b@x.fr", "pw-Secret-123")

    def test_login_bruteforce_blocked(self):
        for _ in range(5):
            self.client.post("/admin/login/", {"username": "boss", "password": "bad"})
        r = self.client.post("/admin/login/", {"username": "boss", "password": "pw-Secret-123"})
        self.assertEqual(r.status_code, 429)

    def test_totp_setup_then_required(self):
        from studio.models import StaffTOTP
        self.client.login(username="boss", password="pw-Secret-123")
        self.assertEqual(self.client.get("/admin-hub/").status_code, 200)          # pas encore active
        self.client.get("/admin-hub/2fa/setup/")
        secret = self.client.session["otp_pending"]
        self.client.post("/admin-hub/2fa/setup/", {"code": security.totp_code(secret)})
        self.assertTrue(StaffTOTP.objects.filter(user=self.u).exists())
        self.client.logout()
        self.client.login(username="boss", password="pw-Secret-123")
        r = self.client.get("/admin-hub/orders/")
        self.assertTrue(r.status_code == 302 and "/admin-hub/2fa/" in r["Location"])
        self.assertEqual(self.client.post("/admin-hub/2fa/", {"code": "000000", "next": "/admin-hub/orders/"}).status_code, 200)
        r = self.client.post("/admin-hub/2fa/", {"code": security.totp_code(secret), "next": "/admin-hub/orders/"})
        self.assertEqual(r["Location"], "/admin-hub/orders/")
        self.assertEqual(self.client.get("/admin-hub/orders/").status_code, 200)

    def test_branded_404(self):
        with override_settings(DEBUG=False):
            r = self.client.get("/nope-%s/" % "x")
        self.assertEqual(r.status_code, 404)
        self.assertContains(r, "404", status_code=404)


class ErpPagesTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        User.objects.create_superuser("boss", "b@x.fr", "pw-Secret-123")
        self.client.login(username="boss", password="pw-Secret-123")

    def test_discounts_crud(self):
        from studio.models import Discount
        self.client.post("/admin-hub/discounts/", {"op": "create", "code": "noel25", "percent": "25", "max_uses": "10"})
        d = Discount.objects.get(code="NOEL25")
        self.assertEqual((d.kind, d.percent, d.max_uses), ("promo", 25, 10))
        self.client.post("/admin-hub/discounts/", {"op": "toggle", "pk": d.pk})
        self.assertFalse(Discount.objects.get(pk=d.pk).active)
        self.assertContains(self.client.get("/admin-hub/discounts/?tab=promo"), "NOEL25")
        self.client.post("/admin-hub/discounts/", {"op": "delete", "pk": d.pk})
        self.assertFalse(Discount.objects.filter(pk=d.pk).exists())

    def test_legal_page_saves(self):
        from studio.models import CompanyInfo
        r = self.client.post("/admin-hub/legal/", {"legal_name": "PaintIt SAS", "email": "contact@paintit.click",
                                                  "vat_rate": "0", "invoice_prefix": "F", "siret": "12345678900011"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(CompanyInfo.get().legal_name, "PaintIt SAS")
        self.assertContains(self.client.get("/mentions-legales/"), "PaintIt SAS")

    def test_lab_renders_in_erp(self):
        r = self.client.get("/pbn/")
        self.assertContains(r, 'id="runBtn"')
        self.assertContains(r, "erp-nav")


@override_settings(MEDIA_ROOT=MEDIA)
class DeletionTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        User.objects.create_superuser("boss", "b@x.fr", "pw-Secret-123")
        self.client.login(username="boss", password="pw-Secret-123")

    def _order(self, uid, email="jane@example.com"):
        o = make_order(uid)
        Order.objects.filter(pk=o.pk).update(customer_email=email)
        os.makedirs(os.path.join(MEDIA, "orders", uid), exist_ok=True)
        return o

    def test_bulk_delete_orders(self):
        a, b, c = self._order("DEL00001-AAAA"), self._order("DEL00002-AAAA"), self._order("DEL00003-AAAA")
        self.client.post("/admin-hub/orders/", {"op": "delete", "ids": [a.pk, b.pk]})
        self.assertEqual(list(Order.objects.values_list("uid", flat=True)), ["DEL00003-AAAA"])
        self.assertFalse(os.path.isdir(os.path.join(MEDIA, "orders", "DEL00001-AAAA")))

    def test_delete_single_order_keeps_mes_toiles_files(self):
        o = self._order("DEL00004-AAAA")
        DigitalCanvas.objects.create(email="jane@example.com", uid="DEL00004-AAAA", colors=24)
        r = self.client.post("/admin-hub/orders/%d/" % o.pk, {"op": "delete"})
        self.assertRedirects(r, "/admin-hub/orders/", fetch_redirect_response=False)
        self.assertFalse(Order.objects.filter(pk=o.pk).exists())
        self.assertTrue(os.path.isdir(os.path.join(MEDIA, "orders", "DEL00004-AAAA")))   # toile du client conservee

    def test_delete_customer_erases_everything(self):
        from studio.models import ContactMessage
        self._order("DEL00005-AAAA", "bob@example.com"); self._order("DEL00006-AAAA", "bob@example.com")
        self._order("DEL00007-AAAA", "other@example.com")
        DigitalCanvas.objects.create(email="bob@example.com", uid="X1", colors=24)
        ContactMessage.objects.create(name="Bob", email="bob@example.com", subject="s", message="m")
        self.client.post("/admin-hub/customers/", {"op": "delete", "email": "bob@example.com"})
        self.assertFalse(Order.objects.filter(customer_email="bob@example.com").exists())
        self.assertFalse(DigitalCanvas.objects.filter(email="bob@example.com").exists())
        self.assertFalse(ContactMessage.objects.filter(email="bob@example.com").exists())
        self.assertTrue(Order.objects.filter(customer_email="other@example.com").exists())


class SupplierPaletteTests(TestCase):
    def test_csv_parsing(self):
        from studio.palette import parse_csv
        cols, rep = parse_csv("numéro origine;code couleur hexadecimal;;;\n1;#FFFFFF;;;\n23;#5C5D3E;;\n23;#5C5D3E;;\n"
                              "123;#127534;;\n123;#439330;;\n9;nope;;\n".encode("cp1252"))
        self.assertEqual([c["code"] for c in cols], ["1", "23", "123", "123"])
        self.assertEqual(rep["duplicates"], ["23"]); self.assertEqual(rep["same_code"], ["123"]); self.assertEqual(rep["invalid"], ["9"])

    def test_snap_uses_only_available_distinct_colors(self):
        import numpy as np, pbn
        labels = np.array([[0, 0, 1], [1, 2, 2]])
        pal = np.array([[250, 250, 250], [10, 10, 200], [12, 12, 198]], dtype=np.uint8)   # BGR
        av = [{"code": "W", "hex": "#FFFFFF"}, {"code": "R", "hex": "#CC0000"}, {"code": "R2", "hex": "#BB1111"}]
        lab2, pal2, codes, de = pbn.snap_palette(labels, pal, av)
        self.assertEqual(sorted(codes), ["R", "R2", "W"])                     # 3 teintes distinctes
        self.assertEqual(pal2.shape, (3, 3))

    def test_supplier_json_uses_codes(self):
        from studio.fulfillment import supplier_colors
        out = supplier_colors([{"number": 1, "hex": "#FFFFFF", "rgb": [255, 255, 255], "code": "1"},
                               {"number": 2, "hex": "#000000", "rgb": [0, 0, 0]}])
        self.assertEqual(out[0], {"number": 1, "supplier_code": "1", "hex": "#FFFFFF", "rgb": [255, 255, 255]})
        self.assertIn("hex", out[1])                                           # code + hex : les deux


class InboxTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        User.objects.create_superuser("boss", "b@x.fr", "pw-Secret-123")
        self.client.login(username="boss", password="pw-Secret-123")

    def test_default_conversation_actions_target_the_message(self):
        from studio.models import ContactMessage
        m = ContactMessage.objects.create(name="A", email="a@x.fr", subject="s", message="m")
        html = self.client.get("/admin-hub/messages/").content.decode()   # 1re conversation affichee par defaut
        self.assertIn('action="/admin-hub/messages/%d/"' % m.pk, html)
        self.client.post("/admin-hub/messages/%d/" % m.pk, {"op": "close"})
        self.assertTrue(ContactMessage.objects.get(pk=m.pk).answered)
        self.client.post("/admin-hub/messages/%d/" % m.pk, {"op": "delete_thread"})
        self.assertFalse(ContactMessage.objects.filter(email="a@x.fr").exists())


class SeoTests(TestCase):
    def _ld(self, url):
        import json, re
        html = self.client.get(url).content.decode()
        out = []
        for b in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
            d = json.loads(b)
            out += [g["@type"] for g in d["@graph"]] if "@graph" in d else [d["@type"]]
        return out

    def test_structured_data(self):
        self.assertIn("Product", self._ld("/"))
        self.assertIn("FAQPage", self._ld("/faq/"))
        self.assertIn("FAQPage", self._ld("/it/faq/"))
        self.assertIn("BreadcrumbList", self._ld("/play/"))

    def test_faq_in_footer_and_sitemap(self):
        self.assertContains(self.client.get("/"), 'href="/faq/"')
        self.assertContains(self.client.get("/sitemap.xml"), "/faq/")

    def test_48_colors_only_when_enabled(self):
        from studio.models import Pricing
        self.assertNotContains(self.client.get("/create/"), 'value="48"')
        p = Pricing.get(); p.av_c48 = True; p.save()
        self.assertContains(self.client.get("/create/"), 'value="48"')


@override_settings(MEDIA_ROOT=MEDIA, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ShopifyBridgeTests(TestCase):
    """Boutique Shopify relais (MangoERP) : creation de commande + retours d'expedition signes."""
    def setUp(self):
        from studio.models import Supplier
        self.sup = Supplier.objects.create(name="Yingsa", checkout="kit", integration="shopify",
                                           shopify={"domain": "pi", "client_id": "cid", "client_secret": "sec"})
        self.calls = []

    def fake(self, url, data=None, headers=None, form=False, timeout=25):
        self.calls.append((url, data))
        if url.endswith("/admin/oauth/access_token"):
            return 200, {"access_token": "tok", "expires_in": 86399}, {}
        if "orderCreate" in (data or {}).get("query", ""):
            return 200, {"data": {"orderCreate": {"order": {"id": "gid://shopify/Order/77", "name": "#1001",
                                                            "legacyResourceId": "77"}, "userErrors": []}}}, {}
        return 200, {"data": {}}, {}

    def test_push_order_once_with_client_credentials(self):
        from studio import shopify
        o = make_order(status=Order.PAID)
        with mock.patch("studio.shopify._http", side_effect=self.fake):
            self.assertEqual(shopify.push_order(self.sup, o, {"X_template.pdf": "https://f/x.pdf"}), "#1001")
            o.refresh_from_db()
            self.assertEqual(o.shopify_order_id, "77")
            self.assertTrue(shopify.push_order(self.sup, o, {}).startswith("deja"))   # pas de doublon
        self.assertTrue(self.calls[0][0].startswith("https://pi.myshopify.com/admin/oauth/access_token"))
        order_in = [d for u, d in self.calls if "orderCreate" in (d or {}).get("query", "")][0]["variables"]["order"]
        self.assertEqual(order_in["financialStatus"], "PAID")
        self.assertEqual(order_in["shippingAddress"]["countryCode"], "FR")

    def test_signed_fulfillment_webhook_marks_shipped(self):
        import base64, hashlib, hmac, json
        o = make_order(status=Order.FULFILLED)
        Order.objects.filter(pk=o.pk).update(shopify_order_id="77", supplier=self.sup)
        body = json.dumps({"order_id": 77, "status": "success", "tracking_company": "YunExpress",
                           "tracking_number": "YT1"}).encode()
        url = "/api/shopify/%s/webhook/" % self.sup.webhook_secret
        bad = self.client.post(url, body, content_type="application/json", HTTP_X_SHOPIFY_HMAC_SHA256="x",
                               HTTP_X_SHOPIFY_TOPIC="fulfillments/create")
        self.assertEqual(bad.status_code, 401)
        sig = base64.b64encode(hmac.new(b"sec", body, hashlib.sha256).digest()).decode()
        ok = self.client.post(url, body, content_type="application/json", HTTP_X_SHOPIFY_HMAC_SHA256=sig,
                              HTTP_X_SHOPIFY_TOPIC="fulfillments/create")
        self.assertEqual(ok.status_code, 200)
        o.refresh_from_db()
        self.assertEqual((o.status, o.tracking_number), (Order.SHIPPED, "YT1"))


class WooConnectorTests(TestCase):
    """Connecteur compatible WooCommerce : l'ERP du fournisseur (MangoERP) lit les commandes et renvoie le suivi."""
    def setUp(self):
        from studio.models import Supplier
        from studio import woo_api
        self.sup = Supplier.objects.create(name="Yingsa", checkout="kit", integration="woo")
        self.keys = woo_api.generate_keys(self.sup)
        self.o = make_order(status=Order.FULFILLED)
        from studio import integrations
        self.assertTrue(integrations.dispatch({"uid": self.o.uid}, {}, sup=self.sup))
        make_order(uid="OTHER000-0000", status=Order.PAID)          # autre fournisseur / non transmise
        self.auth = {"consumer_key": self.keys["ck"], "consumer_secret": self.keys["cs"]}

    def test_auth_required(self):
        self.assertEqual(self.client.get("/wc-api/v3/").status_code, 200)       # index public (comme Woo)
        self.assertEqual(self.client.get("/wc-api/v3/orders").status_code, 401)
        bad = {**self.auth, "consumer_secret": "cs_nope"}
        self.assertEqual(self.client.get("/wc-api/v3/orders", bad).status_code, 401)

    def test_list_legacy_format_scoped(self):
        r = self.client.get("/wc-api/v3/orders", {**self.auth, "status": "processing", "filter[limit]": "50"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["X-WC-Total"], "1")
        o = r.json()["orders"][0]
        self.assertEqual((o["order_number"], o["status"]), (self.o.uid, "processing"))
        self.assertEqual(o["line_items"][0]["sku"], "PBN-40x50-24C")
        self.assertEqual(o["shipping_address"]["country"], "FR")
        self.assertTrue(any(m["key"] == "Fichiers (ZIP)" for m in o["line_items"][0]["meta"]))
        self.assertNotIn("cost", str(o))
        li = o["line_items"][0]
        self.assertIn("_poster.png", li["image"]["src"])
        p = self.client.get("/wc-api/v3/products/%d" % li["product_id"], self.auth).json()["product"]
        self.assertEqual((p["sku"], p["images"][0]["src"]), (li["sku"], li["image"]["src"]))
        # Basic auth + API REST moderne
        import base64
        h = "Basic " + base64.b64encode(("%s:%s" % (self.keys["ck"], self.keys["cs"])).encode()).decode()
        r = self.client.get("/wp-json/wc/v3/orders", {"status": "processing"}, HTTP_AUTHORIZATION=h)
        self.assertEqual((r.status_code, r["X-WP-Total"], r.json()[0]["number"]), (200, "1", self.o.uid))

    def test_oauth1_signature(self):
        import base64, hashlib, hmac, time
        from urllib.parse import quote
        p = {"oauth_consumer_key": self.keys["ck"], "oauth_timestamp": str(int(time.time())),
             "oauth_nonce": "abc", "oauth_signature_method": "HMAC-SHA256", "filter[limit]": "10"}
        enc = lambda s: quote(s, safe="~")
        norm = "&".join("%s=%s" % (enc(k), enc(v)) for k, v in sorted(p.items()))
        base = "&".join(["GET", enc("http://testserver/wc-api/v3/orders"), enc(norm)])
        p["oauth_signature"] = base64.b64encode(hmac.new((self.keys["cs"] + "&").encode(), base.encode(),
                                                          hashlib.sha256).digest()).decode()
        self.assertEqual(self.client.get("/wc-api/v3/orders", p).status_code, 200)

    def test_tracking_update(self):
        import json
        from urllib.parse import urlencode
        url = "/wc-api/v3/orders/%d?%s" % (self.o.pk, urlencode(self.auth))
        r = self.client.put(url, json.dumps({"order": {"status": "completed", "order_meta": {
            "_trackingmore_tracking_number": "YT2412345678", "_trackingmore_tracking_provider": "yunexpress"}}}),
            content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.o.refresh_from_db()
        self.assertEqual((self.o.status, self.o.tracking_number, self.o.carrier),
                         (Order.SHIPPED, "YT2412345678", "yunexpress"))
        self.assertEqual(r.json()["order"]["status"], "completed")
        # note de suivi (format libre)
        o2 = make_order(uid="NOTE0000-0001", status=Order.FULFILLED)
        from studio import integrations
        integrations.dispatch({"uid": o2.uid}, {}, sup=self.sup)
        r = self.client.post("/wc-api/v3/orders/%d/notes?%s" % (o2.pk, urlencode(self.auth)),
                             json.dumps({"order_note": {"note": "Tracking number: LX123456789CN carrier: 4PX"}}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 201)
        o2.refresh_from_db()
        self.assertEqual((o2.status, o2.tracking_number), (Order.SHIPPED, "LX123456789CN"))
        # commande hors perimetre
        other = Order.objects.get(uid="OTHER000-0000")
        self.assertEqual(self.client.get("/wc-api/v3/orders/%d" % other.pk, self.auth).status_code, 404)
        Order.objects.filter(pk=other.pk).update(supplier=self.sup)     # rattachee mais jamais transmise via Woo
        self.assertEqual(self.client.get("/wc-api/v3/orders/%d" % other.pk, self.auth).status_code, 404)
        from studio.models import ConnectorLog
        self.assertTrue(ConnectorLog.objects.filter(supplier=self.sup, method="PUT").exists())
        self.assertNotIn(self.keys["cs"], " ".join(ConnectorLog.objects.values_list("query", flat=True)))


@override_settings(AFTERSHIP_API_KEY="k", EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ParcelTests(TestCase):
    """Arrivee dans le pays -> e-mail « ça arrive » (une fois) avec le n° du transporteur national ; livre -> Livree."""
    def setUp(self):
        from studio.models import Supplier
        self.sup = Supplier.objects.create(name="Yingsa", integration="woo")
        self.o = make_order(status=Order.SHIPPED)
        Order.objects.filter(pk=self.o.pk).update(supplier=self.sup, tracking_number="YT123", carrier="yunexpress",
                                                  country="France")
        self.o.refresh_from_db()
        self.t = {"id": "as1", "tag": "InTransit", "slug": "yunexpress",
                  "checkpoints": [{"country_region": "CHN", "message": "Departed"}], "last_mile": {}}

    def fake(self, method, path, data=None):
        return dict(self.t)

    def test_waits_then_notifies_once_then_delivered(self):
        from django.core import mail
        from studio import parcel
        with mock.patch("studio.parcel._http", side_effect=self.fake):
            parcel.check(self.o, force=True)
            self.assertEqual(len(mail.outbox), 0)                     # encore en Chine
            self.t.update(checkpoints=self.t["checkpoints"] + [{"country_region": "FRA", "slug": "la-poste-colissimo",
                                                                  "message": "Arrivé en France"}],
                          last_mile={"tracking_number": "6A123456789", "slug": "la-poste-colissimo"})
            self.o.refresh_from_db(); parcel.check(self.o, force=True)
            self.assertEqual(len(mail.outbox), 1)
            self.assertIn("6A123456789", mail.outbox[0].body)
            self.assertIn("Colissimo", mail.outbox[0].body)
            self.o.refresh_from_db(); parcel.check(self.o, force=True)
            self.assertEqual(len(mail.outbox), 1)                     # pas de doublon
            self.t["tag"] = "Delivered"
            self.o.refresh_from_db(); parcel.check(self.o, force=True)
        self.o.refresh_from_db()
        self.assertEqual(self.o.status, Order.DELIVERED)


class SupplierGridTests(TestCase):
    """Grille tarifaire au format du devis : cout = prix (couleurs) + livraison zone + supplement couleurs, en EUR."""
    def setUp(self):
        from django.contrib.auth.models import User
        from studio.models import Supplier
        self.sup = Supplier.objects.create(name="Yingsa")
        self.sup.offer = {"grid": Supplier.GRID_TEMPLATE, "taxes": {"vat_recoverable": True}}
        self.sup.save()
        self.u = User.objects.create_superuser("g", "g@x.fr", "pw")

    def test_cost_from_grid(self):
        s = self.sup
        # 40x50, 36 couleurs, UK : (9.5 + 8.5 + 1) USD * 0.92
        self.assertEqual(s.order_cost("40x50", 36, country="GB"), round(19.0 * 0.92, 2))
        self.assertEqual(s.order_cost("40x50", 48, country="NZ"), round((10.5 + 10.5 + 2) * 0.92, 2))
        self.assertIsNone(s.order_cost("40x50", 24, country="FR"))       # livraison France non chiffree
        self.assertIsNone(s.order_cost("60x80", 24, country="GB"))       # taille absente
        self.assertEqual(s.goods_cost("40x50", 24), round(7.5 * 0.92, 2))
        self.assertTrue(s.offer_get("formats", "40x50")["on"])
        self.assertFalse(s.offer_get("colors", 12)["on"])

    def test_save_roundtrip_from_fiche(self):
        from studio import views
        self.client.force_login(self.u)
        with mock.patch("studio.middleware.StaffTwoFactorMiddleware.__call__", lambda m, r: m.get_response(r)):
            r = self.client.get("/admin-hub/suppliers/%d/" % self.sup.pk)
        self.assertContains(r, "Grille tarifaire")
        from django.http import QueryDict
        v = views._grid_view(self.sup)
        P = QueryDict(mutable=True)
        P.update({"g_currency": "USD", "g_fx": "0,9", "g_nz": str(len(v["zones"])), "g_np": str(len(v["products"])),
                  "g_nr": str(v["n_rows"]), "g_kit": "unframed", "tax_vat_recoverable": "on"})
        for c in v["cols"]:
            P.appendlist("g_cols", c)
        for i, z in enumerate(v["zones"]):
            P.update({"z_key_%d" % i: z["key"], "z_label_%d" % i: z["label"], "z_countries_%d" % i: z["countries"]})
        for p in v["products"]:
            P.update({"p_key_%d" % p["j"]: p["key"], "p_label_%d" % p["j"]: p["label"]})
            for c, val in p["extras"]:
                if val is not None:
                    P["p_extra_%d_%s" % (p["j"], c)] = str(val)
            for r in p["rows"]:
                P.update({"r_prod_%d" % r["i"]: r["product"], "r_size_%d" % r["i"]: r["size"], "r_on_%d" % r["i"]: "on"})
                for c, val in r["prices"]:
                    P["r_price_%d_%s" % (r["i"], c)] = str(val)
                for zk, val in r["ships"]:
                    if val is not None:
                        P["r_ship_%d_%s" % (r["i"], zk)] = str(val)
        P.update({"r_size_%d" % v["n_rows"]: "40x40", "r_prod_%d" % v["n_rows"]: "unframed",
                  "r_ship_%d_fr" % 0: "4.5"})                          # livraison France ajoutee sur 30x40
        views._grid_save(self.sup, P)
        self.sup.refresh_from_db()
        self.assertEqual(self.sup.grid()["fx"], 0.9)
        self.assertEqual(len(self.sup.grid()["rows"]), v["n_rows"])     # ligne d'ajout vide / doublon ignores
        self.assertEqual(self.sup.order_cost("30x40", 24, country="FR"), round((5.5 + 4.5) * 0.9, 2))


class LibraryVisualTests(TestCase):
    """Bibliotheque : chaque modele recoit un visuel (MarketingAd) ; « Modifier » ouvre le Marketing Corner
    et la variante regeneree remplace le visuel du modele."""
    def setUp(self):
        from django.contrib.auth.models import User
        from studio.models import PostTemplate
        self.u = User.objects.create_superuser("lv", "lv@x.fr", "pw")
        self.t = PostTemplate.objects.create(name="Dog", hook="I turned my dog into a kit", lang="en",
                                             platforms="reddit,facebook", media_kind="photo")

    def fake_build(self, uid, texts=None, gif_texts=None, links=None, suffix="", style=None, only=None):
        keys = sorted(only or {"gif_portrait", "slider"})
        return [(k, k, "/tmp/promo_%s%s.%s" % (k, suffix, "gif" if k.startswith("gif") else "html"),
                 "gif" if k.startswith("gif") else "web") for k in keys]

    def test_build_and_edit_roundtrip(self):
        from studio import library_visuals
        with mock.patch("studio.marketing.build_all", side_effect=self.fake_build):
            ad = library_visuals.build(self.t, uid="mkt-abc123")
        self.t.refresh_from_db()
        self.assertEqual((self.t.ad_uid, ad.kind, ad.texts["gif_title"]), ("mkt-abc123", "gif_portrait", self.t.hook))
        self.assertEqual(self.t.visual().pk, ad.pk)
        self.client.force_login(self.u)
        with mock.patch("studio.middleware.StaffTwoFactorMiddleware.__call__", lambda m, r: m.get_response(r)), \
                mock.patch("studio.library_visuals.base_canvases", return_value=[]):
            r = self.client.get("/admin-hub/communities/library/")
            self.assertContains(r, "/marketing/?from=mkt-abc123&v=%s&tpl=%d" % (self.t.ad_variant, self.t.pk))
            r = self.client.get("/marketing/?from=mkt-abc123&v=%s&tpl=%d" % (self.t.ad_variant, self.t.pk))
            self.assertContains(r, 'name="tpl" value="%d"' % self.t.pk)
            import os
            from django.conf import settings
            d = os.path.join(settings.MEDIA_ROOT, "orders", "mkt-abc123")
            os.makedirs(d, exist_ok=True)
            open(os.path.join(d, "mkt-abc123_preview.svg"), "w").write("<svg/>")
            with mock.patch("studio.marketing.build_all", side_effect=self.fake_build) as b:
                self.client.post("/marketing/", {"tpl": str(self.t.pk), "reuse_uid": "mkt-abc123", "variant": "B2",
                                                 "s_font": "anton", "s_cta": "#e8862a", "gif_title": "New hook"})
            self.assertEqual(b.call_args.kwargs["style"]["font"], "anton")
        self.t.refresh_from_db()
        self.assertEqual(self.t.ad_variant, "B2")
        # « Mettre à jour le visuel » : meme photo, meme style
        from studio.models import MarketingAd
        MarketingAd.objects.filter(variant="B2").update(texts={"_style": {"font": "anton"}})
        with mock.patch("studio.marketing.build_all", side_effect=self.fake_build) as b, \
                mock.patch("studio.library_visuals._has_files", return_value=True):
            library_visuals.build(self.t)
        self.assertEqual(b.call_args.args[0], "mkt-abc123")
        self.assertEqual(b.call_args.kwargs["style"]["font"], "anton")


@override_settings(MEDIA_ROOT=MEDIA, STRIPE_SECRET_KEY="sk_test_x", DEBUG=False)
class LedgerTests(TestCase):
    """Comptabilite : ventes Stripe (tous produits), remboursements, frais, reglements fournisseurs, exports equilibres."""
    def setUp(self):
        from django.contrib.auth.models import User
        from studio.models import CompanyInfo
        c = CompanyInfo.get(); c.vat_rate = 20.0; c.bank_iban = "FR7630006000011234567890189"; c.bank_bic = "AGRIFRPP"
        c.legal_name = "PaintIt SAS"; c.save()
        self.u = User.objects.create_superuser("acc", "acc@x.fr", "pw")

    def _webhook(self, event):
        with mock.patch("studio.payments.verify_webhook", return_value=event), \
                mock.patch.object(views, "_fulfill", side_effect=fake_fulfill):
            return self.client.post("/webhooks/stripe/", data=b"{}", content_type="application/json")

    def test_sales_refunds_fees_and_exports(self):
        from studio import ledger
        from studio.models import LedgerEntry
        o = make_order(total=48.0)
        sess = {"payment_status": "paid", "metadata": {"uid": o.uid, "kind": "kit"}, "amount_total": 4800,
                "payment_intent": "pi_1", "customer_details": {"email": "jane@example.com"}}
        self._webhook({"type": "checkout.session.completed", "data": {"object": sess}})
        self._webhook({"type": "checkout.session.completed", "data": {"object": sess}})      # rejoue : pas de doublon
        self._webhook({"type": "checkout.session.completed", "data": {"object": {
            "payment_status": "paid", "metadata": {"uid": "XYZ", "kind": "digital"}, "amount_total": 99,
            "payment_intent": "pi_2"}}})
        sale = LedgerEntry.objects.get(kind="sale", source="kit")
        self.assertEqual((sale.amount, sale.vat, LedgerEntry.objects.filter(kind="sale").count()), (48.0, 8.0, 2))
        self._webhook({"type": "charge.refunded", "data": {"object": {"metadata": {"uid": o.uid, "kind": "kit"},
            "payment_intent": "pi_1", "amount_refunded": 1200, "refunds": {"data": [{"id": "re_1", "amount": 1200}]}}}})
        o.refresh_from_db()
        self.assertEqual(o.refunded_amount, 12.0)
        # rapprochement Stripe : frais reels + virement banque
        class L:
            def __init__(self, data): self.data = data
            def auto_paging_iter(self): return iter(self.data)
        bts = L([{"id": "txn_1", "type": "charge", "created": 1760000000, "amount": 4800, "fee": 92,
                  "source": {"id": "ch_1", "payment_intent": "pi_1"}},
                 {"id": "txn_2", "type": "payout", "created": 1760000000, "amount": -4000, "fee": 0, "source": {"id": "po_1"}}])
        with mock.patch("stripe.BalanceTransaction.list", return_value=bts), \
                mock.patch("stripe.PaymentIntent.retrieve", return_value={"metadata": {"uid": o.uid, "kind": "kit"}}):
            r = ledger.sync_stripe()
        self.assertEqual((r["fees"], r["payouts"]), (1, 1))
        self.assertEqual(LedgerEntry.objects.get(kind="sale", stripe_id="pi_1").fee, 0.92)
        # exports : ecritures en partie double equilibrees
        import datetime as dt
        start, a, b = ledger.month_bounds(None)
        a = a - dt.timedelta(days=4000)
        lines = [l.split("\t") for l in ledger.fec_lines(a, b).strip().split("\n")[1:]]
        num = lambda v: float(v.replace(",", "."))
        self.assertAlmostEqual(sum(num(l[11]) for l in lines), sum(num(l[12]) for l in lines), places=2)
        self.client.force_login(self.u)
        with mock.patch("studio.middleware.StaffTwoFactorMiddleware.__call__", lambda m, r: m.get_response(r)):
            self.assertContains(self.client.get("/admin-hub/compta/"), "Comptabilité")
            m = timezone_now_month()
            self.assertEqual(self.client.get("/admin-hub/compta/%s/ventes.csv" % m).status_code, 200)
            self.assertEqual(self.client.get("/admin-hub/compta/%s/ecritures.txt" % m).status_code, 200)

    def test_paid_session_with_real_stripe_object(self):
        """stripe-python >= 12 : les objets n'ont plus .get() -> le retour de paiement ne doit pas planter."""
        import stripe
        from studio import payments
        from studio.models import LedgerEntry
        o = make_order(uid="S1", total=52.7)
        sess = stripe.checkout.Session.construct_from({
            "id": "cs_1", "payment_status": "paid", "metadata": {"uid": "S1", "kind": "kit"}, "client_reference_id": "S1",
            "amount_total": 5270, "payment_intent": "pi_s1", "created": 1760450000,
            "customer_details": {"email": "r@g.fr"}}, "sk_test_x")
        with mock.patch("stripe.checkout.Session.retrieve", return_value=sess):
            self.assertTrue(payments.paid_session("cs_1", uid="S1", kind="kit"))
        self.assertEqual(LedgerEntry.objects.get(stripe_id="pi_s1").amount, 52.7)

    def test_supplier_payment_flow(self):
        from studio.models import Supplier, LedgerEntry
        sup = Supplier.objects.create(name="Atelier FR", iban="FR7612345678901234567890123", bic="BNPAFRPP")
        o1 = make_order(uid="P1", status=Order.SHIPPED); o2 = make_order(uid="P2", status=Order.DELIVERED)
        Order.objects.filter(pk__in=[o1.pk, o2.pk]).update(supplier=sup, cost=12.5)
        self.client.force_login(self.u)
        with mock.patch("studio.middleware.StaffTwoFactorMiddleware.__call__", lambda m, r: m.get_response(r)):
            r = self.client.post("/admin-hub/suppliers/%d/" % sup.pk, {"op": "pay_mark", "oid": [o1.pk, o2.pk]})
            pay = sup.payments.get()
            self.assertRedirects(r, "/admin-hub/payments/%d/" % pay.pk, fetch_redirect_response=False)
            self.assertEqual(pay.amount_eur, 25.0)
            x = self.client.get("/admin-hub/payments/%d/?sepa=1" % pay.pk).content
            import xml.dom.minidom
            doc = xml.dom.minidom.parseString(x)
            self.assertEqual(doc.getElementsByTagName("InstdAmt")[0].firstChild.data, "25.00")
            self.client.post("/admin-hub/payments/%d/" % pay.pk, {"op": "confirm", "paid_at": "2026-10-10", "bank_ref": "VIR1"})
        o1.refresh_from_db()
        self.assertIsNotNone(o1.supplier_paid_at)
        self.assertEqual(LedgerEntry.objects.get(kind="supplier").amount, -25.0)


def timezone_now_month():
    from django.utils import timezone
    return timezone.localdate().strftime("%Y-%m")


class SeoLandingTests(TestCase):
    """Pages thematiques : vocabulaire par langue, hreflang croises, redirection de langue, sitemap, IndexNow."""
    def test_landings(self):
        r = self.client.get("/peinture-par-numeros-adulte/")
        self.assertContains(r, "<h1>Peinture par numéros pour adulte</h1>", html=False)
        self.assertContains(r, 'hreflang="de" href="https://paintit.click/de/malen-nach-zahlen-erwachsene/"')
        self.assertContains(r, '"@type": "FAQPage"')
        self.assertRedirects(self.client.get("/en/peinture-par-numeros-adulte/"), "/en/paint-by-numbers-for-adults/",
                             status_code=301, fetch_redirect_response=False)
        self.assertEqual(self.client.get("/pas-une-page/").status_code, 404)
        self.assertRedirects(self.client.get("/peinture-par-numeros-animal/"), "/peinture-par-numeros-personnalisee/",
                             status_code=301, fetch_redirect_response=False)      # page retiree
        self.assertContains(self.client.get("/sitemap.xml"), "/it/come-dipingere-con-i-numeri/")
        self.assertContains(self.client.get("/"), "/peinture-par-numeros-enfant/")     # maillage (pied de page)
        from studio.seo import indexnow_key
        k = indexnow_key()
        self.assertEqual(self.client.get("/%s.txt" % k).content.decode(), k)


def tearDownModule():
    shutil.rmtree(MEDIA, ignore_errors=True)


@override_settings(MEDIA_ROOT=MEDIA)
class SatisfyingVideoTests(TestCase):
    UID = "lib-video-test"

    def setUp(self):
        import json
        from django.contrib.auth.models import User
        d = os.path.join(MEDIA, "orders", self.UID)
        os.makedirs(d, exist_ok=True)
        # deux zones : un carre troue (evenodd) et le carre du trou
        svg = ('<svg viewBox="0 0 100 100"><path data-n="1" d="M 0 0 L 100 0 L 100 100 L 0 100 Z M 30 30 L 70 30 L 70 70 L 30 70 Z"/>'
               '<path data-n="2" d="M 30 30 L 70 30 L 70 70 L 30 70 Z"/></svg>')
        open(os.path.join(d, self.UID + "_digipaint.svg"), "w").write(svg)
        json.dump([{"number": 1, "rgb": [200, 40, 40]}, {"number": 2, "rgb": [20, 40, 200]}],
                  open(os.path.join(d, self.UID + "_colors.json"), "w"))
        DigitalCanvas.objects.create(uid=self.UID, source="library", title="Rose")
        DigitalCanvas.objects.create(uid="client-photo", source="digital")      # commande client : jamais proposee
        User.objects.create_superuser("boss", "b@x.fr", "pw-Secret-123")
        self.client.login(username="boss", password="pw-Secret-123")

    def test_zones_cover_canvas_and_only_catalogue_sources(self):
        from studio import satisfying
        labels, colors, numbers, areas, edge = satisfying.load_zones(self.UID, 100, 100)
        self.assertEqual((labels < 0).mean(), 0)                               # trou rempli par la zone 2
        self.assertEqual(int(numbers[labels[50, 50]]), 2)
        self.assertEqual(int(numbers[labels[5, 5]]), 1)
        self.assertEqual([u for u, _t in satisfying.sources()], [self.UID])
        start = satisfying.schedule(50, np.arange(50) % 5, "random", 60)
        self.assertTrue(start.min() >= 0 and start.max() <= 60)

    def test_render_short_video(self):
        from studio import satisfying
        out = os.path.join(MEDIA, "t.mp4")
        for style in ("ambient", "electro", "space", "none"):
            satisfying.render(self.UID, out, duration=1.0, hook="{zones} zones, {colors} colors 🎨", music_style=style)
            self.assertGreater(os.path.getsize(out), 1000)
        self.assertEqual(satisfying.hook_text("{zones} zones / {colors}", 869, 32), "869 zones / 32")

    def test_admin_page_whitelists_canvas(self):
        from studio.models import SocialVideo
        self.assertContains(self.client.get("/admin-hub/videos/"), "Vidéos TikTok")
        with mock.patch("studio.satisfying.enqueue") as enq:
            self.client.post("/admin-hub/videos/", {"op": "create", "canvas": "client-photo"})
            self.assertEqual(SocialVideo.objects.count(), 0)
            self.client.post("/admin-hub/videos/", {"op": "create", "canvas": self.UID, "count": "2", "music_style": "space"})
        self.assertEqual(SocialVideo.objects.count(), 2)
        self.assertEqual(enq.call_count, 2)
        self.assertTrue(all(61 <= v.duration <= 70 and v.music_style == "space" for v in SocialVideo.objects.all()))
        v = SocialVideo.objects.first()
        self.assertEqual(self.client.get("/admin-hub/videos/?format=json").json()["videos"][0]["status"], "queued")
        self.assertEqual(self.client.get("/admin-hub/videos/%d.mp4" % v.pk).status_code, 404)
