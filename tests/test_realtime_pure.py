from datetime import UTC, date, datetime, time, timedelta

from ode.models import Market
from ode.notify import chunk, html_escape
from ode.realtime.hourly import detect_spikes, domain_codes, dump_url, parse_lines
from ode.realtime.trends import parse_rss, parse_traffic
from ode.scheduler import Job, is_due, jobs_from_config, parse_at
from ode.scoring.actions import TopicView, plan_topic
from ode.scoring.seasonal import seasonal_peak
from tests.conftest import make_config

H = datetime(2026, 6, 30, 12, tzinfo=UTC)


# ------------------------------------------------------------ hourly dumps
def test_dump_url_and_domain_codes():
    assert dump_url(H) == ("https://dumps.wikimedia.org/other/pageviews/2026/2026-06/"
                           "pageviews-20260630-120000.gz")
    codes = domain_codes([Market.parse("en-US"), Market.parse("ur-PK")])
    assert codes == {"en": "en.wikipedia", "en.m": "en.wikipedia",
                     "ur": "ur.wikipedia", "ur.m": "ur.wikipedia"}


def test_parse_lines_sums_desktop_and_mobile_and_ignores_other_projects():
    lines = [b"en Heat_pump 120 0", b"en.m Heat_pump 300 0", b"en.b Heat_pump 999 0",
             b"de W\xc3\xa4rmepumpe 50 0", b"en Tiny 3 0", b"garbage", b"en Bad x 0"]
    out = parse_lines(lines, domain_codes([Market.parse("en-US")]), min_row=5)
    assert out == {("en.wikipedia", "Heat pump"): 420}   # de not configured, Tiny < min_row


def series(base, recent, hours=51):
    s = {}
    for i in range(hours):
        h = H - timedelta(hours=i)
        s[h] = recent if i < 3 else base
    return s


def test_detect_spikes():
    data = {
        ("en.wikipedia", "Spiking"): series(100, 1000),       # 10x
        ("en.wikipedia", "Steady"): series(400, 420),
        ("en.wikipedia", "Small"): series(10, 150),           # 15x but tiny
        ("en.wikipedia", "Brand new"): {H: 2000, H - timedelta(hours=1): 1500},
    }
    spikes = {s.title: s for s in detect_spikes(data, H)}
    assert set(spikes) == {"Spiking", "Brand new"}
    assert spikes["Spiking"].ratio == 10.0 and spikes["Spiking"].window_views == 3000
    assert spikes["Brand new"].ratio is None and "NEW" in spikes["Brand new"].label


