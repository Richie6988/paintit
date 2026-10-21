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

    def test_production_files_removed_60_days_after_shipping_unless_mes_toiles(self):
        from datetime import timedelta
        from django.core.management import call_command
        from django.utils import timezone
        make_order("SHIP0001-AAAA", Order.DELIVERED)
        make_order("SHIP0002-AAAA", Order.DELIVERED)
        make_order("SHIP0003-AAAA", Order.SHIPPED)
        Order.objects.filter(uid__in=("SHIP0001-AAAA", "SHIP0002-AAAA")).update(status_changed_at=timezone.now() - timedelta(days=61))
        DigitalCanvas.objects.create(email="x@y.z", uid="SHIP0002-AAAA", colors=24)      # encore dans Mes toiles
        for u in ("SHIP0001-AAAA", "SHIP0002-AAAA", "SHIP0003-AAAA"):
            os.makedirs(os.path.join(MEDIA, "orders", u), exist_ok=True)
        call_command("purge_old_data", stdout=open(os.devnull, "w"))
        left = set(os.listdir(os.path.join(MEDIA, "orders")))
        self.assertNotIn("SHIP0001-AAAA", left)
        self.assertTrue({"SHIP0002-AAAA", "SHIP0003-AAAA"} <= left)
        self.assertTrue(Order.objects.filter(uid="SHIP0001-AAAA").exists())               # piece comptable conservee
        self.assertTrue(Order.objects.get(uid="SHIP0001-AAAA").events.filter(user="purge").exists())


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

    def test_supplier_text_is_readable(self):
        from studio.fulfillment import supplier_text
        txt = supplier_text({
            "consignee_information": {"full_name": "Marie Curie", "phone_code": "+33", "phone": "612345678",
                                      "email": "m@x.fr", "address1": "1 rue A", "address2": "Bât B",
                                      "postal_code": "75005", "city": "Paris", "country": "FR"},
            "order_information": {"uid": "AB12-CD34", "created_at": "2026-10-15T10:20:00", "orientation": "portrait",
                                  "width_cm": 40, "height_cm": 50, "colors_count": 3, "brushes": True,
                                  "files": {"template_tiff": "AB12-CD34_template.tiff"}},
            "color_specifications": [{"number": 1, "supplier_code": "A-01"},
                                     {"number": 2, "supplier_code": "B-07", "supplier_code_approx": True},
                                     {"number": 3, "hex": "#123456"}]})
        for part in ("PAINTIT ORDER / 订单  AB12-CD34", "Marie Curie", "+33 612345678", "40 x 50 cm", "YES / 是",
                     "A-01", "B-07 *", "#123456", "closest code"):
            self.assertIn(part, txt)
        self.assertNotIn("price", txt.lower())

    @override_settings(MEDIA_ROOT=MEDIA)
    def test_supplier_colors_sorted_by_factory_code(self):
        from studio.fulfillment import add_areas, supplier_text, supplier_colors_image
        d = os.path.join(MEDIA, "orders", "AREA-1"); os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "AREA-1_digipaint.svg"), "w").write(
            '<svg><path data-n="1" data-a="600" d=""/><path data-n="2" data-a="100" d=""/>'
            '<path data-n="3" data-a="250" d=""/><path data-n="2" data-a="50" d=""/></svg>')
        specs = add_areas("AREA-1", [{"number": 1, "supplier_code": "1205", "hex": "#454545"},
                                     {"number": 2, "supplier_code": "27", "hex": "#2b4a2a"},
                                     {"number": 3, "supplier_code": "115", "hex": "#2d3f2a"}], 40, 50)
        self.assertEqual([c["number"] for c in specs], [2, 3, 1])               # 0027 < 0115 < 1205
        self.assertEqual((specs[0]["area_pct"], specs[0]["area_cm2"]), (15.0, 300.0))
        sj = {"order_information": {"uid": "AREA-1", "width_cm": 40, "height_cm": 50}, "consignee_information": {},
              "color_specifications": list(reversed(specs))}
        txt = supplier_text(sj)
        self.assertIn("Sorted by factory color code (small -> large)", txt)
        self.assertLess(txt.index("0027"), txt.index("0115"))
        self.assertLess(txt.index("0115"), txt.index("1205"))
        jpg = supplier_colors_image(sj, os.path.join(d, "t.jpg"))
        from PIL import Image
        self.assertEqual(Image.open(jpg).format, "JPEG")


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


@override_settings(MEDIA_ROOT=MEDIA)
class SupplierFilesTests(TestCase):
    """L'ERP fournisseur doit voir tous les fichiers : TIFF + tableau couleurs en images produit, lien court en remarque."""
    def setUp(self):
        import os
        from studio.models import Supplier
        from studio import woo_api, integrations
        self.sup = Supplier.objects.create(name="Yingsa", checkout="kit", integration="woo")
        self.auth = dict(zip(("consumer_key", "consumer_secret"), (lambda k: (k["ck"], k["cs"]))(woo_api.generate_keys(self.sup))))
        self.o = make_order(uid="FILES000-0001", status=Order.FULFILLED)
        self.assertTrue(integrations.dispatch({"uid": self.o.uid}, {}, sup=self.sup))
        d = os.path.join(MEDIA, "orders", self.o.uid)
        os.makedirs(d, exist_ok=True)
        for n in ("poster.png", "template.tiff", "template.png", "supplier.json", "supplier_colors.jpg"):
            open(os.path.join(d, "%s_%s" % (self.o.uid, n)), "wb").write(b"x")
        open(os.path.join(d, "%s_supplier.txt" % self.o.uid), "w").write("Order FILES000-0001 / 订单")

    def test_erp_sees_all_files(self):
        from studio import security, shopify
        li = self.client.get("/wc-api/v3/orders", self.auth).json()["orders"][0]
        self.assertIn(security.production_url(self.o.uid), li["note"])
        self.assertNotIn("49.90", str(li))                      # jamais notre prix de vente
        p = self.client.get("/wc-api/v3/products/%d" % li["line_items"][0]["product_id"], self.auth).json()["product"]
        srcs = " ".join(i["src"] for i in p["images"])
        for n in ("_poster.png", "_template.tiff", "_supplier_colors.jpg"):
            self.assertIn(n, srcs)
        props = " ".join(v for _, v in shopify.line_properties(self.o))
        self.assertIn("_supplier.txt", props)
        self.assertIn("_supplier_colors.jpg", props)
        r = self.client.get(security.production_url(self.o.uid, absolute=False))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "_template.tiff")
        self.assertContains(r, "订单")
        self.assertEqual(r["X-Robots-Tag"], "noindex, nofollow")
        self.assertEqual(self.client.get("/p/%s/badsig000000/" % self.o.uid).status_code, 404)
        import io, zipfile
        from urllib.parse import urlparse
        z = self.client.get(urlparse(security.file_url(self.o.uid, "production.zip")).path + "?" +
                            urlparse(security.file_url(self.o.uid, "production.zip")).query)
        names = zipfile.ZipFile(io.BytesIO(b"".join(z.streaming_content) if z.streaming else z.content)).namelist()
        self.assertTrue(any(n.endswith("_supplier.txt") for n in names))


