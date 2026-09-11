"""Tests for the feed builder.

They pin down the guarantees the feeds depend on: an episode keeps its GUID
for life, the two renditions of one broadcast are a single episode, a feed
never shrinks because a source failed, and an unchanged run rewrites nothing.

Run with: python -m unittest -v
"""

import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import main

UTC = timezone.utc
# The two renditions of one and the same broadcast: note the shared stamp.
MP3 = "https://media.example.com/mp3/abc/2026-09-11_05_06_00_alaunebalado_0000.mp3"
HLS = "https://media.example.com/hls/def/2026-09-11_05_06_00_alaune_0000_master.m3u8"
# A different broadcast, on the previous day.
HLS_PREVIOUS = (
    "https://media.example.com/hls/ghi/2026-09-10_05_06_00_alaune_0000_master.m3u8"
)


def episode(**kwargs) -> main.Episode:
    base = dict(
        title="Un titre",
        published=datetime(2026, 9, 11, 9, 6, tzinfo=UTC),
        duration=1380,
        origin="page",
    )
    base.update(kwargs)
    return main.Episode(**base)


class TitleNormalisationTests(unittest.TestCase):
    def test_strips_the_broadcast_date_prefix_added_by_the_page(self):
        self.assertEqual(
            main.normalize_title("Vendredi 11 septembre 2026 - 9/11 : 25 ans"),
            main.normalize_title("9/11 : 25 ans"),
        )

    def test_accepts_a_colon_separator_and_a_missing_space(self):
        self.assertEqual(
            main.normalize_title("Lundi 31 août 2026: Cinquième jour"),
            main.normalize_title("Cinquième jour"),
        )

    def test_non_breaking_spaces_and_curly_quotes_do_not_split_an_episode(self):
        self.assertEqual(
            main.normalize_title("L’avenue Trump, et nos billets de 20&nbsp;$"),
            main.normalize_title("L'avenue Trump, et nos billets de 20 $"),
        )

    def test_keeps_distinct_titles_distinct(self):
        self.assertNotEqual(
            main.normalize_title("Le bêtisier 2025"),
            main.normalize_title("Le bêtisier 2024"),
        )


class BroadcastStampTests(unittest.TestCase):
    def test_both_renditions_of_one_broadcast_share_a_stamp(self):
        self.assertEqual(main.broadcast_stamp(MP3), main.broadcast_stamp(HLS))
        self.assertEqual(main.broadcast_stamp(MP3), "2026-09-11_05_06_00")

    def test_absent_stamp_is_empty(self):
        self.assertEqual(main.broadcast_stamp("https://example.com/audio.mp3"), "")


class EnclosureTests(unittest.TestCase):
    def test_hls_playlists_are_not_progressive(self):
        self.assertFalse(main.is_progressive(HLS))
        self.assertTrue(main.is_progressive(MP3))

    def test_declared_mime_is_honoured(self):
        self.assertFalse(main.is_progressive("https://x/a", "application/x-mpegURL"))


class MergeTests(unittest.TestCase):
    def test_the_mp3_replaces_the_hls_url_but_the_guid_survives(self):
        index = main.EpisodeIndex()
        first = index.add(
            episode(url=HLS, mime="application/x-mpegURL", guid=f"{HLS}?v=2")
        )
        index.add(
            episode(
                title="9/11 : 25 ans",
                url=MP3,
                mime="audio/mpeg",
                length=33_094_844,
                origin="rss",
            )
        )
        self.assertEqual(len(index), 1, "the two renditions must be one episode")
        self.assertEqual(first.url, MP3)
        self.assertEqual(first.guid, f"{HLS}?v=2")
        self.assertEqual(first.length, 33_094_844)

    def test_an_hls_url_never_replaces_an_mp3(self):
        index = main.EpisodeIndex()
        kept = index.add(episode(url=MP3, mime="audio/mpeg", origin="rss"))
        index.add(episode(url=HLS, mime="application/x-mpegURL", origin="page"))
        self.assertEqual(kept.url, MP3)

    def test_the_page_title_and_the_rss_title_are_one_episode(self):
        index = main.EpisodeIndex()
        index.add(episode(title="Jeudi 10 septembre 2026 : L’entrevue", url=HLS))
        index.add(episode(title="L'entrevue", url=MP3, origin="rss"))
        self.assertEqual(len(index), 1)

    def test_the_broadcast_instant_matches_before_any_url_is_known(self):
        index = main.EpisodeIndex()
        known = index.add(episode(title="Titre du flux", url=MP3, origin="rss"))
        # What the page gives us: no URL yet, and an editorially different title.
        found = index.find(episode(title="Un tout autre titre", url=""))
        self.assertIs(found, known, "a known episode must not be resolved again")

    def test_same_title_one_year_apart_stays_two_episodes(self):
        index = main.EpisodeIndex()
        index.add(episode(title="Le tournoi : la finale", url=MP3, origin="rss"))
        index.add(
            episode(
                title="Le tournoi : la finale",
                published=datetime(2025, 9, 11, 9, 6, tzinfo=UTC),
                url="https://media.example.com/mp3/x/2025-09-11_05_06_00_a_0000.mp3",
                origin="rss",
            )
        )
        self.assertEqual(len(index), 2)

    def test_episodes_come_out_newest_first(self):
        index = main.EpisodeIndex()
        older = datetime(2026, 9, 1, tzinfo=UTC)
        for offset in (0, 5, 2):
            index.add(
                episode(
                    title=f"Episode {offset}",
                    published=older + timedelta(days=offset),
                    url=f"https://media.example.com/mp3/{offset}/a.mp3",
                )
            )
        dates = [ep.published for ep in index.sorted_episodes()]
        self.assertEqual(dates, sorted(dates, reverse=True))


