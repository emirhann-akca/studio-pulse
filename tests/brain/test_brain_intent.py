"""What a question asks for (brain/intent.py), read without the router."""

from __future__ import annotations

import datetime as dt

from studio_pulse.brain import intent

AS_OF = dt.date(2026, 11, 1)
APPS = {
    "mossy_match": "Mossy Match",
    "quartz_merge": "Quartz Merge",
    "pebble_pop": "Pebble Pop",
    "demo_lanternleap": "Lantern Leap",
}
GENRES = {"mossy_match": "puzzle", "pebble_pop": "puzzle", "quartz_merge": "casual"}


def p(q: str) -> intent.Intent:
    return intent.parse(q, apps=APPS, as_of=AS_OF, genres=GENRES)


def test_filters_with_turkish_suffixes():
    x = p("quartz_merge'ün Meta'daki iOS CPI'ı Türkiye'de ne?")
    assert x.apps == ["quartz_merge"]
    assert x.filters() == {"network": ["meta"], "platform": ["IOS"], "country": ["TR"]}
    assert p("Almanyada mossy_match D1").countries == ["DE"]
    assert p("TikTok'a Ekim'de ne kadar harcandı").networks == ["tiktok"]
    assert p("Google Play rating of mossy_match").filters() == {"store": ["google_play"]}
    assert p("frame time of quartz_merge 1.8.0 on low-end phones").filters() == {
        "app_version": ["1.8.0"],
        "device_tier": ["low"],
    }


def test_words_that_only_look_like_filters():
    assert p("Which UA campaign spent the most?").countries == []  # UA = user acquisition
    assert p("user-perceived crash rate of the user base").countries == []
    assert p("metadata of mossy_match").networks == []
    assert p("mossy_match son 30 gün kurulum maliyeti").day_ns == []  # "30 gün" is a period
    assert p("mossy_match D30 elde tutma").day_ns == [30]
    assert p("level 7 fail rate of mossy_match").levels == [7]


def test_apps_typos_unknown_names_and_genres():
    assert p("quartz_merg DAU").apps == ["quartz_merge"] and p("quartz_merg DAU").typo_apps
    assert p("pebblepop gelir").apps == ["pebble_pop"]
    assert p("How many people played Pebble Pop?").apps == ["pebble_pop"]
    assert p("What is the DAU of nebula_dash?").unknown_apps == ["nebula_dash"]
    assert p("How much did Comet Crush earn?").unknown_apps == ["Comet Crush"]
    assert p("Average Android DAU of mossy_match").unknown_apps == []
    assert p("experiment booster_offer_v2 lift").unknown_apps == ["booster_offer_v2"]
    x = intent.parse(
        "lift of booster_offer_v2", apps=APPS, as_of=AS_OF, known_ids={"booster_offer_v2"}
    )
    assert x.unknown_apps == []
    assert p("D7 of our puzzle game").genre_apps == ["mossy_match", "pebble_pop"]


def test_periods():
    y = p("DAU yesterday").period
    assert (y.start, y.end) == (dt.date(2026, 10, 31),) * 2
    assert p("son 14 günde gelir").period.days == 14
    assert p("over the last two weeks").period.days == 14
    oc = p("revenue in October").period
    assert (oc.start, oc.end) == (dt.date(2026, 10, 1), dt.date(2026, 10, 31))
    d = p("15 Ekim 2026'da DAU").period
    assert d.start == d.end == dt.date(2026, 10, 15)
    assert p("since 20 October").period.start == dt.date(2026, 10, 20)
    assert p("revenue this month").period.start == dt.date(2026, 11, 1)
    assert p("pebble_pop'un 20 Kasım 2026'daki DAU'su ne olacak?").future
    assert p("en son ANR oranı").period.latest
    assert p("revenue in may").period is None  # "may" the verb


def test_question_shape():
    assert p("Why did D1 drop in quartz_merge?").why
    assert p("quartz_merge çökme oranı kötüleşiyor mu?").trend
    assert p("Compare D7 of mossy_match and quartz_merge").compare
    assert p("Which network is the cheapest?").by == ["network"]
    assert p("hangi seviyede bırakıyor").by == ["level"]
    assert p("total DAU across all apps").total and p("across all apps").all_apps
    assert p("What's the NPS?").unknown_metrics == ["NPS"]