@override_settings(MEDIA_ROOT=MEDIA)
class WooTestShopTests(TestCase):
    def test_create_and_delete(self):
        import io, os
        from django.core.management import call_command
        from studio.models import Supplier
        src = make_order(uid="SRC00000-0001", status=Order.FULFILLED)
        d = os.path.join(MEDIA, "orders", src.uid)
        os.makedirs(d, exist_ok=True)
        for n in ("poster.png", "template.tiff", "source.jpg", "order.json"):
            open(os.path.join(d, "%s_%s" % (src.uid, n)), "wb").write(b"x")
        out = io.StringIO()
        call_command("woo_test_shop", stdout=out)
        shop = Supplier.objects.get(name__startswith="Boutique test")
        self.assertFalse(shop.active)
        t = Order.objects.get(uid__startswith="TEST")
        self.assertTrue(t.is_test)
        files = os.listdir(os.path.join(MEDIA, "orders", t.uid))
        self.assertIn("%s_template.tiff" % t.uid, files)
        self.assertFalse(any("_source" in f or f.endswith("_order.json") for f in files))
        r = self.client.get("/wc-api/v3/orders", {"consumer_key": shop.woo["ck"], "consumer_secret": shop.woo["cs"]})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["orders"][0]["order_number"], t.uid)
        self.assertIn("/p/", r.json()["orders"][0]["customer_note"])
        call_command("woo_test_shop", "--delete", stdout=out)
        self.assertFalse(Supplier.objects.filter(pk=shop.pk).exists() or Order.objects.filter(uid=t.uid).exists())

    def test_admin_ui(self):
        import os
        from django.contrib.auth.models import User
        from studio.models import Supplier
        src = make_order(uid="SRC00000-0002", status=Order.FULFILLED)
        d = os.path.join(MEDIA, "orders", src.uid)
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "%s_poster.png" % src.uid), "wb").write(b"x")
        User.objects.create_superuser("boss", "b@x.fr", "pw-Secret-123")
        self.client.login(username="boss", password="pw-Secret-123")
        r = self.client.post("/admin-hub/suppliers/", {"op": "woo_test"})
        shop = Supplier.objects.get(name__startswith="Boutique test")
        page = self.client.get("/admin-hub/suppliers/%d/" % shop.pk)
        self.assertContains(page, "où arrive chaque information")
        self.assertContains(page, "customer_note / note")
        self.client.post("/admin-hub/suppliers/%d/" % shop.pk, {"op": "woo_test_notes", "map_note": "订单详情 → 备注"})
        shop.refresh_from_db()
        self.assertEqual(shop.woo["map"]["note"], "订单详情 → 备注")
        self.client.post("/admin-hub/suppliers/%d/" % shop.pk, {"op": "woo_test_delete"})
        self.assertFalse(Supplier.objects.filter(pk=shop.pk).exists())


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

    def tearDown(self):
        root = os.path.join(MEDIA, "orders")
        for d in os.listdir(root):
            if d.startswith("vid-"):
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)

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
        for style, mode in (("ambient", "flow"), ("electro", "random"), ("space", "colors"), ("none", "flow")):
            satisfying.render(self.UID, out, duration=1.0, hook="{zones} zones, {colors} colors 🎨", music_style=style,
                              mode=mode)
            self.assertGreater(os.path.getsize(out), 1000)
        self.assertEqual(satisfying.hook_text("{zones} zones / {colors}", 869, 32), "869 zones / 32")

    def test_admin_page_whitelists_canvas(self):
        from studio.models import SocialVideo
        r = self.client.get("/admin-hub/videos/")
        self.assertContains(r, "Vidéos TikTok"); self.assertContains(r, "It takes patience"); self.assertContains(r, "vtitles")
        with mock.patch("studio.satisfying.enqueue") as enq:
            self.client.post("/admin-hub/videos/", {"op": "create", "canvas": "client-photo"})
            self.assertEqual(SocialVideo.objects.count(), 0)
            self.client.post("/admin-hub/videos/", {"op": "create", "canvas": self.UID, "count": "2", "music_style": "space", "before_after": "1"})
        self.assertEqual(SocialVideo.objects.count(), 2)
        self.assertEqual(enq.call_count, 2)
        self.assertTrue(all(15 <= v.duration <= 40 and v.fmt == "fill" and v.music_style == "space" and v.before_after
                            for v in SocialVideo.objects.all()))
        v = SocialVideo.objects.first()
        self.assertEqual(self.client.get("/admin-hub/videos/?format=json").json()["videos"][0]["status"], "queued")
        self.assertEqual(self.client.get("/admin-hub/videos/%d.mp4" % v.pk).status_code, 404)

    def test_sources_newest_import_first(self):
        import shutil as _sh, time as _t
        from studio import satisfying
        for uid in ("vid-old0000001", "vid-new0000001"):
            d = os.path.join(MEDIA, "orders", uid)
            os.makedirs(d, exist_ok=True)
            _sh.copy(os.path.join(MEDIA, "orders", self.UID, self.UID + "_digipaint.svg"), os.path.join(d, uid + "_digipaint.svg"))
            _t.sleep(0.05)
        os.utime(os.path.join(MEDIA, "orders", "vid-new0000001"))
        uids = [u for u, _t2 in satisfying.sources()]
        self.assertEqual(uids[:2], ["vid-new0000001", "vid-old0000001"])
        r = self.client.get("/admin-hub/videos/?canvas=vid-old0000001")
        self.assertEqual(r.context["picked"], "vid-old0000001")

    def test_render_short_formats(self):
        from studio import shorts
        out = os.path.join(MEDIA, "s.mp4")
        for fmt in ("guess", "versus", "tips"):
            shorts.render(fmt, self.UID, out, duration=2.0, answer="It's a rose!", series="#3", uid2=self.UID)
            self.assertGreater(os.path.getsize(out), 1000)
        self.assertEqual(shorts._next_series("#3"), "#4")

    def test_versus_generates_two_canvases(self):
        import shutil as _sh
        from studio.models import SocialVideo
        open(os.path.join(MEDIA, "orders", self.UID, self.UID + "_source_x.jpg"), "wb").write(b"x")
        seen = []

        def fake_generate(path, colors, w, h, uid=None, source_name=None, density=None, detail=1.0):
            seen.append(colors)
            src, dst = os.path.join(MEDIA, "orders", self.UID), os.path.join(MEDIA, "orders", uid)
            os.makedirs(dst)
            for ext in ("_digipaint.svg", "_colors.json"):
                _sh.copy(os.path.join(src, self.UID + ext), os.path.join(dst, uid + ext))
        with mock.patch("studio.pipeline.generate", side_effect=fake_generate), mock.patch("studio.satisfying.enqueue"):
            self.client.post("/admin-hub/videos/", {"op": "create", "canvas": self.UID, "fmt": "versus"})
        v = SocialVideo.objects.get()
        self.assertEqual((seen, v.fmt, v.duration), ([12, 48], "versus", 16.0))
        self.assertTrue(v.canvas_uid.startswith("vid-") and v.canvas_uid2.startswith("vid-"))

    def test_density_regenerates_from_source_photo(self):
        import shutil as _sh
        from studio.models import SocialVideo
        open(os.path.join(MEDIA, "orders", self.UID, self.UID + "_source_x.jpg"), "wb").write(b"x")
        seen = {}

        def fake_generate(path, colors, w, h, uid=None, source_name=None, density=None, detail=1.0):
            seen.update(path=path, colors=colors, density=density, detail=detail)
            src, dst = os.path.join(MEDIA, "orders", self.UID), os.path.join(MEDIA, "orders", uid)
            os.makedirs(dst)
            for ext in ("_digipaint.svg", "_colors.json"):
                _sh.copy(os.path.join(src, self.UID + ext), os.path.join(dst, uid + ext))
        with mock.patch("studio.pipeline.generate", side_effect=fake_generate), mock.patch("studio.satisfying.enqueue"):
            self.client.post("/admin-hub/videos/", {"op": "create", "canvas": self.UID, "density": "expert"})
        v = SocialVideo.objects.get()
        self.assertTrue(v.canvas_uid.startswith("vid-") and v.title == "Rose · Expert (zones fines)")
        self.assertEqual((seen["colors"], seen["density"], seen["detail"]), (2, 1.0, 0.6))     # 2 = couleurs de la toile
        self.assertTrue(seen["path"].endswith("_source_x.jpg"))
        from studio import satisfying
        self.assertEqual(dict(satisfying.sources())[v.canvas_uid], "Rose · Expert (zones fines)")

    def test_upload_image_creates_canvas(self):
        import shutil as _sh
        from django.core.files.uploadedfile import SimpleUploadedFile
        from studio.models import SocialVideo

        def fake_generate(path, colors, w, h, uid=None, source_name=None, density=None, detail=1.0):
            src, dst = os.path.join(MEDIA, "orders", self.UID), os.path.join(MEDIA, "orders", uid)
            os.makedirs(dst)
            for ext in ("_digipaint.svg", "_colors.json"):
                _sh.copy(os.path.join(src, self.UID + ext), os.path.join(dst, uid + ext))
        with mock.patch("studio.pipeline.generate", side_effect=fake_generate), mock.patch("studio.satisfying.enqueue"):
            self.client.post("/admin-hub/videos/", {"op": "create", "image": SimpleUploadedFile("me.jpg", b"x"),
                                                    "colors": "18", "music_style": "electro"})
        v = SocialVideo.objects.get()
        self.assertTrue(v.canvas_uid.startswith("vid-"))
        from studio import satisfying
        self.assertEqual(satisfying.sources()[0][0], v.canvas_uid)


class SiteBannerTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_standard_then_priority_and_schedule(self):
        from datetime import timedelta
        from django.utils import timezone
        from studio.models import SiteBanner
        r = self.client.get("/")
        self.assertContains(r, "N°1 de la peinture par numéros sur-mesure")
        now = timezone.now()
        SiteBanner.objects.create(name="p", kind="promo", text_fr="-15 % avec", text_en="15% off with", code="PAINT15",
                                  link_url="/create/", countdown_to=now + timedelta(days=2))
        SiteBanner.objects.create(name="futur", kind="maintenance", text_fr="Maintenance", starts_at=now + timedelta(days=1))
        SiteBanner.objects.create(name="fini", kind="info", text_fr="Vieux", ends_at=now - timedelta(hours=1))
        r = self.client.get("/")
        self.assertContains(r, "tb-promo"); self.assertContains(r, "PAINT15"); self.assertContains(r, "tb-cd")
        self.assertNotContains(r, "Maintenance"); self.assertNotContains(r, "Vieux")
        r = self.client.get("/en/")
        self.assertContains(r, "15% off with"); self.assertContains(r, 'href="/en/create/"')
        SiteBanner.objects.create(name="m", kind="maintenance", text_fr="Maintenance en cours")
        self.assertContains(self.client.get("/"), "Maintenance en cours")          # maintenance prioritaire

    def test_admin_page_saves(self):
        from django.contrib.auth.models import User
        from studio.models import SiteBanner
        User.objects.create_superuser("boss", "b@x.fr", "pw-Secret-123")
        self.client.login(username="boss", password="pw-Secret-123")
        self.assertContains(self.client.get("/admin-hub/banner/"), "Bandeau du site")
        self.client.post("/admin-hub/banner/", {"op": "save", "name": "Flash", "kind": "promo", "text_fr": "Flash -20 %",
                                                 "code": "FLASH20", "ends_at": "2099-01-01T10:00", "active": "on"})
        b = SiteBanner.objects.get()
        self.assertTrue(b.is_live())
        self.assertContains(self.client.get("/"), "Flash -20 %")


class ProductionStatusTests(TestCase):
    def test_supplier_events_move_forward_only(self):
        from studio import integrations
        from studio.models import Order, Supplier
        sup = Supplier.objects.create(name="S", integration="api", api_url="https://x.test")
        o = Order.objects.create(uid="PROD-1", status=Order.FULFILLED, format_label="40x50", orientation="portrait",
                                 width_cm=40, height_cm=50, colors=24, price=40, total=40, customer_name="A",
                                 customer_email="a@x.fr", supplier=sup)
        self.assertEqual(integrations.handle_event(sup, {"uid": "PROD-1", "status": "in_production"})[0], 200)
        o.refresh_from_db(); self.assertEqual(o.status, Order.PRODUCTION)
        integrations.handle_event(sup, {"uid": "PROD-1", "status": "shipped", "tracking_number": "1Z"})
        integrations.handle_event(sup, {"uid": "PROD-1", "status": "accepted"})      # evenement en retard : ignore
        o.refresh_from_db(); self.assertEqual(o.status, Order.SHIPPED)
        self.assertIn(Order.PRODUCTION, Order.PAID_STATUSES)


