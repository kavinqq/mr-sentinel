"""SonarQube report files served from the dashboard: only plain *.html names inside the
reports folder, never anything else on disk."""
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from reviews import sonar_reports


class ReadTest(SimpleTestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        self.dir = root / "reports"
        self.dir.mkdir()
        (self.dir / "index.html").write_text("<h1>idx</h1>")
        (self.dir / "pocket_oa.html").write_text("<p>oa</p>")
        (self.dir / "secret.txt").write_text("nope")
        (root / "leak.html").write_text("outside")
        (self.dir / "link.html").symlink_to(root / "leak.html")

    def test_serves_html_inside_the_folder(self):
        self.assertEqual(sonar_reports.read("pocket_oa.html", self.dir), b"<p>oa</p>")

    def test_refuses_traversal_and_other_extensions(self):
        for name in ("../leak.html", "secret.txt", "missing.html", "a/b.html", ".html"):
            with self.subTest(name=name):
                self.assertIsNone(sonar_reports.read(name, self.dir))

    def test_refuses_a_symlink_that_points_outside(self):
        self.assertIsNone(sonar_reports.read("link.html", self.dir))


class ViewTest(TestCase):
    databases = {"default"}

    def setUp(self):
        self.staff = User.objects.create_user("staff", password="pw-for-tests-only", is_staff=True)

    def test_report_needs_staff_login(self):
        resp = self.client.get(reverse("sonar_report", args=["index.html"]))
        self.assertNotEqual(resp.status_code, 200)

    def test_staff_gets_the_report_and_unknown_names_are_404(self):
        self.client.force_login(self.staff)
        with mock.patch.object(sonar_reports, "read", side_effect=lambda n: b"<html>ok</html>" if n == "index.html" else None):
            self.assertEqual(self.client.get(reverse("sonar_report", args=["index.html"])).status_code, 200)
            self.assertEqual(self.client.get(reverse("sonar_report", args=["nope.html"])).status_code, 404)
