-- What reviews talk about: keyword topics over the 30 days ending at the app's
-- latest review, against the 30 days before. A review can match several topics.
-- The lexicon is English plus common Turkish stems (lower-cased title + text;
-- English words with word boundaries, Turkish stems as substrings because the
-- regex engine's \b is ASCII-only). Edit the VALUES list to change it.
WITH topics(topic, label, pattern) AS (
    VALUES
    ('crashes_bugs', 'Crashes and bugs',
     '\b(crash\w*|bugs?|bugg\w*|freez\w*|stuck|glitch\w*|errors?|black screen|closes?|not (open|load)\w*)\b|çök|donuyor|donma|hata|kapan[ıi]yor|açılm[ıi]yor'),
    ('ads', 'Ads',
     '\b(ads?|adverts?|advertis\w*|commercials?)\b|reklam'),
    ('performance', 'Speed, lag, battery',
     '\b(slow|lag\w*|battery|overheat\w*|heats? up|fps|loading)\b|kas[ıi]yor|yava[şs]|şarj|ısın'),
    ('pricing', 'Prices and payments',
     '\b(price\w*|expensive|pay\w*|money|subscription\w*|refunds?|cost\w*|charged?|trial)\b|pahal[ıi]|abonelik|ücret|para'),
    ('difficulty', 'Difficulty',
     '\b(hard|difficult|impossible|unfair|too many levels|stuck on level)\b|zor|geçemiyorum'),
    ('account_progress', 'Login and lost progress',
     '\b(login|log in|account|sync\w*|lost (my )?progress|progress (is )?lost|save)\b|hesap|ilerleme'),
    ('praise', 'Praise',
     '\b(love|fun|great|addict\w*|relax\w*|best|awesome|amazing)\b|harika|süper|bayıld|güzel')
),
last AS (SELECT app_id, MAX(date) AS d FROM stg_store_reviews GROUP BY 1),
r AS (
    SELECT s.app_id, s.rating,
           lower(COALESCE(s.title, '') || ' ' || COALESCE(s.body, '')) AS txt,
           CASE WHEN s.date > l.d - 30 THEN 'last' WHEN s.date > l.d - 60 THEN 'prior' END AS period
    FROM stg_store_reviews s JOIN last l USING (app_id)
),
tot AS (SELECT app_id, period, COUNT(*) AS n FROM r WHERE period IS NOT NULL GROUP BY 1, 2),
hits AS (
    SELECT r.app_id, r.period, t.topic, t.label, COUNT(*) AS reviews, AVG(r.rating) AS avg_rating,
           AVG(CASE WHEN r.rating <= 2 THEN 1.0 ELSE 0.0 END) AS low_star_share
    FROM r JOIN topics t ON regexp_matches(r.txt, t.pattern)
    WHERE r.period IS NOT NULL
    GROUP BY 1, 2, 3, 4
)
SELECT
    h.app_id, h.topic, MAX(h.label) AS label,
    MAX(CASE WHEN h.period = 'last' THEN h.reviews END) AS reviews_30d,
    MAX(CASE WHEN h.period = 'last' THEN CAST(h.reviews AS DOUBLE) / t.n END) AS share_30d,
    MAX(CASE WHEN h.period = 'last' THEN h.avg_rating END) AS avg_rating_30d,
    MAX(CASE WHEN h.period = 'last' THEN h.low_star_share END) AS low_star_share_30d,
    MAX(CASE WHEN h.period = 'prior' THEN h.reviews END) AS reviews_prior_30d,
    MAX(CASE WHEN h.period = 'prior' THEN CAST(h.reviews AS DOUBLE) / t.n END) AS share_prior_30d
FROM hits h JOIN tot t ON t.app_id = h.app_id AND t.period = h.period
GROUP BY h.app_id, h.topic
ORDER BY h.app_id, reviews_30d DESC NULLS LAST, h.topic