class TestDataResetTests(TestCase):
    def _order(self, uid, live):
        from studio.models import Order
        with override_settings(STRIPE_SECRET_KEY="sk_live_x" if live else "sk_test_x"):
            return Order.objects.create(uid=uid, status=Order.PAID, format_label="40x50", orientation="portrait",
                                        width_cm=40, height_cm=50, colors=24, price=40, total=40, customer_name="A",
                                        customer_email="a@x.fr")

    def test_test_orders_excluded_then_reset_and_purge(self):
        from django.contrib.auth.models import User
        from studio import erp
        from studio.models import Order
        t, r = self._order("T-1", live=False), self._order("R-1", live=True)
        self.assertTrue(t.is_test); self.assertFalse(r.is_test)
        self.assertEqual(list(erp._paid().values_list("uid", flat=True)) if hasattr(erp, "_paid") else
                         list(Order.real.values_list("uid", flat=True)), ["R-1"])
        User.objects.create_superuser("boss", "b@x.fr", "pw-Secret-123")
        self.client.login(username="boss", password="pw-Secret-123")
        self.assertContains(self.client.get("/admin-hub/"), "1 commande de test exclue")
        self.client.post("/admin-hub/test-data/", {"op": "mark_all", "next": "/admin-hub/"})
        self.assertEqual(Order.real.count(), 0)
        self.client.post("/admin-hub/test-data/", {"op": "toggle", "id": r.pk, "next": "/admin-hub/"})
        self.assertEqual(list(Order.real.values_list("uid", flat=True)), ["R-1"])
        self.client.post("/admin-hub/test-data/", {"op": "purge", "confirm": "non"})
        self.assertEqual(Order.objects.count(), 2)                                   # confirmation exigee
        self.client.post("/admin-hub/test-data/", {"op": "purge", "confirm": "SUPPRIMER"})
        self.assertEqual(list(Order.objects.values_list("uid", flat=True)), ["R-1"])


