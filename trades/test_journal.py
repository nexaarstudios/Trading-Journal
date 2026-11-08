from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .journal import streaks
from .models import JournalEntry


def d(day):
    return date(2026, 10, day)


class StreakMathTests(SimpleTestCase):
    def test_empty(self):
        self.assertEqual(streaks([], d(15)), {
            "current_streak": 0, "longest_streak": 0, "total_days": 0,
            "journaled_today": False, "last_entry_date": None,
        })

    def test_run_ending_today(self):
        out = streaks([d(13), d(14), d(15)], d(15))
        self.assertEqual((out["current_streak"], out["longest_streak"], out["total_days"]), (3, 3, 3))
        self.assertTrue(out["journaled_today"])
        self.assertEqual(out["last_entry_date"], d(15))

    def test_a_streak_ending_yesterday_is_still_alive(self):
        out = streaks([d(12), d(13), d(14)], d(15))
        self.assertEqual(out["current_streak"], 3)
        self.assertFalse(out["journaled_today"])

    def test_streak_breaks_after_a_missed_day(self):
        out = streaks([d(12), d(13), d(14)], d(16))
        self.assertEqual((out["current_streak"], out["longest_streak"]), (0, 3))

    def test_longest_can_be_in_the_past(self):
        out = streaks([d(1), d(2), d(3), d(4), d(8), d(9)], d(9))
        self.assertEqual((out["current_streak"], out["longest_streak"], out["total_days"]), (2, 4, 6))

    def test_duplicates_and_future_days_are_ignored(self):
        out = streaks([d(14), d(14), d(15), d(20)], d(15))
        self.assertEqual((out["current_streak"], out["total_days"]), (2, 2))
        self.assertEqual(out["last_entry_date"], d(15))