class DurationTests(unittest.TestCase):
    def test_parses_seconds_and_both_clock_shapes(self):
        self.assertEqual(main.parse_duration(1380), 1380)
        self.assertEqual(main.parse_duration("23:00"), 1380)
        self.assertEqual(main.parse_duration("00:22:58"), 1378)
        self.assertIsNone(main.parse_duration(""))
        self.assertIsNone(main.parse_duration("pas une durée"))

    def test_always_written_as_hours_minutes_seconds(self):
        self.assertEqual(main.format_duration(1380), "00:23:00")
        self.assertEqual(main.format_duration(None), "")


class DescriptionTests(unittest.TestCase):
    def test_an_escaped_description_is_unescaped_once(self):
        self.assertEqual(main.clean_text("&lt;p&gt;Bonjour&lt;/p&gt;"), "<p>Bonjour</p>")

    def test_raw_html_is_left_alone(self):
        self.assertEqual(main.clean_text("<p>Bonjour</p>"), "<p>Bonjour</p>")


class FeedRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.show = main.SHOWS[0]
        self.meta = main.ChannelMeta(
            title="Une émission",
            description="Une description",
            image="https://images.example.com/a.jpg",
        )
        self.episodes = [
            episode(title="Deuxième", url=MP3, length=1234, guid="guid-2"),
            episode(
                title="Premier",
                published=datetime(2026, 9, 10, 9, 6, tzinfo=UTC),
                url=HLS_PREVIOUS,
                mime="application/x-mpegURL",
                guid="guid-1",
            ),
        ]

    def test_the_generated_feed_is_valid_xml_with_every_episode(self):
        xml = main.build_feed_xml(self.show, self.meta, self.episodes)
        channel = ET.fromstring(xml).find("channel")
        self.assertEqual(len(channel.findall("item")), 2)
        self.assertEqual(channel.findtext("title"), "Une émission")

    def test_building_twice_gives_the_same_bytes(self):
        # Nothing derived from "now" may end up in the file, otherwise every
        # scheduled run would produce a commit.
        first = main.build_feed_xml(self.show, self.meta, self.episodes)
        second = main.build_feed_xml(self.show, self.meta, self.episodes)
        self.assertEqual(first, second)

    def test_reading_back_preserves_guids_urls_and_durations(self):
        xml = main.build_feed_xml(self.show, self.meta, self.episodes)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, self.show.filename)
            self.assertTrue(main.write_if_changed(path, xml))
            self.assertFalse(
                main.write_if_changed(path, xml),
                "identical content must not be rewritten",
            )
            meta, episodes = main.read_existing_feed(path)

        self.assertEqual(meta.title, "Une émission")
        self.assertEqual(meta.image, "https://images.example.com/a.jpg")
        self.assertEqual({ep.guid for ep in episodes}, {"guid-1", "guid-2"})
        self.assertEqual({ep.url for ep in episodes}, {MP3, HLS_PREVIOUS})
        self.assertEqual({ep.duration for ep in episodes}, {1380})

    def test_a_reread_feed_merges_with_a_fresh_source_without_duplicating(self):
        xml = main.build_feed_xml(self.show, self.meta, self.episodes)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, self.show.filename)
            main.write_if_changed(path, xml)
            _, stored = main.read_existing_feed(path)

        index = main.EpisodeIndex()
        for stored_episode in stored:
            index.add(stored_episode)
        # The same two episodes, seen again on the next run.
        for fresh in self.episodes:
            index.add(
                episode(
                    title=fresh.title,
                    published=fresh.published,
                    url=fresh.url,
                    mime=fresh.mime,
                )
            )
        self.assertEqual(len(index), 2)
        self.assertEqual({ep.guid for ep in index.sorted_episodes()}, {"guid-1", "guid-2"})

    def test_a_damaged_feed_is_reported_as_empty_rather_than_raising(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "feed_1.xml")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("<rss><channel><item>")
            with self.assertLogs(main.LOG, level="WARNING"):
                meta, episodes = main.read_existing_feed(path)
        self.assertEqual(episodes, [])
        self.assertEqual(meta.title, "")

    def test_the_placeholder_length_is_not_carried_forward(self):
        xml = (
            '<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>'
            "<title>T</title><item><title>A</title>"
            f'<enclosure url="{MP3}" length="100000000" type="audio/mpeg"/>'
            "</item></channel></rss>"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "feed_1.xml")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(xml)
            _, episodes = main.read_existing_feed(path)
        self.assertEqual(episodes[0].length, 0)