@override_settings(MEDIA_ROOT=MEDIA, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class CatalogueVisibilityAndPurchaseTests(TestCase):
    def setUp(self):
        self.m = DigitalCanvas.objects.create(email="__library__", uid="gal-test-rose", title="Rose", price=4.9,
                                              source="library", colors=24)

    def test_hidden_model_leaves_gallery_but_owners_keep_it(self):
        from django.contrib.auth.models import User
        self.assertContains(self.client.get("/play/"), "gal-test-rose")
        User.objects.create_superuser("boss", "b@x.fr", "pw-Secret-123")
        self.client.login(username="boss", password="pw-Secret-123")
        self.client.post("/admin-hub/catalogue/", {"op": "hide", "id": self.m.pk})
        self.m.refresh_from_db(); self.assertTrue(self.m.hidden)
        self.client.logout()
        self.assertNotContains(self.client.get("/play/"), "gal-test-rose")
        self.assertRedirects(self.client.get("/play/buy/gal-test-rose/"), "/play/", fetch_redirect_response=False)
        self.assertTrue(DigitalCanvas.objects.filter(uid="gal-test-rose").exists())          # design conserve

    def test_paid_gallery_purchase_sends_one_confirmation(self):
        from django.core import mail
        from studio import views
        sess = {"amount_total": 490, "metadata": {"uid": "gal-test-rose", "kind": "gallery", "lang": "fr"}}
        self.assertTrue(views._grant_gallery_to(self.m, "buyer@x.fr"))
        views._digital_receipt("buyer@x.fr", self.m.uid, self.m.title, sess, "fr")
        self.assertFalse(views._grant_gallery_to(self.m, "buyer@x.fr"))                   # 2e passage : deja attribuee
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Rose", mail.outbox[0].subject)
        self.assertIn("4,90 €", mail.outbox[0].body)


class ActivityTests(TestCase):
    UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile"

    def _post(self, data, ua=None, **extra):
        import json
        return self.client.post("/a/", json.dumps(data), content_type="application/json",
                                HTTP_USER_AGENT=ua or self.UA, HTTP_ACCEPT_LANGUAGE="fr-BE,fr;q=0.9", **extra)

    def test_collect_pageviews_duration_cta_and_dashboard(self):
        from django.contrib.auth.models import User
        from studio.models import PageHit, SiteEvent, Visit
        h = self._post({"t": "pv", "p": "/", "r": "https://www.tiktok.com/@paintit", "w": 390}).json()["h"]
        self._post({"t": "end", "h": h, "p": "/", "d": 42})
        self._post({"t": "pv", "p": "/create/", "r": "https://paintit.click/"})
        self._post({"t": "c", "p": "/", "n": "Accueil · Créer ma peinture (haut)"})
        self._post({"t": "pv", "p": "/"}, ua="Googlebot/2.1")                       # robot ignore
        v = Visit.objects.get()
        self.assertEqual((v.pages, v.duration_s, v.entry_path, v.referrer, v.country, v.device),
                         (2, 42, "/", "tiktok.com", "BE", "mobile"))
        self.assertEqual(PageHit.objects.count(), 2)
        self.assertEqual(SiteEvent.objects.filter(kind="cta").count(), 1)
        User.objects.create_superuser("boss", "b@x.fr", "pw-Secret-123")
        self.client.login(username="boss", password="pw-Secret-123")
        r = self.client.get("/admin-hub/activity/?p=7")
        for s in ("Activité du site", "Belgique", "tiktok.com", "Accueil · Créer ma peinture (haut)", "/create/"):
            self.assertContains(r, s)
        self.assertEqual(self._post({"t": "pv", "p": "/"}).status_code, 204)          # equipe connectee : exclue


class GeoMerchantTests(TestCase):
    def test_robots_welcomes_ai_crawlers(self):
        body = self.client.get("/robots.txt").content.decode()
        for bot in ("GPTBot", "OAI-SearchBot", "PerplexityBot", "ClaudeBot", "Google-Extended"):
            self.assertIn("User-agent: %s" % bot, body)
        self.assertIn("Disallow: /checkout/", body)

    def test_company_identity_exposed(self):
        from django.core.cache import cache
        from .models import CompanyInfo
        c = CompanyInfo.get()
        c.legal_name, c.siret, c.vat_number, c.address = "PaintIt SAS", "12345678900011", "FR00123456789", "1 rue d'Art\n75001 Paris"
        c.save()
        cache.clear()
        html = self.client.get("/").content.decode()
        self.assertIn('"legalName":"PaintIt SAS"', html)
        self.assertIn('"vatID":"FR00123456789"', html)
        self.assertIn("PaintIt SAS", self.client.get("/llm.txt").content.decode())
        self.assertEqual(self.client.get("/llm.json").json()["company"]["siret"], "12345678900011")

    def test_merchant_feed(self):
        import xml.etree.ElementTree as ET
        r = self.client.get("/feeds/google-merchant.xml")
        self.assertEqual(r.status_code, 200)
        root = ET.fromstring(r.content)
        g = "{http://base.google.com/ns/1.0}"
        items = root.findall("./channel/item")
        self.assertTrue(items)
        for it in items:
            self.assertTrue(it.find(g + "price").text.endswith(" EUR"))
            self.assertEqual(self.client.get(it.find("link").text.replace("http://testserver", "")
                                             .split("paintit.click")[-1]).status_code, 200)


class ImageBankTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        User.objects.create_superuser("boss", "b@x.fr", "pw-Secret-123")
        self.client.login(username="boss", password="pw-Secret-123")

    def _fake_session(self):
        import io as _io
        from PIL import Image as _Im

        def jpg(color):
            b = _io.BytesIO()
            im = _Im.new("RGB", (64, 64), color)
            if color != (128, 128, 128):
                for x in range(32):
                    for y in range(64):
                        im.putpixel((x, y), (20, 60, 230))
            im.save(b, "JPEG")
            return b.getvalue()

        class R:
            def __init__(self, data=None, content=b""):
                self._d, self.content = data, content

            def raise_for_status(self):
                pass

            def json(self):
                return self._d

        ov = {"results": [
            {"id": "a1", "title": "Parrot", "url": "https://x/a1.jpg", "thumbnail": "https://x/t-vivid", "width": 3000,
             "height": 2000, "license": "cc0", "creator": "Bob", "foreign_landing_url": "https://x/a1", "tags": []},
            {"id": "a2", "title": "Grey", "url": "https://x/a2.jpg", "thumbnail": "https://x/t-grey", "width": 3000,
             "height": 2000, "license": "pdm", "tags": []},
            {"id": "a3", "title": "Tiny", "url": "https://x/a3.jpg", "thumbnail": "https://x/t-vivid", "width": 600,
             "height": 400, "license": "cc0", "tags": []},
            {"id": "a4", "title": "Woman with flowers", "url": "https://x/a4.jpg", "thumbnail": "https://x/t-vivid",
             "width": 2400, "height": 3000, "license": "cc0", "tags": [{"name": "woman"}]}]}
        wm = {"query": {"pages": {
            "1": {"pageid": 11, "title": "File:Sunset_sea.jpg", "imageinfo": [{
                "url": "https://w/s.jpg", "thumburl": "https://x/t-vivid", "width": 4000, "height": 3000,
                "mime": "image/jpeg", "descriptionurl": "https://w/p",
                "extmetadata": {"LicenseShortName": {"value": "Public domain"}}}]},
            "2": {"pageid": 12, "title": "File:BySA.jpg", "imageinfo": [{
                "url": "https://w/b.jpg", "thumburl": "https://x/t-vivid", "width": 4000, "height": 3000,
                "mime": "image/jpeg", "extmetadata": {"LicenseShortName": {"value": "CC BY-SA 4.0"}}}]}}}}

        class S:
            def get(self, url, **kw):
                if "openverse" in url:
                    return R(ov)
                if "wikimedia" in url:
                    return R(wm)
                return R(content=jpg((128, 128, 128) if "grey" in url else (240, 40, 40)))
        return S()

    def test_harvest_filters_and_admin_flow(self):
        from studio import imagebank
        from studio.models import StockImage
        with mock.patch("studio.imagebank._session", side_effect=self._fake_session):
            added, skipped = imagebank.harvest(categories=["Oiseaux"], providers=["openverse", "wikimedia"],
                                               per_query=5, pause=0)
            again, _ = imagebank.harvest(categories=["Oiseaux"], providers=["openverse"], per_query=5, pause=0)
        self.assertEqual(again, 0)                                          # doublons ignores
        titles = set(StockImage.objects.values_list("title", flat=True))
        self.assertEqual(titles, {"Parrot", "Woman with flowers", "Sunset sea"})   # pas de terne, petite, CC BY-SA
        self.assertTrue(StockImage.objects.get(title="Woman with flowers").people)
        p = StockImage.objects.get(title="Parrot")
        self.assertGreater(p.vibrancy, 35)
        self.assertEqual(p.orientation, "paysage")
        r = self.client.get("/admin-hub/images/")
        self.assertContains(r, "Banque d'images"); self.assertContains(r, "Parrot")
        self.assertNotContains(r, "Woman with flowers")                    # personnes masquees par defaut
        self.assertEqual(self.client.get("/admin-hub/images/%d.jpg" % p.pk).status_code, 200)
        self.client.post("/admin-hub/images/", {"op": "kept", "ids": [p.pk]})
        self.assertEqual(StockImage.objects.get(pk=p.pk).status, "kept")
        hd = os.path.join(MEDIA, "uploads", "stock-test.jpg")
        os.makedirs(os.path.dirname(hd), exist_ok=True)

        def fake_hd(img, max_side=3000):
            open(hd, "wb").write(b"x")
            return hd
        with mock.patch("studio.imagebank.fetch_hd", side_effect=fake_hd), mock.patch("studio.pipeline.generate") as gen:
            r = self.client.post("/admin-hub/images/", {"op": "studio", "id": p.pk})
        self.assertTrue(r["Location"].startswith("/marketing/?img=mkt-"))
        self.assertTrue(gen.called and not os.path.exists(hd))
        self.assertIn("studio mkt-", StockImage.objects.get(pk=p.pk).used_in)

    def test_rate_limit_skips_source_and_artic(self):
        from studio import imagebank
        from studio.models import StockImage
        base = self._fake_session()

        class R429:
            status_code, headers = 429, {"Retry-After": "3600"}

        aic = {"data": [{"id": 7, "title": "Starry", "artist_title": "V.", "image_id": "abc", "is_public_domain": True,
                         "thumbnail": {"width": 3000, "height": 2400}}]}

        class S:
            calls = 0

            def get(self, url, **kw):
                if "openverse" in url:
                    S.calls += 1
                    return R429()
                if "artic.edu/api" in url:
                    r = base.get("https://x/t-vivid"); r._d = aic
                    return r
                return base.get(url, **kw)
        logs = []
        with mock.patch("studio.imagebank._session", side_effect=S):
            added, _ = imagebank.harvest(categories=["Oiseaux"], providers=["openverse", "artic"], per_query=5,
                                         pause=0, log=logs.append)
        self.assertEqual(S.calls, 1)                                       # source mise de cote apres le 1er 429
        self.assertEqual(len([l for l in logs if "openverse" in l]), 1)
        a = StockImage.objects.get(provider="artic")
        self.assertEqual((a.width, a.height, a.license), (1686, 1348, "CC0"))
        self.assertIn("/iiif/2/abc/full/1686,", a.full_url)

    def test_catalogue_add_from_bank(self):
        from studio.models import StockImage
        img = StockImage.objects.create(provider="artic", ext_id="9", title="Sunflowers", category="Fleurs",
                                        full_url="https://x/s.jpg", width=2000, height=2500, vibrancy=70, status="kept")
        self.assertContains(self.client.get("/admin-hub/catalogue/"), 'data-id="%d"' % img.pk)
        hd = os.path.join(MEDIA, "uploads", "stock-cat.jpg")
        os.makedirs(os.path.dirname(hd), exist_ok=True)

        def fake_hd(i, max_side=3000):
            open(hd, "wb").write(b"x")
            return hd

        class M:
            uid, title = "gal-sunflowers", "Sunflowers"
        with mock.patch("studio.imagebank.fetch_hd", side_effect=fake_hd), \
                mock.patch("studio.views._create_gallery_model", return_value=M()) as cg:
            self.client.post("/admin-hub/catalogue/", {"op": "add", "stock_id": img.pk, "title": "Sunflowers",
                                                       "category": "Fleurs", "in_slider": "1", "colors": "24"})
        self.assertTrue(cg.called and cg.call_args.kwargs["in_slider"] and cg.call_args.kwargs["image_path"] == hd)
        self.assertIn("catalogue gal-sunflowers", StockImage.objects.get(pk=img.pk).used_in)

    def test_delete_sort_and_page_links(self):
        from studio.models import StockImage
        for k in range(130):
            StockImage.objects.create(provider="artic", ext_id="p%d" % k, title="Img %d" % k, full_url="https://x",
                                      width=2000, height=2000, vibrancy=50 + k % 7)
        first = StockImage.objects.order_by("created_at", "pk").first()
        r = self.client.get("/admin-hub/images/?sort=oldest")
        self.assertEqual(r.context["page"].object_list[0].pk, first.pk)
        self.assertEqual(r.context["page_links"], [1, 2, 3])
        self.assertContains(r, "Aller à")
        self.assertEqual(self.client.get("/admin-hub/images/?page=3").context["page"].number, 3)
        self.client.post("/admin-hub/images/", {"op": "delete", "ids": [first.pk]})
        self.assertEqual(StockImage.objects.get(pk=first.pk).status, "deleted")
        self.assertNotIn(first.pk, [i.pk for i in self.client.get("/admin-hub/images/?sort=oldest").context["page"]])
        self.assertEqual(self.client.get("/admin-hub/images/").context["total"], 129)

    def test_swipe_triage(self):
        import json as _json
        from studio.models import StockImage
        a = StockImage.objects.create(provider="artic", ext_id="s1", title="A", full_url="https://x", vibrancy=80)
        b = StockImage.objects.create(provider="artic", ext_id="s2", title="B", full_url="https://x", vibrancy=60)
        self.assertContains(self.client.get("/admin-hub/images/tri/"), "Tri express")
        d = self.client.get("/admin-hub/images/tri/?format=json").json()
        self.assertEqual(([i["id"] for i in d["items"]], d["left"]), ([a.pk, b.pk], 2))
        self.assertEqual([i["id"] for i in self.client.get("/admin-hub/images/tri/?format=json&skip=%d" % a.pk)
                          .json()["items"]], [b.pk])
        post = lambda pk, st: self.client.post("/admin-hub/images/tri/", _json.dumps({"id": pk, "status": st}),
                                               content_type="application/json").json()
        self.assertEqual(post(a.pk, "kept"), {"ok": True, "left": 1})
        self.assertEqual(post(b.pk, "rejected")["left"], 0)
        self.assertEqual(post(b.pk, "new")["left"], 1)                    # annuler
        self.assertEqual(StockImage.objects.get(pk=a.pk).status, "kept")

    def test_bulk_select_all_filtered_gallery_and_videos(self):
        from studio.models import StockImage
        for k in range(70):
            StockImage.objects.create(provider="artic", ext_id="b%d" % k, title="Img %d" % k, category="Fleurs" if k < 65 else "Espace",
                                      full_url="https://x", width=2000, height=2000, vibrancy=60)
        r = self.client.get("/admin-hub/images/?cat=Fleurs")
        self.assertContains(r, "Sélectionner les 65 images du filtre")
        with mock.patch("studio.imagebank.bulk_async", return_value=True) as ba:
            self.client.post("/admin-hub/images/", {"op": "bulk_gallery", "all": "1", "filter_qs": "cat=Fleurs",
                                                    "colors": "36", "density": "0.45|1.0", "price": "2.5"})
            kind, ids, opts = ba.call_args.args
            self.assertEqual((kind, len(ids), opts["colors"], opts["price"], opts["density"]), ("gallery", 65, 36, "2.5", "0.45|1.0"))
            one = StockImage.objects.filter(category="Espace").first()
            self.client.post("/admin-hub/images/", {"op": "bulk_video", "ids": [one.pk], "fmt": "guess", "music_style": "space"})
            kind, ids, opts = ba.call_args.args
            self.assertEqual((kind, ids, opts["fmt"], opts["music_style"]), ("video", [one.pk], "guess", "space"))

    def test_bulk_worker_creates_video_and_gallery(self):
        from studio import imagebank
        from studio.models import StockImage, SocialVideo
        img = StockImage.objects.create(provider="artic", ext_id="w1", title="Sunflowers", category="Fleurs",
                                        full_url="https://x", width=2000, height=2000, vibrancy=60)
        hd = os.path.join(MEDIA, "uploads", "stock-w1.jpg")
        os.makedirs(os.path.dirname(hd), exist_ok=True)

        def fake_hd(i, max_side=3000):
            open(hd, "wb").write(b"x")
            return hd

        class M:
            uid = "gal-sun"
        with mock.patch("studio.imagebank.fetch_hd", side_effect=fake_hd), mock.patch("studio.pipeline.generate"), \
                mock.patch("studio.satisfying.save_label"), mock.patch("studio.satisfying.enqueue") as enq, \
                mock.patch("studio.views._create_gallery_model", return_value=M()) as cg:
            imagebank._bulk_one("video", img, {"colors": 24, "fmt": "tips", "music_style": "electro", "hooks": ["x"]})
            imagebank._bulk_one("gallery", img, {"colors": 36, "price": "0", "density": ""})
        v = SocialVideo.objects.get()
        self.assertEqual((v.fmt, v.music_style, v.duration), ("tips", "electro", 21.0))
        self.assertTrue(enq.called and cg.call_args.kwargs["colors"] == 36)
        used = StockImage.objects.get(pk=img.pk).used_in
        self.assertIn("vidéo vid-", used); self.assertIn("catalogue gal-sun", used)


@override_settings(MEDIA_ROOT=MEDIA)
class PlayFiltersTests(TestCase):
    def test_free_first_and_filters(self):
        for k, (c, p) in enumerate([("Animaux", 3), ("Fleurs", 0), ("Animaux", 0)]):
            DigitalCanvas.objects.create(uid="gal-t%d" % k, email="__library__", source="library", title="T%d" % k,
                                         category=c, price=p, colors=24, zones=10)
        r = self.client.get("/play/")
        self.assertEqual([m.uid for m in r.context["models"]][-1], "gal-t0")          # payante en dernier
        self.assertEqual((r.context["n_free"], r.context["n_paid"]), (2, 1))
        self.assertEqual(r.context["cats"], [("Animaux", "Animaux", 2), ("Fleurs", "Fleurs", 1)])
        self.assertContains(r, 'id="gcat"'); self.assertContains(r, 'value="free"')
        self.assertContains(r, 'id="gsort"'); self.assertContains(r, 'data-zones="10"')

    def test_paid_model_has_5_min_trial_then_buy(self):
        d = os.path.join(MEDIA, "orders", "gal-paid1")
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "gal-paid1_digipaint.svg"), "w").write('<svg viewBox="0 0 10 10"></svg>')
        DigitalCanvas.objects.create(uid="gal-paid1", email="__library__", source="library", title="P", price=4.9)
        r = self.client.get("/play/gal-paid1/")
        self.assertEqual(r.status_code, 200)                                   # plus de redirection vers l'achat
        self.assertFalse(r.context["owned"])                                   # minuteur d'essai actif
        self.assertEqual(r.context["buy"]["url"], "/play/buy/gal-paid1/")
        self.assertContains(r, "Acheter et continuer")
        self.assertContains(self.client.get("/play/"), 'href="/play/gal-paid1/"')

    def test_categories_translated(self):
        from studio import categories
        self.assertEqual(categories.label("papillons & insectes", "de"), "Schmetterlinge & Insekten")
        self.assertEqual(categories.label("Ete", "it"), "Estate")
        self.assertEqual(categories.label("Truc perso", "en"), "Truc perso")
        DigitalCanvas.objects.create(uid="gal-c1", email="__library__", source="library", title="X", category="Fleurs")
        r = self.client.get("/en/play/")
        self.assertContains(r, '<option value="fleurs">Flowers (1)</option>', html=False)
        self.assertContains(r, 'data-catl="flowers"')

    def test_game_has_order_kit_cta(self):
        d = os.path.join(MEDIA, "orders", "gal-kit1")
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "gal-kit1_digipaint.svg"), "w").write('<svg viewBox="0 0 400 500"></svg>')
        DigitalCanvas.objects.create(uid="gal-kit1", email="__library__", source="library", title="K", price=0)
        r = self.client.get("/play/gal-kit1/")
        self.assertContains(r, 'id="kitBtn"'); self.assertContains(r, 'href="/play/gal-kit1/buy/"')
        self.assertContains(r, "chez un proche")
        self.assertRedirects(self.client.get("/play/gal-kit1/buy/"), "/preview/", fetch_redirect_response=False)

    def test_famous_painting_caption(self):
        from studio.models import StockImage
        DigitalCanvas.objects.create(uid="gal-art1", email="__library__", source="library", title="The Starry Night",
                                     category="Tableaux célèbres")
        DigitalCanvas.objects.create(uid="gal-fl1", email="__library__", source="library", title="Rose", category="Fleurs")
        StockImage.objects.create(provider="artic", ext_id="a1", title="x", full_url="https://x",
                                  author="Vincent van Gogh", used_in="catalogue gal-art1")
        r = self.client.get("/play/")
        self.assertContains(r, '<span class="g-art">The Starry Night · Vincent van Gogh</span>', html=False)
        self.assertNotContains(r, '<span class="g-art">Rose')

    def test_collab_room_create_sync_and_access(self):
        import json as _json
        from studio.models import CollabRoom
        d = os.path.join(MEDIA, "orders", "gal-col1")
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "gal-col1_digipaint.svg"), "w").write('<svg viewBox="0 0 400 500"></svg>')
        DigitalCanvas.objects.create(uid="gal-col1", email="__library__", source="library", title="C", price=0)
        r = self.client.post("/play/gal-col1/collab/", _json.dumps({"painted": {"3": "#ff0000", "x": "#fff", "5": "red;}"}}),
                             content_type="application/json").json()
        room = CollabRoom.objects.get(token=r["token"])
        self.assertEqual(room.painted, {"3": "#ff0000"})                      # valeurs suspectes ignorees
        self.assertIn("?room=" + room.token, r["url"])
        g = self.client.get("/collab/%s/?cid=a&v=0" % room.token).json()
        self.assertEqual((g["painted"], g["peers"]), ({"3": "#ff0000"}, 1))
        p = self.client.post("/collab/%s/?cid=b" % room.token, _json.dumps({"zones": {"7": "rgb(1, 2, 3)", "3": "#000"}}),
                             content_type="application/json").json()
        self.assertEqual(p["peers"], 2)
        room.refresh_from_db()
        self.assertEqual(room.painted, {"3": "#ff0000", "7": "rgb(1, 2, 3)"})    # premiere couleur conservee
        self.assertNotIn("painted", self.client.get("/collab/%s/?v=%d" % (room.token, room.version)).json())
        self.assertEqual(self.client.get("/collab/nope/").status_code, 404)
        # modele payant non achete : pas d'invitation pendant l'essai ; un invite n'a pas de minuteur
        DigitalCanvas.objects.filter(uid="gal-col1").update(price=4)
        self.assertEqual(self.client.post("/play/gal-col1/collab/", "{}", content_type="application/json").status_code, 403)
        self.assertTrue(self.client.get("/play/gal-col1/?room=" + room.token).context["owned"])
        self.assertFalse(self.client.get("/play/gal-col1/").context["owned"])

    def test_order_kit_from_game_keeps_this_canvas_photo(self):
        d = os.path.join(MEDIA, "orders", "AB12CD34-5678")
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "AB12CD34-5678_digipaint.svg"), "w").write('<svg viewBox="0 0 400 500"></svg>')
        src = os.path.join(d, "AB12CD34-5678_source_chat.jpg")
        open(src, "wb").write(b"x")
        other = os.path.join(MEDIA, "uploads", "autre.jpg")
        os.makedirs(os.path.dirname(other), exist_ok=True)
        open(other, "wb").write(b"y")
        s = self.client.session; s["last_photo"] = other; s.save()       # photo d'une creation precedente
        self.client.get("/play/AB12CD34-5678/buy/")
        self.assertEqual(self.client.session["last_photo"], src)        # « Modifier » repart de cette toile
        self.assertTrue(self.client.get("/create/").context["has_photo"])
        os.remove(src)                                                   # photo d'origine purgee : pas de mauvaise photo
        s = self.client.session; s["last_photo"] = other; s.save()
        self.client.get("/play/AB12CD34-5678/buy/")
        self.assertNotIn("last_photo", self.client.session)


