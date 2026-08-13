from efb_telegram_master.rate_limiter import TelegramRateLimiter


def test_rate_limiter_waits_after_burst_is_consumed():
    now = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    limiter = TelegramRateLimiter(2, 2, now=lambda: now[0], sleep=sleep)
    limiter.wait()
    limiter.wait()
    limiter.wait()

    assert sleeps == [0.5]