def eastern(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=main.EASTERN)


class ScheduleTests(unittest.TestCase):
    # 2026-09-11 is a Friday, 2026-09-12 a Saturday.
    SHOW = main.Show(
        1, "test", "Test", main.Schedule((main.FRIDAY,), 19, 0, window_minutes=120)
    )

    def test_not_watched_before_the_expected_time(self):
        self.assertFalse(
            main.awaiting_episode(self.SHOW, None, eastern(2026, 9, 11, 18, 30))
        )

    def test_watched_once_the_episode_is_due_and_missing(self):
        self.assertTrue(
            main.awaiting_episode(self.SHOW, None, eastern(2026, 9, 11, 19, 10))
        )

    def test_not_watched_once_todays_episode_is_in_the_feed(self):
        newest = eastern(2026, 9, 11, 19, 6)
        self.assertFalse(
            main.awaiting_episode(self.SHOW, newest, eastern(2026, 9, 11, 19, 10))
        )

    def test_yesterdays_episode_does_not_satisfy_today(self):
        newest = eastern(2026, 9, 4, 19, 6)
        self.assertTrue(
            main.awaiting_episode(self.SHOW, newest, eastern(2026, 9, 11, 19, 10))
        )

    def test_the_window_closes_so_a_skipped_day_is_not_watched_forever(self):
        self.assertFalse(
            main.awaiting_episode(self.SHOW, None, eastern(2026, 9, 11, 21, 30))
        )

    def test_a_show_is_not_watched_on_a_day_it_never_publishes(self):
        self.assertFalse(
            main.awaiting_episode(self.SHOW, None, eastern(2026, 9, 12, 19, 10))
        )

    def test_a_show_without_a_schedule_is_never_watched(self):
        show = main.Show(2, "irregular", "Irregular")
        self.assertFalse(
            main.awaiting_episode(show, None, eastern(2026, 9, 11, 19, 10))
        )

    def test_the_schedule_follows_eastern_time_across_daylight_saving(self):
        # 2026-01-09 is a Friday on EST, 2026-09-11 a Friday on EDT. The same
        # local 19:00 is a different UTC hour, and both must be recognised.
        for moment in (eastern(2026, 1, 9, 19, 10), eastern(2026, 9, 11, 19, 10)):
            self.assertTrue(
                main.awaiting_episode(self.SHOW, None, moment.astimezone(UTC))
            )

    def test_one_missing_publication_is_not_yet_a_warning(self):
        newest = eastern(2026, 9, 4, 19, 6)
        self.assertEqual(
            main.missed_publications(self.SHOW, newest, eastern(2026, 9, 11, 22, 0)), 1
        )

    def test_several_missing_publications_are_counted(self):
        newest = eastern(2026, 8, 14, 19, 6)
        self.assertGreaterEqual(
            main.missed_publications(self.SHOW, newest, eastern(2026, 9, 11, 22, 0)), 2
        )

    def test_an_up_to_date_feed_reports_nothing_missing(self):
        newest = eastern(2026, 9, 11, 19, 6)
        self.assertEqual(
            main.missed_publications(self.SHOW, newest, eastern(2026, 9, 11, 22, 0)), 0
        )

    def test_every_configured_schedule_is_well_formed(self):
        for show in main.SHOWS:
            if show.schedule is None:
                continue
            with self.subTest(show=show.slug):
                self.assertTrue(show.schedule.weekdays)
                self.assertTrue(0 <= show.schedule.hour <= 23)
                self.assertTrue(0 <= show.schedule.minute <= 59)
                self.assertTrue(0 < show.schedule.window_minutes <= 24 * 60)
                self.assertTrue(all(0 <= d <= 6 for d in show.schedule.weekdays))