from django.test import TransactionTestCase  # noqa: E402


@override_settings(CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}})
class CollabWebSocketTests(TransactionTestCase):
    def test_paint_cursor_and_score_relay(self):
        import sys
        import types
        from asgiref.sync import async_to_sync
        if "daphne" not in sys.modules:                 # channels.testing importe daphne (serveur non utilise ici)
            for name in ("daphne", "daphne.testing", "daphne.server", "daphne.endpoints"):
                sys.modules.setdefault(name, types.ModuleType(name))
            sys.modules["daphne.testing"].DaphneProcess = object
        from channels.testing.websocket import WebsocketCommunicator
        from studio.consumers import CollabConsumer
        from studio.models import CollabRoom
        CollabRoom.objects.create(token="tok1", uid="gal-x", painted={"1": "#111111"}, version=1)
        app = CollabConsumer.as_asgi()

        async def run():
            a = WebsocketCommunicator(app, "/ws/collab/tok1/")
            a.scope["url_route"] = {"kwargs": {"token": "tok1"}}
            b = WebsocketCommunicator(app, "/ws/collab/tok1/")
            b.scope["url_route"] = {"kwargs": {"token": "tok1"}}
            assert (await a.connect())[0]
            st = await a.receive_json_from()
            assert st["t"] == "state" and st["painted"] == {"1": "#111111"}
            await a.receive_json_from()                                  # son propre « hello »
            assert (await b.connect())[0]
            await b.receive_json_from(); await b.receive_json_from()     # state + hello de b
            hello = await a.receive_json_from()
            assert hello["t"] == "hello"
            await b.send_json_to({"t": "paint", "z": {"2": "#00ff00", "1": "#000", "bad": "x"}})
            m = await a.receive_json_from()
            assert m["t"] == "paint" and m["z"] == {"2": "#00ff00"}        # premiere couleur gagne, valeurs filtrees
            await b.receive_json_from()
            await b.send_json_to({"t": "cur", "x": 12.34, "y": 5, "n": "🦊"})
            c = await a.receive_json_from()
            assert c["t"] == "cur" and c["x"] == 12.3 and c["n"] == "🦊"
            await b.receive_json_from()
            await b.send_json_to({"t": "score", "s": 320, "n": "🦊"})
            sc = await a.receive_json_from()
            assert sc["t"] == "score" and sc["s"] == 320
            await a.disconnect(); await b.disconnect()
            bad = WebsocketCommunicator(app, "/ws/collab/nope/")
            bad.scope["url_route"] = {"kwargs": {"token": "nope"}}
            assert not (await bad.connect())[0]
        async_to_sync(run)()
        self.assertEqual(CollabRoom.objects.get(token="tok1").painted, {"1": "#111111", "2": "#00ff00"})

        async def reload_same_player():
            def comm(q):
                c = WebsocketCommunicator(app, "/ws/collab/tok1/?" + q)
                c.scope["url_route"] = {"kwargs": {"token": "tok1"}}
                return c
            watcher, old, new = comm("pid=watcher01"), comm("pid=player0001"), comm("pid=player0001")
            await watcher.connect(); await watcher.receive_json_from(); await watcher.receive_json_from()
            await old.connect(); st = await old.receive_json_from()
            assert st["me"]["id"] == "player0001"
            await watcher.receive_json_from()                            # hello de l'ancien onglet
            await new.connect(); st2 = await new.receive_json_from()
            assert st2["me"] == st["me"]                                  # meme joueur, meme couleur
            await watcher.receive_json_from()                            # hello du nouvel onglet (meme id)
            await old.disconnect()                                       # l'ancien onglet se ferme...
            assert await watcher.receive_nothing(0.2)                     # ...sans « bye » : le joueur est toujours la
            await new.disconnect()
            bye = await watcher.receive_json_from()
            assert bye == {"t": "bye", "id": "player0001"}
            await watcher.disconnect()
        async_to_sync(reload_same_player)()


