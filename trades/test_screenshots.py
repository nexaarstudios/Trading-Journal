from datetime import datetime, timedelta, timezone as dt_tz
from decimal import Decimal as D
from io import BytesIO
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient

from . import screenshots as shots
from .models import InstrumentSpec, Screenshot, Trade, TradingAccount

MEMORY = {**settings.STORAGES, "screenshots": {"BACKEND": "django.core.files.storage.InMemoryStorage"}}


def image_bytes(fmt, size=(60, 40), color=(200, 30, 30), mode="RGB", **options):
    out = BytesIO()
    Image.new(mode, size, color).save(out, fmt, **options)
    return out.getvalue()


PNG = image_bytes("PNG")
JPEG = image_bytes("JPEG")
GIF = image_bytes("GIF")
WEBP = image_bytes("WEBP")


class SniffAndTokenTests(SimpleTestCase):
    def test_real_and_fake_images(self):
        self.assertEqual(shots.sniff(PNG), ("image/png", "png"))
        self.assertEqual(shots.sniff(JPEG), ("image/jpeg", "jpg"))
        self.assertEqual(shots.sniff(GIF)[0], "image/gif")
        self.assertEqual(shots.sniff(WEBP), ("image/webp", "webp"))
        for fake in (
            b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", b"<html></html>", b"%PDF-1.7",
            b"MZ\x90\x00", b"", b"RIFF\x00\x00\x00\x00WAVE", b"GIF8", b"\x89PNG",
        ):
            self.assertIsNone(shots.sniff(fake), fake)

    def test_tokens(self):
        class Fake:
            pk = 7

        token = shots.make_token(Fake())
        self.assertEqual(shots.read_token(token), (7, False))
        self.assertEqual(shots.read_token(shots.make_token(Fake(), thumb=True)), (7, True))
        for bad in ("", None, "garbage", token + "x", signing.dumps({"s": 7}, salt="another purpose"),
                    signing.dumps({"wrong": 1}, salt=shots.SALT)):
            self.assertIsNone(shots.read_token(bad), bad)
        with override_settings(SCREENSHOT_URL_TTL=-1):  # every link is already expired
            self.assertIsNone(shots.read_token(token))