# ------------------------------------------------------------ trends
RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0"><channel>
<item><title>heat pump grant</title><ht:approx_traffic>20,000+</ht:approx_traffic>
<ht:news_item><ht:news_item_title>Grants open</ht:news_item_title></ht:news_item></item>
<item><title>cricket score</title><ht:approx_traffic>200K+</ht:approx_traffic></item>
<item><title>no traffic</title></item>
</channel></rss>"""


def test_parse_traffic_and_rss():
    assert parse_traffic("200K+") == 200_000 and parse_traffic("2,000+") == 2000
    assert parse_traffic("1M+") == 1_000_000 and parse_traffic(None) is None
    items = parse_rss(RSS)
    assert [(i.title, i.traffic) for i in items] == [
        ("heat pump grant", 20_000), ("cricket score", 200_000), ("no traffic", None)]
    assert items[0].news == ("Grants open",)
    assert parse_rss("<not xml") == []


# ------------------------------------------------------------ seasonal
def yearly(peak_month, as_of, peak=1000.0, base=200.0):
    return {as_of - timedelta(days=i): (peak if (as_of - timedelta(days=i)).month == peak_month
                                         else base) for i in range(400)}


def test_seasonal_peak_coming_up():
    as_of = date(2026, 10, 1)
    s = seasonal_peak(yearly(11, as_of), as_of)       # November peak, 4 weeks away
    assert s is not None and s.peak_month == 11 and s.month_name == "November"
    assert 2 <= s.weeks_until <= 8 and s.ratio == 5.0


def test_seasonal_outside_lead_window_flat_or_short_history():
    as_of = date(2026, 10, 1)
    assert seasonal_peak(yearly(3, as_of), as_of) is None              # March: too far
    assert seasonal_peak({as_of - timedelta(days=i): 300.0 for i in range(400)}, as_of) is None
    short = {as_of - timedelta(days=i): 300.0 for i in range(200)}
    assert seasonal_peak(short, as_of) is None


def test_seasonal_no_lookahead():
    as_of = date(2026, 10, 1)
    data = yearly(11, as_of) | {as_of + timedelta(days=i): 1e9 for i in range(1, 60)}
    assert seasonal_peak(data, as_of) == seasonal_peak(yearly(11, as_of), as_of)


def test_seasonal_topic_gets_actions_even_while_declining():
    v = TopicView(market="en-US", language="en", name="Tax calculator", phase="DECLINING",
                  momentum=0.3, demand=70, money=40, money_path="ADS", money_paths={"ADS": "a"},
                  gap=None, competition=None, confidence=0.6, saturation="UNKNOWN",
                  seasonal={"month_name": "January", "ratio": 4.2, "weeks": 5})
    acts = plan_topic(v)
    assert acts and all("SEASONAL" in a.why[0] for a in acts)
    plain = TopicView(**{**v.__dict__, "seasonal": None})
    assert plan_topic(plain) == []


# ------------------------------------------------------------ scheduler
def test_is_due_daily_and_interval():
    job = Job("daily", at=time(4, 17, tzinfo=UTC))
    morning = datetime(2026, 6, 30, 4, 30, tzinfo=UTC)
    assert is_due(job, None, morning)
    assert not is_due(job, morning, morning + timedelta(minutes=5))      # already ran
    assert not is_due(job, datetime(2026, 6, 29, 4, 20, tzinfo=UTC),
                      datetime(2026, 6, 30, 4, 0, tzinfo=UTC))           # before today's slot
    assert is_due(job, datetime(2026, 6, 29, 4, 20, tzinfo=UTC), morning)
    # after a long downtime, a stale slot doesn't fire hours late
    assert not is_due(job, None, datetime(2026, 6, 30, 3, 0, tzinfo=UTC))
    every = Job("hourly_wiki", every=timedelta(minutes=60))
    assert is_due(every, None, morning) and not is_due(every, morning, morning + timedelta(minutes=30))
    assert is_due(every, morning, morning + timedelta(minutes=61))


def test_jobs_from_config():
    names = [j.name for j in jobs_from_config(make_config())]
    assert names == ["daily", "content", "reports", "plan_message", "export", "week", "midday_wiki", "competitors",
                     "hourly_wiki", "trends", "breakout", "social", "backtest"]
    assert parse_at("06:47") == time(6, 47, tzinfo=UTC)


# ------------------------------------------------------------ notify helpers
def test_chunk_and_escape():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    parts = chunk(text, limit=1000)
    assert all(len(p) <= 1000 for p in parts) and "".join(parts) == text
    assert len(chunk("y" * 2500, limit=1000)) == 3
    assert html_escape("<b>&") == "&lt;b&gt;&amp;"


async def test_scheduler_cancels_running_job_on_stop_and_does_not_mark_it():
    import asyncio

    from ode.scheduler import Scheduler

    marked: list[str] = []
    sched = Scheduler(make_config(), None)  # type: ignore[arg-type]

    async def slow(db):
        await asyncio.sleep(30)
        return {}

    async def mark(db, job, when):
        marked.append(job)

    sched._runner = lambda name: slow          # type: ignore[method-assign]
    sched._mark = mark                         # type: ignore[method-assign]
    run = asyncio.create_task(sched.run_job(None, Job("daily", at=time(4, 17, tzinfo=UTC))))
    await asyncio.sleep(0.05)
    sched.stop()
    assert await asyncio.wait_for(run, timeout=2) is False and marked == []


async def test_scheduler_marks_failed_jobs_so_the_loop_continues():
    from ode.scheduler import Scheduler

    marked: list[str] = []
    sched = Scheduler(make_config(), None)  # type: ignore[arg-type]

    async def boom(db):
        raise RuntimeError("upstream down")

    async def mark(db, job, when):
        marked.append(job)

    sched._runner = lambda name: boom          # type: ignore[method-assign]
    sched._mark = mark                         # type: ignore[method-assign]
    assert await sched.run_job(None, Job("trends", every=timedelta(minutes=60))) is True
    assert marked == ["trends"]