@override_settings(MEDIA_ROOT=MEDIA)
class CreatePhotoCacheTests(TestCase):
    def test_create_page_photo_never_stale(self):
        d = os.path.join(MEDIA, "orders", "EF12CD34-9999")
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "EF12CD34-9999_digipaint.svg"), "w").write('<svg viewBox="0 0 400 500"></svg>')
        open(os.path.join(d, "EF12CD34-9999_source_a.jpg"), "wb").write(b"a")
        self.client.get("/play/EF12CD34-9999/buy/")
        r = self.client.get("/create/")
        self.assertIn("no-store", r["Cache-Control"])
        v1 = r.context["photo_v"]
        self.assertContains(r, "/current-photo/?v=" + v1)
        self.assertIn("no-store", self.client.get("/current-photo/")["Cache-Control"])
        d2 = os.path.join(MEDIA, "orders", "EF12CD34-8888")
        os.makedirs(d2, exist_ok=True)
        open(os.path.join(d2, "EF12CD34-8888_digipaint.svg"), "w").write('<svg viewBox="0 0 400 500"></svg>')
        open(os.path.join(d2, "EF12CD34-8888_source_b.jpg"), "wb").write(b"b")
        self.client.get("/play/EF12CD34-8888/buy/")
        self.assertNotEqual(self.client.get("/create/").context["photo_v"], v1)   # autre toile -> autre adresse d'image

    def test_http_players_scores_for_ladder(self):
        from studio.models import CollabRoom
        CollabRoom.objects.create(token="tokh", uid="gal-x", version=1)
        self.client.get("/collab/tokh/?cid=a&s=120&n=🦊")
        d = self.client.get("/collab/tokh/?cid=b&s=40&n=🐼").json()
        got = {p["id"]: (p["s"], p["n"]) for p in d["players"]}
        self.assertEqual(got, {"a": (120, "🦊"), "b": (40, "🐼")})
        self.assertTrue(all(p["c"].startswith("#") for p in d["players"]))

    def test_played_gallery_canvases_show_in_my_canvases(self):
        for uid, price in (("gal-pa", 0), ("gal-pb", 4.9)):
            d = os.path.join(MEDIA, "orders", uid)
            os.makedirs(d, exist_ok=True)
            open(os.path.join(d, uid + "_digipaint.svg"), "w").write('<svg viewBox="0 0 400 500"></svg>')
            DigitalCanvas.objects.create(uid=uid, email="__library__", source="library", title=uid, price=price, colors=24)
        self.client.get("/play/gal-pa/"); self.client.get("/play/gal-pb/")
        r = self.client.get("/paint/")
        self.assertEqual([c.uid for c in r.context["canvases"]], ["gal-pb", "gal-pa"])     # derniere jouee en tete
        self.assertTrue(r.context["canvases"][0].trial)
        self.assertContains(r, "Essai 5 min")
        self.client.post("/play/gal-pa/delete/")                                           # retiree de Mes toiles...
        self.assertTrue(os.path.exists(os.path.join(MEDIA, "orders", "gal-pa", "gal-pa_digipaint.svg")))  # ...fichiers intacts
        self.assertEqual([c.uid for c in self.client.get("/paint/").context["canvases"]], ["gal-pb"])
        self.client.get("/play/gal-pa/")
        s = self.client.session; s["verified_email"] = "jo@x.fr"; s.save()
        self.client.get("/paint/")
        self.assertTrue(DigitalCanvas.objects.filter(email="jo@x.fr", uid="gal-pa").exists())   # gratuite -> compte
        self.assertFalse(DigitalCanvas.objects.filter(email="jo@x.fr", uid="gal-pb").exists())  # payante : pas offerte
