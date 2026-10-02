"""Step 2X: the real Redis integration suite is mandatory in CI, never skipped."""
import unittest
from test_step2n_real_redis import validate_test_url

class TestRealRedisCIContract(unittest.TestCase):
    def test_required_url_missing_is_failure(self):
        with self.assertRaisesRegex(RuntimeError,"REAL_REDIS_REQUIRED_URL_MISSING"):
            validate_test_url(None,required=True)
    def test_local_developer_can_skip_without_url(self):
        with self.assertRaisesRegex(unittest.SkipTest,"not set"):
            validate_test_url(None)
    def test_dedicated_local_db15_is_accepted(self):
        self.assertEqual(validate_test_url("redis://127.0.0.1:6379/15",required=True),
                         "redis://127.0.0.1:6379/15")
    def test_production_or_unsafe_redis_url_never_accepted(self):
        for url in ("redis://prod.redis.example:6379/15",
                    "redis://127.0.0.1:6379/0",
                    "rediss://127.0.0.1:6379/15",
                    "redis://user:secret@127.0.0.1:6379/15",
                    "redis://127.0.0.1:6380/15",
                    "redis://127.0.0.1:6379/15?db=0"):
            with self.subTest(url=url):
                with self.assertRaisesRegex(RuntimeError,"REAL_REDIS_REQUIRED_UNSAFE_URL"):
                    validate_test_url(url,required=True)

if __name__=="__main__":unittest.main()