class JournalAPITests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.alice = User.objects.create_user("alice", "alice@example.com", "x")
        self.bob = User.objects.create_user("bob", "bob@example.com", "x")
        self.client = APIClient()
        self.client.force_authenticate(self.alice)
        self.bob_client = APIClient()
        self.bob_client.force_authenticate(self.bob)

    ENTRIES = "/api/journal/entries/"
    TEMPLATES = "/api/journal/templates/"

    def save(self, kind="post_market", day="2026-10-10", answer="Stuck to the plan", client=None, **extra):
        body = {"kind": kind, "date": day, "sections": [{"prompt": "How did it go?", "answer": answer}]}
        body.update(extra)
        return (client or self.client).post(self.ENTRIES, body, format="json")

    def templates(self, client=None):
        return (client or self.client).get(self.TEMPLATES).json()

    # ---- templates ----

    def test_default_templates_are_created_once_per_user(self):
        mine = self.templates()
        self.assertEqual(sorted(t["kind"] for t in mine), ["post_market", "pre_market", "weekly"])
        self.assertEqual(len(self.templates()), 3)
        self.assertEqual(len(self.client.get(self.TEMPLATES, {"kind": "pre_market"}).json()), 1)
        theirs = self.templates(self.bob_client)
        self.assertEqual(len(theirs), 3)
        self.assertFalse({t["id"] for t in mine} & {t["id"] for t in theirs})

    def test_template_crud_and_validation(self):
        self.templates()  # seeds the defaults
        make = lambda **body: self.client.post(self.TEMPLATES, body, format="json")

        ok = make(kind="pre_market", name="Gold session", prompts=["Key levels?", "News today?"])
        self.assertEqual(ok.status_code, 201)
        self.assertEqual(make(kind="pre_market", name="gold SESSION", prompts=["x"]).status_code, 400)
        self.assertEqual(make(kind="post_market", name="Gold session", prompts=["x"]).status_code, 201)
        self.assertEqual(make(kind="pre_market", name="pre-market plan", prompts=["x"]).status_code, 400)  # a default
        self.assertEqual(make(kind="pre_market", name="Blank", prompts=["ok", "  "]).status_code, 400)
        self.assertEqual(make(kind="pre_market", name="Empty", prompts=[]).status_code, 400)
        self.assertEqual(make(kind="pre_market", name="Long", prompts=["p"] * 21).status_code, 400)
        self.assertEqual(make(kind="mood", name="Odd", prompts=["x"]).status_code, 400)

        url = f"{self.TEMPLATES}{ok.json()['id']}/"
        patched = self.client.patch(url, {"prompts": ["Only one"]}, format="json")
        self.assertEqual(patched.json()["prompts"], ["Only one"])
        self.assertEqual(self.client.delete(url).status_code, 204)

    def test_creating_a_template_first_still_gets_the_defaults(self):
        res = self.client.post(
            self.TEMPLATES, {"kind": "weekly", "name": "Mine", "prompts": ["x"]}, format="json"
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(len(self.templates()), 4)

    # ---- entries ----

    def test_template_prefills_and_saving_is_an_upsert(self):
        pre = next(t for t in self.templates() if t["kind"] == "pre_market")
        opened = self.client.post(
            self.ENTRIES, {"kind": "pre_market", "date": "2026-10-12", "template": pre["id"]}, format="json"
        )
        self.assertEqual(opened.status_code, 201)
        body = opened.json()
        self.assertEqual([s["prompt"] for s in body["sections"]], pre["prompts"])
        self.assertTrue(all(s["answer"] == "" for s in body["sections"]))
        self.assertFalse(body["has_content"])

        filled = self.save(kind="pre_market", day="2026-10-12", answer="Watching EURUSD")
        self.assertEqual(filled.status_code, 200)
        self.assertEqual(filled.json()["id"], body["id"])
        self.assertTrue(filled.json()["has_content"])

        reopened = self.client.post(
            self.ENTRIES, {"kind": "pre_market", "date": "2026-10-12", "template": pre["id"]}, format="json"
        )
        self.assertEqual(reopened.status_code, 200)
        self.assertEqual(reopened.json()["sections"][0]["answer"], "Watching EURUSD")  # not wiped
        self.assertEqual(JournalEntry.objects.count(), 1)

    def test_dates_kinds_and_sections_are_validated(self):
        future = (timezone.now() + timedelta(days=2)).date().isoformat()
        self.assertEqual(self.save(day=future).status_code, 400)
        self.assertEqual(self.save(kind="weekly", day="2026-10-14").status_code, 400)  # a Wednesday
        self.assertEqual(self.save(kind="weekly", day="2026-10-12").status_code, 201)  # the Monday
        self.assertEqual(self.save(kind="diary").status_code, 400)
        self.assertEqual(self.save(day="2026-13-45").status_code, 400)
        self.assertEqual(self.client.post(self.ENTRIES, {"kind": "post_market"}, format="json").status_code, 400)

        post = next(t for t in self.templates() if t["kind"] == "post_market")
        self.assertEqual(self.save(kind="pre_market", template=post["id"]).status_code, 400)

        too_many = [{"prompt": "p", "answer": "a"}] * 31
        self.assertEqual(self.save(sections=too_many).status_code, 400)
        self.assertEqual(self.save(answer="x" * 10_001).status_code, 400)
        self.assertEqual(self.save(sections=[{"answer": "no prompt"}]).status_code, 400)

    def test_patch_and_delete(self):
        entry = self.save().json()
        url = f"{self.ENTRIES}{entry['id']}/"
        edited = self.client.patch(
            url, {"sections": [{"prompt": "How did it go?", "answer": "Revised"}]}, format="json"
        )
        self.assertEqual(edited.json()["sections"][0]["answer"], "Revised")
        self.assertEqual(self.client.patch(url, {"date": "2026-10-11"}, format="json").status_code, 400)
        self.assertEqual(self.client.patch(url, {"kind": "pre_market"}, format="json").status_code, 400)
        self.assertEqual(self.client.delete(url).status_code, 204)
        self.assertEqual(JournalEntry.objects.count(), 0)

    def test_everything_is_private(self):
        entry = self.save().json()
        url = f"{self.ENTRIES}{entry['id']}/"
        self.assertEqual(self.bob_client.get(url).status_code, 404)
        self.assertEqual(self.bob_client.patch(url, {"sections": []}, format="json").status_code, 404)
        self.assertEqual(self.bob_client.delete(url).status_code, 404)
        self.assertEqual(self.bob_client.get(self.ENTRIES).json()["count"], 0)

        alice_template = self.templates()[0]
        stolen = self.save(kind=alice_template["kind"], client=self.bob_client, template=alice_template["id"])
        self.assertEqual(stolen.status_code, 400)

        self.assertEqual(self.save(day="2026-10-10", client=self.bob_client).status_code, 201)  # same day, own entry
        self.assertEqual(JournalEntry.objects.filter(date="2026-10-10").count(), 2)
        self.assertEqual(self.bob_client.get("/api/journal/streak/", {"date": "2026-10-10"}).json()["total_days"], 1)

    def test_filters(self):
        self.save(kind="pre_market", day="2026-10-10")
        self.save(kind="post_market", day="2026-10-10")
        self.save(kind="post_market", day="2026-10-11")
        self.save(kind="post_market", day="2026-10-12")

        def count(**params):
            return self.client.get(self.ENTRIES, params).json()["count"]

        self.assertEqual(count(), 4)
        self.assertEqual(count(kind="post_market"), 3)
        self.assertEqual(count(date="2026-10-10"), 2)
        self.assertEqual(count(**{"from": "2026-10-11", "to": "2026-10-12"}), 2)
        self.assertEqual(self.client.get(self.ENTRIES, {"date": "nope"}).status_code, 400)
        self.assertEqual(self.client.get(self.ENTRIES, {"kind": "diary"}).status_code, 400)

    # ---- streak ----

    def streak(self, day):
        return self.client.get("/api/journal/streak/", {"date": day}).json()

    def test_streak_endpoint(self):
        for day in ("2026-10-10", "2026-10-11", "2026-10-12"):
            self.save(day=day)
        self.save(day="2026-10-13", answer="   ")                       # saved but blank: doesn't count
        self.save(kind="pre_market", day="2026-10-11", answer="Plan")   # same day as another entry: still one day

        on_the_12th = self.streak("2026-10-12")
        self.assertEqual(
            (on_the_12th["current_streak"], on_the_12th["longest_streak"], on_the_12th["total_days"]),
            (3, 3, 3),
        )
        self.assertTrue(on_the_12th["journaled_today"])
        self.assertEqual(on_the_12th["last_entry_date"], "2026-10-12")

        on_the_13th = self.streak("2026-10-13")
        self.assertEqual(on_the_13th["current_streak"], 3)   # yesterday's streak is still alive
        self.assertFalse(on_the_13th["journaled_today"])

        on_the_14th = self.streak("2026-10-14")
        self.assertEqual((on_the_14th["current_streak"], on_the_14th["longest_streak"]), (0, 3))

        self.assertEqual(self.client.get("/api/journal/streak/", {"date": "soon"}).status_code, 400)
        self.assertEqual(self.client.get("/api/journal/streak/").status_code, 200)

    def test_weekly_reviews_do_not_count_toward_the_streak(self):
        self.save(kind="weekly", day="2026-10-12", answer="Good week")
        out = self.streak("2026-10-12")
        self.assertEqual((out["total_days"], out["current_streak"], out["journaled_today"]), (0, 0, False))