class ImageInspectTests(SimpleTestCase):
    def thumb_of(self, data):
        return Image.open(BytesIO(shots.inspect_image(data)[2]))

    def test_dimensions_and_thumbnail(self):
        width, height, thumb = shots.inspect_image(image_bytes("PNG", size=(600, 400)))
        self.assertEqual((width, height), (600, 400))
        image = Image.open(BytesIO(thumb))
        self.assertEqual((image.format, image.size), ("JPEG", (480, 320)))

    def test_small_images_are_not_enlarged(self):
        self.assertEqual(self.thumb_of(PNG).size, (60, 40))

    def test_transparency_is_flattened_onto_white(self):
        see_through = image_bytes("PNG", mode="RGBA", color=(0, 0, 0, 0))
        pixel = self.thumb_of(see_through).convert("RGB").getpixel((5, 5))
        self.assertTrue(all(channel >= 250 for channel in pixel), pixel)

    def test_phone_photo_rotation_is_applied(self):
        exif = Image.Exif()
        exif[274] = 6  # "rotate 90 degrees to display"
        photo = image_bytes("JPEG", size=(60, 40), exif=exif.tobytes())
        width, height, _thumb = shots.inspect_image(photo)
        self.assertEqual((width, height), (40, 60))

    def test_unreadable_and_oversized_images_are_refused(self):
        for junk in (b"\x89PNG\r\n\x1a\n" + b"junk" * 10, PNG[: len(PNG) // 2], b"GIF89a" + b"x" * 20, b""):
            with self.assertRaises(shots.ScreenshotError):
                shots.inspect_image(junk)
        with override_settings(SCREENSHOT_MAX_PIXELS=1000):  # the test image has 2,400 pixels
            with self.assertRaises(shots.ScreenshotError):
                shots.inspect_image(PNG)


@override_settings(STORAGES=MEMORY)
class ScreenshotTests(TestCase):
    PASSWORD = "pass-12345"

    def setUp(self):
        cache.clear()  # the delete endpoint is rate limited through the cache
        User = get_user_model()
        self.alice = User.objects.create_user("alice", "alice@example.com", self.PASSWORD)
        self.bob = User.objects.create_user("bob", "bob@example.com", self.PASSWORD)
        self.account = TradingAccount.objects.create(user=self.alice, name="A", currency="USD")
        self.bob_account = TradingAccount.objects.create(user=self.bob, name="B", currency="USD")
        InstrumentSpec.objects.create(
            symbol="EURUSD", asset_class="forex", base_currency="EUR",
            quote_currency="USD", contract_size=D("100000"), pip_size=D("0.0001"),
        )
        self.trade = self.make_trade(self.account, 15)
        self.other = self.make_trade(self.account, 16)
        self.bob_trade = self.make_trade(self.bob_account, 15)
        self.client = APIClient()
        self.client.force_authenticate(self.alice)
        self.bob_client = APIClient()
        self.bob_client.force_authenticate(self.bob)

    def make_trade(self, account, day):
        closed = datetime(2026, 10, day, 12, tzinfo=dt_tz.utc)
        return Trade.objects.create(
            account=account, symbol="EURUSD", side="long", opened_at=closed - timedelta(hours=1),
            closed_at=closed, entry_price=D("1.10000"), exit_price=D("1.10500"), lots=D("1"),
        )

    def upload(self, data=PNG, name="shot.png", trade=None, client=None, **fields):
        trade = trade or self.trade
        file = SimpleUploadedFile(name, data, content_type="image/png")
        return (client or self.client).post(
            f"/api/trades/{trade.id}/screenshots/", {"file": file, **fields}, format="multipart"
        )

    def shot_of(self, response):
        return Screenshot.objects.get(pk=response.json()["id"])

    # ---- uploading and viewing ----

    def test_upload_list_and_signed_download(self):
        res = self.upload(caption="Entry on the retest")
        self.assertEqual(res.status_code, 201, res.content)
        body = res.json()
        self.assertEqual(
            (body["trade"], body["caption"], body["position"], body["content_type"], body["size"],
             body["width"], body["height"], body["url_expires_in"]),
            (self.trade.id, "Entry on the retest", 1, "image/png", len(PNG), 60, 40, 900),
        )
        self.assertIn("/api/files/screenshot/?t=", body["url"])
        self.assertIn("/api/files/screenshot/?t=", body["thumbnail_url"])
        self.assertNotEqual(body["url"], body["thumbnail_url"])
        self.assertNotIn("key", body)  # where it lives in storage stays private

        shot = Screenshot.objects.get(pk=body["id"])
        self.assertTrue(shot.key.startswith(f"screenshots/{self.alice.id}/{self.trade.id}/"))
        self.assertTrue(shot.key.endswith(".png"))
        self.assertTrue(shot.thumb_key.endswith(".thumb.jpg"))
        self.assertTrue(shots.storage().exists(shot.key))
        self.assertTrue(shots.storage().exists(shot.thumb_key))

        listed = self.client.get(f"/api/trades/{self.trade.id}/screenshots/").json()
        self.assertEqual([s["id"] for s in listed], [body["id"]])
        counts = {t["id"]: t["screenshot_count"] for t in self.client.get("/api/trades/").json()["results"]}
        self.assertEqual(counts, {self.trade.id: 1, self.other.id: 0})

        anonymous = APIClient()  # no login header: the signed link is the credential
        full = anonymous.get(body["url"])
        self.assertEqual((full.status_code, full["Content-Type"]), (200, "image/png"))
        self.assertEqual(b"".join(full.streaming_content), PNG)
        self.assertEqual(full["Content-Disposition"], "inline")
        self.assertEqual(full["X-Content-Type-Options"], "nosniff")
        small = anonymous.get(body["thumbnail_url"])
        self.assertEqual((small.status_code, small["Content-Type"]), (200, "image/jpeg"))
        thumbnail = Image.open(BytesIO(b"".join(small.streaming_content)))
        self.assertEqual((thumbnail.format, thumbnail.size), ("JPEG", (60, 40)))

    def test_links_that_must_not_work(self):
        token = self.upload().json()["url"].split("t=")[1]
        anonymous = APIClient()
        path = "/api/files/screenshot/"
        self.assertEqual(anonymous.get(path).status_code, 404)
        for bad in ("", "garbage", token[:-2] + "xx"):
            self.assertEqual(anonymous.get(path, {"t": bad}).status_code, 404, bad)
        with override_settings(SCREENSHOT_URL_TTL=-1):  # expired
            self.assertEqual(anonymous.get(path, {"t": token}).status_code, 404)
        self.assertEqual(anonymous.get(path, {"t": token}).status_code, 200)
        Screenshot.objects.all().delete()
        self.assertEqual(anonymous.get(path, {"t": token}).status_code, 404)

    def test_upload_validation(self):
        svg = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
        self.assertEqual(self.upload(data=svg, name="x.svg").status_code, 400)
        self.assertEqual(self.upload(data=b"<html><script>1</script></html>", name="x.png").status_code, 400)
        self.assertEqual(self.upload(data=b"%PDF-1.7 fake", name="x.pdf").status_code, 400)
        self.assertEqual(self.upload(data=b"", name="x.png").status_code, 400)
        # Looks like a PNG from the outside but isn't one
        self.assertEqual(self.upload(data=b"\x89PNG\r\n\x1a\n" + b"junk" * 10).status_code, 400)
        self.assertEqual(self.upload(data=PNG[: len(PNG) // 2]).status_code, 400)
        url = f"/api/trades/{self.trade.id}/screenshots/"
        self.assertEqual(self.client.post(url, {"caption": "x"}, format="multipart").status_code, 400)
        self.assertEqual(self.upload(caption="c" * 201).status_code, 400)
        self.assertEqual(Screenshot.objects.count(), 0)

        with override_settings(SCREENSHOT_MAX_BYTES=len(PNG)):
            self.assertEqual(self.upload(data=PNG + b"x").status_code, 400)  # one byte over
            self.assertEqual(self.upload(data=PNG).status_code, 201)         # exactly the limit
        with override_settings(SCREENSHOT_MAX_PIXELS=1000):
            self.assertEqual(self.upload().status_code, 400)

        for name, data in (("a.jpg", JPEG), ("b.gif", GIF), ("c.webp", WEBP)):
            self.assertEqual(self.upload(data=data, name=name).status_code, 201, name)

    def test_limit_per_trade(self):
        with override_settings(SCREENSHOT_MAX_PER_TRADE=2):
            first, second = self.upload(), self.upload()
            self.assertEqual((first.json()["position"], second.json()["position"]), (1, 2))
            self.assertEqual(self.upload().status_code, 400)
            self.assertEqual(self.upload(trade=self.other).status_code, 201)  # another trade is unaffected
        self.assertEqual(Screenshot.objects.filter(trade=self.trade).count(), 2)

    def test_storage_quota_and_usage(self):
        quota = len(PNG) * 2
        with override_settings(SCREENSHOT_USER_QUOTA_BYTES=quota):
            self.assertEqual(self.upload().status_code, 201)
            self.assertEqual(self.upload(trade=self.other).status_code, 201)  # fits exactly
            refused = self.upload()
            self.assertEqual(refused.status_code, 400)
            self.assertIn("screenshot storage", str(refused.json()["file"]))
            self.assertEqual(self.client.get("/api/screenshots/usage/").json(), {
                "used_bytes": quota, "limit_bytes": quota, "remaining_bytes": 0, "count": 2,
                "max_per_trade": settings.SCREENSHOT_MAX_PER_TRADE,
                "max_file_bytes": settings.SCREENSHOT_MAX_BYTES,
            })

            self.assertEqual(self.upload(trade=self.bob_trade, client=self.bob_client).status_code, 201)  # his own allowance
            mine = Screenshot.objects.filter(trade=self.trade).first()
            self.assertEqual(self.client.delete(f"/api/screenshots/{mine.id}/").status_code, 204)
            self.assertEqual(self.upload().status_code, 201)  # deleting made room
        self.assertEqual(APIClient().get("/api/screenshots/usage/").status_code, 401)

    # ---- editing, privacy and deleting ----

    def test_caption_and_order_can_be_edited_nothing_else(self):
        shot = self.upload().json()
        url = f"/api/screenshots/{shot['id']}/"
        edited = self.client.patch(url, {"caption": "  New  ", "position": 3}, format="json").json()
        self.assertEqual((edited["caption"], edited["position"]), ("New", 3))
        for body in ({"position": -1}, {"position": 100}, {"caption": "c" * 201}):
            self.assertEqual(self.client.patch(url, body, format="json").status_code, 400, body)

        kept = self.client.patch(
            url, {"size": 1, "width": 1, "content_type": "text/html", "trade": self.other.id}, format="json"
        ).json()
        self.assertEqual(
            (kept["size"], kept["width"], kept["content_type"], kept["trade"]),
            (len(PNG), 60, "image/png", self.trade.id),
        )
        self.assertEqual(self.client.put(url, {"caption": "x"}, format="json").status_code, 405)
        self.assertEqual(self.client.get(url).status_code, 405)

    def test_screenshots_are_private(self):
        shot = self.upload().json()
        url = f"/api/screenshots/{shot['id']}/"
        trade_url = f"/api/trades/{self.trade.id}/screenshots/"
        self.assertEqual(self.bob_client.get(trade_url).status_code, 404)
        self.assertEqual(self.upload(client=self.bob_client).status_code, 404)  # alice's trade
        self.assertEqual(self.bob_client.patch(url, {"caption": "x"}, format="json").status_code, 404)
        self.assertEqual(self.bob_client.delete(url).status_code, 404)
        self.assertEqual(Screenshot.objects.count(), 1)

        anonymous = APIClient()
        self.assertEqual(anonymous.get(trade_url).status_code, 401)
        self.assertEqual(anonymous.post(trade_url).status_code, 401)
        self.assertEqual(anonymous.patch(url, {"caption": "x"}, format="json").status_code, 401)

        mine = self.upload(trade=self.bob_trade, client=self.bob_client)
        self.assertEqual(mine.status_code, 201)

    def test_deleting_a_screenshot_removes_both_files(self):
        shot = self.shot_of(self.upload())
        keys = (shot.key, shot.thumb_key)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.delete(f"/api/screenshots/{shot.id}/").status_code, 204)
        self.assertFalse(Screenshot.objects.filter(pk=shot.pk).exists())
        for key in keys:
            self.assertFalse(shots.storage().exists(key), key)
        self.assertEqual(self.client.get(f"/api/trades/{self.trade.id}/").json()["screenshot_count"], 0)

    def test_deleting_trades_removes_their_files(self):
        gone = [self.shot_of(self.upload()) for _ in range(2)]
        kept = self.shot_of(self.upload(trade=self.other))
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.delete(f"/api/trades/{self.trade.id}/").status_code, 204)
        for shot in gone:
            self.assertFalse(shots.storage().exists(shot.key))
            self.assertFalse(shots.storage().exists(shot.thumb_key))
        self.assertTrue(shots.storage().exists(kept.key))
        self.assertTrue(shots.storage().exists(kept.thumb_key))

        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(
                "/api/trades/bulk/", {"ids": [self.other.id], "action": "delete"}, format="json"
            )
        self.assertEqual(res.json()["affected"], 1)
        self.assertFalse(shots.storage().exists(kept.key))
        self.assertFalse(shots.storage().exists(kept.thumb_key))

    def test_deleting_all_data_removes_every_file(self):
        mine = [self.shot_of(self.upload()) for _ in range(2)]
        bobs = self.shot_of(self.upload(trade=self.bob_trade, client=self.bob_client))
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(
                "/api/data/delete/",
                {"password": self.PASSWORD, "scope": "data", "confirm": "DELETE"},
                format="json",
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["deleted"]["screenshots"], 2)
        for shot in mine:
            self.assertFalse(shots.storage().exists(shot.key))
            self.assertFalse(shots.storage().exists(shot.thumb_key))
        self.assertTrue(shots.storage().exists(bobs.key))
        self.assertEqual(Screenshot.objects.count(), 1)

    def test_bucket_links_when_the_storage_signs_them(self):
        body = self.upload().json()
        shot = Screenshot.objects.get(pk=body["id"])
        fake = mock.Mock()
        fake.url.return_value = "https://bucket.example/obj?X-Amz-Signature=abc"
        with override_settings(SCREENSHOT_LINKS="storage"), mock.patch.object(shots, "storage", return_value=fake):
            listed = self.client.get(f"/api/trades/{self.trade.id}/screenshots/").json()
        self.assertEqual(listed[0]["url"], "https://bucket.example/obj?X-Amz-Signature=abc")
        self.assertEqual(listed[0]["thumbnail_url"], "https://bucket.example/obj?X-Amz-Signature=abc")
        fake.url.assert_any_call(shot.key, expire=900)
        fake.url.assert_any_call(shot.thumb_key, expire=900)