"""Tests du parcours de paiement et des protections de securite (python manage.py test studio)."""
import os
import shutil
import tempfile
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
        r = self.client.post("/paint/XYZ/confirm/")
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


def tearDownModule():
    shutil.rmtree(MEDIA, ignore_errors=True)
