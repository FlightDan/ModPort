"""Focused tests for checksum-verified Gradle Wrapper archive reuse."""
from hashlib import sha256
from http.client import IncompleteRead
from io import BytesIO
import threading
import tempfile
from pathlib import Path
import stat
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from unittest.mock import patch

from modport.environment_wrapper_cache import (
    EnvironmentWrapperDistributionCache,
    _gradle_url_hash,
    _redirect_is_safe,
    _SafeRedirectHandler,
    _trusted_final_url,
    _copy_verified,
    load_wrapper_distribution,
    zipfile_is_valid,
)


BASELINE_URL = "https://services.gradle.org/distributions/gradle-8.1.1-bin.zip"


def archive_bytes() -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("gradle-8.1.1/bin/gradle", b"fixture launcher")
    return output.getvalue()


class FakeResponse:
    def __init__(self, body: bytes, url: str):
        self.body = body
        self.url = url
        self.status = 200
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self.url

    def read(self, limit=-1):
        if limit < 0:
            limit = len(self.body) - self.offset
        result = self.body[self.offset:self.offset + limit]
        self.offset += len(result)
        return result


class EnvironmentWrapperDistributionCacheTests(unittest.TestCase):
    def test_zip_directory_must_be_consumed_exactly(self):
        end_record = self.archive.rfind(b"PK\x05\x06")
        self.assertGreater(end_record, 0)
        malformed = bytearray(self.archive[:end_record] + b"x" + self.archive[end_record:])
        directory_size_offset = end_record + 1 + 12
        directory_size = int.from_bytes(
            malformed[directory_size_offset:directory_size_offset + 4], "little")
        malformed[directory_size_offset:directory_size_offset + 4] = (
            directory_size + 1).to_bytes(4, "little")
        archive = self.root / "trailing-directory-byte.zip"
        archive.write_bytes(malformed)
        self.assertFalse(zipfile_is_valid(archive, "gradle-8.1.1-bin.zip"))

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache_root = self.root / "environment-cache"
        self.cache = EnvironmentWrapperDistributionCache(self.cache_root)
        self.archive = archive_bytes()
        self.digest = sha256(self.archive).hexdigest()
        properties = self.root / "mdk/gradle/wrapper/gradle-wrapper.properties"
        properties.parent.mkdir(parents=True)
        properties.write_text(
            "distributionUrl=https\\://services.gradle.org/distributions/gradle-8.1.1-bin.zip\n"
            "zipStoreBase=GRADLE_USER_HOME\n"
            "zipStorePath=wrapper/dists\n",
            encoding="utf-8",
        )
        self.distribution = load_wrapper_distribution(properties)
        self.assertIsNotNone(self.distribution)

    def test_cold_publish_and_hot_reuse_use_gradle_path_in_independent_homes(self):
        requested = []

        def open_response(url, *, expected_path, timeout):
            requested.append(url)
            if url == BASELINE_URL + ".sha256":
                # Some official service paths redirect to release hosting;
                # the cache rejects that redirect and uses the official CDN.
                raise urllib.error.URLError("untrusted redirect")
            if url == "https://downloads.gradle.org/distributions/gradle-8.1.1-bin.zip.sha256":
                return FakeResponse((self.digest + "\n").encode(), url)
            if url == BASELINE_URL:
                raise urllib.error.URLError("untrusted redirect")
            if url == "https://downloads.gradle.org/distributions/gradle-8.1.1-bin.zip":
                return FakeResponse(self.archive, url)
            raise AssertionError(f"unexpected URL: {url}")

        with patch("modport.environment_wrapper_cache._open_https_response",
                   side_effect=open_response):
            cold_home = self.root / "run-one/baseline-gradle-cache"
            cold = self.cache.seed(self.distribution, cold_home)

        self.assertEqual("stored", cold["state"])
        self.assertEqual("gradle_published", cold["checksum_source"])
        self.assertEqual([
            BASELINE_URL + ".sha256",
            "https://downloads.gradle.org/distributions/gradle-8.1.1-bin.zip.sha256",
            BASELINE_URL,
            "https://downloads.gradle.org/distributions/gradle-8.1.1-bin.zip",
        ], requested)
        url_hash = _gradle_url_hash(BASELINE_URL)
        self.assertEqual("9wiye5v2saajue4irfo8ybqfp", url_hash)
        wrapper_root = (Path("wrapper/dists/gradle-8.1.1-bin") / url_hash)
        self.assertEqual("wrapper/dists/gradle-8.1.1-bin/9wiye5v2saajue4irfo8ybqfp",
                         wrapper_root.as_posix())
        relative = wrapper_root / "gradle-8.1.1-bin.zip"
        cold_zip = cold_home / relative
        self.assertEqual(self.archive, cold_zip.read_bytes())
        self.assertTrue(cold_zip.stat().st_mode & stat.S_IWUSR)

        hot_home = self.root / "run-two/target-gradle-cache"
        with patch("modport.environment_wrapper_cache._open_https_response",
                   side_effect=AssertionError("hot read must not use the network")):
            hot = self.cache.seed(self.distribution, hot_home)
        hot_zip = hot_home / relative
        self.assertEqual("hit", hot["state"])
        self.assertEqual(cold["distribution_sha256"], hot["distribution_sha256"])
        self.assertEqual(self.archive, hot_zip.read_bytes())
        self.assertNotEqual((cold_zip.stat().st_dev, cold_zip.stat().st_ino),
                            (hot_zip.stat().st_dev, hot_zip.stat().st_ino))
        self.assertNotEqual((self.cache_root / "wrapper-distributions-v1"
                             / cold["cache_key"] / "distribution.zip").stat().st_ino,
                            hot_zip.stat().st_ino)

    def test_truncated_official_responses_fall_back_to_the_second_endpoint(self):
        requested = []

        def open_response(url, *, expected_path, timeout):
            requested.append(url)
            if url.startswith("https://services.gradle.org"):
                raise IncompleteRead(b"truncated", 100)
            if url.endswith(".sha256"):
                return FakeResponse((self.digest + "\n").encode(), url)
            return FakeResponse(self.archive, url)

        with patch("modport.environment_wrapper_cache._open_https_response",
                   side_effect=open_response):
            result = self.cache.seed(self.distribution, self.root / "private-home")
        self.assertEqual("stored", result["state"])
        self.assertEqual(4, len(requested))

    def test_fallback_download_shares_one_deadline(self):
        distribution = type(self.distribution)(
            self.distribution.url, self.distribution.filename,
            self.distribution.zip_relative_path,
            self.distribution.distribution_relative_path,
            self.distribution.marker_relative_path, self.digest)
        timeouts = []

        def open_response(url, *, expected_path, timeout):
            timeouts.append(timeout)
            if url.startswith("https://services.gradle.org"):
                time.sleep(0.03)
                raise urllib.error.URLError("first endpoint unavailable")
            return FakeResponse(self.archive, url)

        with patch("modport.environment_wrapper_cache._open_https_response",
                   side_effect=open_response):
            result = self.cache.seed(distribution, self.root / "private-home",
                                     timeout_seconds=0.15)
        self.assertEqual("stored", result["state"])
        self.assertEqual(2, len(timeouts))
        self.assertLess(timeouts[1], timeouts[0] - 0.02)

    def test_private_materialization_stops_at_deadline_and_removes_temporary_copy(self):
        large_archive = BytesIO()
        with zipfile.ZipFile(large_archive, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("gradle-8.1.1/lib/large.bin", b"x" * (3 * 1024 * 1024))
        body = large_archive.getvalue()
        pinned = type(self.distribution)(
            self.distribution.url, self.distribution.filename,
            self.distribution.zip_relative_path,
            self.distribution.distribution_relative_path,
            self.distribution.marker_relative_path, sha256(body).hexdigest())

        def open_response(url, *, expected_path, timeout):
            if url == BASELINE_URL:
                return FakeResponse(body, url)
            raise AssertionError(f"unexpected URL: {url}")

        with patch("modport.environment_wrapper_cache._open_https_response",
                   side_effect=open_response):
            stored = self.cache.seed(pinned, self.root / "warm-home")
        self.assertEqual("stored", stored["state"])

        copying = [False]
        clock = [100.0]
        original_copy = _copy_verified

        def monotonic():
            if copying[0]:
                clock[0] += 0.3
            return clock[0]

        def start_slow_copy(*args, **kwargs):
            copying[0] = True
            return original_copy(*args, **kwargs)

        destination_home = self.root / "slow-home"
        with (
            patch("modport.environment_wrapper_cache.time.monotonic",
                  side_effect=monotonic),
            patch("modport.environment_wrapper_cache._copy_verified",
                  side_effect=start_slow_copy),
            patch("modport.environment_wrapper_cache._open_https_response",
                  side_effect=AssertionError("a hot seed must not use the network")),
        ):
            with self.assertRaisesRegex(TimeoutError, "deadline expired"):
                self.cache.seed(pinned, destination_home, timeout_seconds=1.0)

        self.assertFalse((destination_home / pinned.zip_relative_path).exists())
        self.assertEqual([], list(destination_home.rglob(".wrapper-distribution-*")))
        cached_archive = (self.cache_root / "wrapper-distributions-v1"
                          / stored["cache_key"] / "distribution.zip")
        self.assertTrue(cached_archive.is_file())

    def test_redirect_does_not_drain_unbounded_body(self):
        class RedirectBody:
            closed = False

            def read(self, *_args):
                raise AssertionError("redirect body must not be read")

            def close(self):
                self.closed = True

        body = RedirectBody()
        destination = ("https://github.com/gradle/gradle-distributions/releases/"
                       "download/v8.1.1/gradle-8.1.1-bin.zip")
        handler = _SafeRedirectHandler(
            expected_path="/distributions/gradle-8.1.1-bin.zip",
            deadline=time.monotonic() + 1)
        opened = []

        class Parent:
            def open(self, request, timeout):
                opened.append((request.full_url, timeout,
                               dict(request.header_items())))
                return "redirected"

        handler.parent = Parent()
        request = urllib.request.Request(BASELINE_URL)
        request.add_unredirected_header("Host", "services.gradle.org")
        response = handler.http_error_302(
            request, body, 307, "Temporary Redirect", {"location": destination})
        self.assertEqual("redirected", response)
        self.assertTrue(body.closed)
        self.assertEqual(destination, opened[0][0])
        self.assertGreater(opened[0][1], 0)
        self.assertLessEqual(opened[0][1], 1)
        self.assertNotIn("Host", opened[0][2])

    def test_configured_sum_is_required_for_nonofficial_hosts_and_never_fetches_them(self):
        properties = self.root / "mirror/gradle/wrapper/gradle-wrapper.properties"
        properties.parent.mkdir(parents=True)
        properties.write_text(
            "distributionUrl=https\\://mirror.example/distributions/gradle-8.1.1-bin.zip\n"
            f"distributionSha256Sum={self.digest}\n",
            encoding="utf-8",
        )
        distribution = load_wrapper_distribution(properties)
        with patch("modport.environment_wrapper_cache._open_https_response",
                   side_effect=AssertionError("custom URLs must not be fetched on the host")):
            result = self.cache.seed(distribution, self.root / "private-home")
        self.assertIsNone(result)
        self.assertFalse(self.cache_root.exists())

    def test_cold_checksum_mismatch_is_not_published_or_materialized(self):
        bad_distribution = type(self.distribution)(
            self.distribution.url,
            self.distribution.filename,
            self.distribution.zip_relative_path,
            self.distribution.distribution_relative_path,
            self.distribution.marker_relative_path,
            "0" * 64,
        )

        def open_response(url, *, expected_path, timeout):
            if url == BASELINE_URL:
                return FakeResponse(self.archive, url)
            if url == "https://downloads.gradle.org/distributions/gradle-8.1.1-bin.zip":
                return FakeResponse(self.archive, url)
            raise AssertionError(f"unexpected URL: {url}")

        destination = self.root / "private-home"
        with patch("modport.environment_wrapper_cache._open_https_response",
                   side_effect=open_response):
            result = self.cache.seed(bad_distribution, destination)
        self.assertIsNone(result)
        self.assertFalse((destination / self.distribution.zip_relative_path).exists())
        self.assertFalse(self.cache._entry(BASELINE_URL).exists())

    def test_tampered_host_archive_is_rejected_before_materialization(self):
        def open_response(url, *, expected_path, timeout):
            if url.endswith(".sha256"):
                return FakeResponse((self.digest + "\n").encode(), url)
            if url == BASELINE_URL:
                return FakeResponse(self.archive, url)
            if url == "https://downloads.gradle.org/distributions/gradle-8.1.1-bin.zip":
                return FakeResponse(self.archive, url)
            raise AssertionError(f"unexpected URL: {url}")

        with patch("modport.environment_wrapper_cache._open_https_response",
                   side_effect=open_response):
            published = self.cache.seed(self.distribution, self.root / "first-home")
        cached = (self.cache_root / "wrapper-distributions-v1"
                  / published["cache_key"] / "distribution.zip")
        cached.chmod(0o644)
        cached.write_bytes(b"tampered")
        cached.chmod(0o444)
        destination = self.root / "second-home"
        with self.assertRaisesRegex(ValueError, "trusted checksum"):
            self.cache.seed(self.distribution, destination)
        self.assertFalse((destination / self.distribution.zip_relative_path).exists())

    def test_custom_zip_store_path_bypasses_cache_and_unsafe_urls_are_rejected(self):
        properties = self.root / "custom/gradle/wrapper/gradle-wrapper.properties"
        properties.parent.mkdir(parents=True)
        properties.write_text(
            "distributionUrl=https\\://services.gradle.org/distributions/gradle-8.1.1-bin.zip\n"
            "zipStorePath=custom/dists\n",
            encoding="utf-8",
        )
        self.assertIsNone(load_wrapper_distribution(properties))

        for url in (
            "http://services.gradle.org/distributions/gradle-8.1.1-bin.zip",
            "https://user@services.gradle.org/distributions/gradle-8.1.1-bin.zip",
            "https://services.gradle.org/distributions/gradle-8.1.1-bin.zip?token=x",
            "https://services.gradle.org/distributions/../gradle-8.1.1-bin.zip",
        ):
            properties.write_text(f"distributionUrl={url}\n", encoding="utf-8")
            with self.subTest(url=url), self.assertRaises(ValueError):
                load_wrapper_distribution(properties)

    def test_redirect_policy_allows_only_exact_gradle_github_asset_chain(self):
        github = (
            "https://github.com/gradle/gradle-distributions/releases/download/"
            "v8.1.1/gradle-8.1.1-bin.zip"
        )
        asset = (
            "https://release-assets.githubusercontent.com/"
            "github-production-release-asset/696192900/"
            "af13de0a-0e00-4d00-b2cc-6f17ddc42aa3?token=temporary"
        )
        self.assertTrue(_redirect_is_safe(BASELINE_URL, github,
                                          expected_path="/distributions/gradle-8.1.1-bin.zip"))
        self.assertTrue(_redirect_is_safe(github, asset,
                                          expected_path="/distributions/gradle-8.1.1-bin.zip"))
        self.assertTrue(_trusted_final_url(asset,
                                           "/distributions/gradle-8.1.1-bin.zip"))
        self.assertFalse(_redirect_is_safe(BASELINE_URL, asset,
                                           expected_path="/distributions/gradle-8.1.1-bin.zip"))
        self.assertFalse(_redirect_is_safe(
            BASELINE_URL,
            "https://github.com/other/project/releases/download/v8.1.1/gradle-8.1.1-bin.zip",
            expected_path="/distributions/gradle-8.1.1-bin.zip"))
        self.assertFalse(_trusted_final_url(
            "https://release-assets.githubusercontent.com/asset.zip?token=x",
            "/distributions/gradle-8.1.1-bin.zip"))

    def test_checksum_matching_zip_with_unsafe_entries_is_not_published(self):
        malformed_archives = []
        traversal = BytesIO()
        with zipfile.ZipFile(traversal, "w") as archive:
            archive.writestr("gradle-8.1.1/bin/gradle", b"launcher")
            archive.writestr("../escape", b"bad")
        malformed_archives.append(traversal.getvalue())

        symlink = BytesIO()
        with zipfile.ZipFile(symlink, "w") as archive:
            entry = zipfile.ZipInfo("gradle-8.1.1/bin/gradle")
            entry.create_system = 3
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(entry, "../../escape")
        malformed_archives.append(symlink.getvalue())

        for index, malformed in enumerate(malformed_archives):
            with self.subTest(archive=index):
                pinned = type(self.distribution)(
                    self.distribution.url,
                    self.distribution.filename,
                    self.distribution.zip_relative_path,
                    self.distribution.distribution_relative_path,
                    self.distribution.marker_relative_path,
                    sha256(malformed).hexdigest(),
                )
                with patch("modport.environment_wrapper_cache._open_https_response",
                           return_value=FakeResponse(malformed, BASELINE_URL)):
                    result = self.cache.seed(pinned, self.root / f"unsafe-home-{index}")
                self.assertIsNone(result)
                self.assertFalse(self.cache._entry(BASELINE_URL).exists())

    def test_repeat_seed_preserves_verified_zip_and_completed_wrapper_install(self):
        def open_response(url, *, expected_path, timeout):
            if url.endswith(".sha256"):
                return FakeResponse((self.digest + "\n").encode(), url)
            if url == BASELINE_URL:
                return FakeResponse(self.archive, url)
            raise AssertionError(f"unexpected URL: {url}")

        home = self.root / "phase-home"
        with patch("modport.environment_wrapper_cache._open_https_response",
                   side_effect=open_response):
            stored = self.cache.seed(self.distribution, home)
        archive_path = home / self.distribution.zip_relative_path
        original_inode = archive_path.stat().st_ino
        with patch("modport.environment_wrapper_cache._open_https_response",
                   side_effect=AssertionError("hot seed must not use the network")):
            repeated = self.cache.seed(self.distribution, home)
        self.assertEqual("already_seeded", repeated["materialization"])
        self.assertEqual(original_inode, archive_path.stat().st_ino)

        archive_path.unlink()  # Gradle removes this after successful extraction.
        wrapper_root = home / self.distribution.distribution_relative_path
        installation = wrapper_root / "gradle-8.1.1"
        (installation / "bin").mkdir(parents=True)
        (installation / "lib").mkdir()
        (installation / "bin" / "gradle").write_bytes(b"launcher")
        (installation / "lib" / "gradle-launcher-8.1.1.jar").write_bytes(b"jar")
        marker = home / self.distribution.marker_relative_path
        marker.touch()
        (wrapper_root / "gradle-8.1.1-bin.zip.lck").touch()
        marker_inode = marker.stat().st_ino
        with patch("modport.environment_wrapper_cache._open_https_response",
                   side_effect=AssertionError("installed seed must not use the network")):
            installed = self.cache.seed(self.distribution, home)
        self.assertEqual("already_installed", installed["materialization"])
        self.assertEqual(marker_inode, marker.stat().st_ino)
        self.assertFalse(archive_path.exists())
        self.assertEqual(stored["distribution_sha256"], installed["distribution_sha256"])

    def test_concurrent_cold_runs_share_one_download(self):
        calls = []
        download_started = threading.Event()
        release_download = threading.Event()
        results = []
        failures = []

        def open_response(url, *, expected_path, timeout):
            calls.append(url)
            if url.endswith(".sha256"):
                return FakeResponse((self.digest + "\n").encode(), url)
            if url == BASELINE_URL:
                download_started.set()
                if not release_download.wait(5):
                    raise TimeoutError("test download release timed out")
                return FakeResponse(self.archive, url)
            raise AssertionError(f"unexpected URL: {url}")

        def seed(index):
            try:
                results.append(self.cache.seed(
                    self.distribution, self.root / f"concurrent-home-{index}",
                    timeout_seconds=10))
            except BaseException as exc:
                failures.append(exc)

        with patch("modport.environment_wrapper_cache._open_https_response",
                   side_effect=open_response):
            first = threading.Thread(target=seed, args=(1,))
            second = threading.Thread(target=seed, args=(2,))
            first.start()
            self.assertTrue(download_started.wait(5))
            second.start()
            time.sleep(0.05)
            release_download.set()
            first.join(5)
            second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual([], failures)
        self.assertEqual(2, len(results))
        self.assertEqual(1, calls.count(BASELINE_URL))
        self.assertEqual(1, calls.count(BASELINE_URL + ".sha256"))
        self.assertCountEqual(["stored", "hit"], [result["state"] for result in results])

    def test_handlers_only_seed_and_report_wrapper_cache_for_workflow_v26_plus(self):
        from modport.contracts import OperationInput
        from modport.handlers import _sandboxed_build_command, _wrapper_cache_diagnostic

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            worktree = root / "worktree"
            worktree.mkdir()
            seed_result = {"state": "hit", "gradle_version": "8.1.1"}
            with (
                patch("modport.handlers.shutil.which", return_value="/usr/bin/bwrap"),
                patch("modport.dependency_build.dependency_mounts",
                      side_effect=lambda _root, args: ([], args)),
                patch("modport.handlers._seed_gradle_wrapper_distribution",
                      return_value=seed_result) as seed,
            ):
                old_command = OperationInput(
                    run_id="r", task_id="t", stage_id="s", command_id="c",
                    run_dir=str(root), options={"workflow_version": 25},
                )
                old_info = {}
                old_sandbox = _sandboxed_build_command(
                    root, worktree, ["bash", "/workspace/gradlew", "tasks"],
                    operation=old_command, wrapper_cache_info=old_info,
                )
                self.assertFalse(seed.called)
                self.assertEqual({}, old_info)
                self.assertEqual({}, _wrapper_cache_diagnostic(old_command, old_info))
                self.assertIn("--clearenv", old_sandbox)

                new_command = OperationInput(
                    run_id="r2", task_id="t2", stage_id="s2", command_id="c2",
                    run_dir=str(root), options={"workflow_version": 26},
                )
                new_info = {}
                _sandboxed_build_command(
                    root, worktree, ["bash", "/workspace/gradlew", "tasks"],
                    operation=new_command, wrapper_cache_info=new_info,
                )
                seed.assert_called_once_with(new_command, root, worktree, "gradle-cache")
                self.assertEqual(seed_result, new_info)
                self.assertEqual({"wrapper_distribution_cache": seed_result},
                                 _wrapper_cache_diagnostic(new_command, new_info))


if __name__ == "__main__":
    unittest.main()