class WatchLoopTests(unittest.TestCase):
    """The loop must never burn its window when there is nothing to wait for."""

    def setUp(self):
        self.slept = []
        self.real_sleep = main.time.sleep
        main.time.sleep = self.slept.append
        self.addCleanup(setattr, main.time, "sleep", self.real_sleep)

    def test_returns_at_once_when_no_show_is_due(self):
        show = main.Show(1, "irregular", "Irregular")
        results = {"irregular": main.ShowResult(show=show)}
        with self.assertLogs(main.LOG, level="INFO"):
            main.watch_for_episodes([show], results, ".", minutes=45, poll_seconds=180)
        self.assertEqual(self.slept, [], "it must not sleep with nothing pending")

    @staticmethod
    def _due_show():
        return main.Show(
            1, "test", "Test", main.Schedule((main.FRIDAY,), 19, 0, window_minutes=120)
        )

    def _stub_process(self, episode_arrives_after=None):
        """Replace the network pass with a counter, optionally simulating the
        episode showing up after a given number of checks."""
        calls = []

        def fake_process(target, out_dir, dry_run=False):
            calls.append(target.slug)
            newest = None
            if (
                episode_arrives_after is not None
                and len(calls) >= episode_arrives_after
            ):
                newest = eastern(2026, 9, 11, 19, 6)
            return main.ShowResult(show=target, newest=newest, changed=newest is not None)

        original = main.process_show
        main.process_show = fake_process
        self.addCleanup(setattr, main, "process_show", original)
        return calls

    def test_gives_up_when_the_window_closes(self):
        show = self._due_show()
        results = {"test": main.ShowResult(show=show)}
        calls = self._stub_process()
        # Friday 19:10 ET: the episode is due and missing, but no time is left.
        with self.assertLogs(main.LOG, level="INFO") as logs:
            main.watch_for_episodes(
                [show],
                results,
                ".",
                minutes=0,
                poll_seconds=180,
                now_provider=lambda: eastern(2026, 9, 11, 19, 10).astimezone(UTC),
            )
        self.assertEqual(calls, [], "an exhausted window must not re-check")
        self.assertTrue(any("Watch window closed" in line for line in logs.output))

    def test_stops_as_soon_as_the_episode_arrives(self):
        show = self._due_show()
        results = {"test": main.ShowResult(show=show)}
        calls = self._stub_process(episode_arrives_after=2)
        with self.assertLogs(main.LOG, level="INFO"):
            main.watch_for_episodes(
                [show],
                results,
                ".",
                minutes=45,
                poll_seconds=30,
                now_provider=lambda: eastern(2026, 9, 11, 19, 10).astimezone(UTC),
            )
        self.assertEqual(len(calls), 2, "it must stop at the first sighting")
        self.assertTrue(results["test"].changed)
        self.assertEqual(self.slept, [30, 30])


class TitleMergeGuardTests(unittest.TestCase):
    def test_two_podcast_episodes_sharing_a_title_stay_separate(self):
        # Upstream never lists one episode twice, so a repeated title there is
        # a genuine second episode even a day apart.
        index = main.EpisodeIndex()
        index.add(episode(title="Le match", url=MP3, origin="rss"))
        index.add(
            episode(
                title="Le match",
                published=datetime(2026, 9, 12, 9, 6, tzinfo=UTC),
                url="https://media.example.com/mp3/z/2026-09-12_05_06_00_a_0000.mp3",
                origin="rss",
            )
        )
        self.assertEqual(len(index), 2)

    def test_a_page_episode_still_merges_into_its_podcast_twin(self):
        index = main.EpisodeIndex()
        kept = index.add(episode(title="Le match", url=MP3, origin="rss"))
        index.add(episode(title="Le match", url="", origin="page"))
        self.assertEqual(len(index), 1)
        self.assertEqual(kept.url, MP3)


class ShowConfigurationTests(unittest.TestCase):
    def test_every_show_has_a_unique_id_slug_and_feed_name(self):
        self.assertEqual(len({show.id for show in main.SHOWS}), len(main.SHOWS))
        self.assertEqual(len({show.slug for show in main.SHOWS}), len(main.SHOWS))
        self.assertEqual(len({show.filename for show in main.SHOWS}), len(main.SHOWS))

    def test_shows_can_be_selected_by_slug_or_id(self):
        self.assertEqual(main.select_shows("une"), [main.SHOWS_BY_ID[302]])
        self.assertEqual(main.select_shows("302"), [main.SHOWS_BY_ID[302]])
        self.assertEqual(len(main.select_shows("")), len(main.SHOWS))
        with self.assertRaises(SystemExit):
            main.select_shows("inconnue")


if __name__ == "__main__":
    unittest.main()
