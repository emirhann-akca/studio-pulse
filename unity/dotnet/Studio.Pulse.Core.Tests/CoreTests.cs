using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;

namespace Studio.Pulse.Tests
{
    public static class QueueAndDeliveryTests
    {
        static void Play(PulseCore core, int levels)
        {
            for (int i = 1; i <= levels; i++)
            {
                core.LevelStart(i);
                core.LevelFail(i, "out_of_moves", 0.25, 0.5, 0.5);
                core.LevelStart(i);
                core.LevelComplete(i, 100 * i, 3);
            }
        }

        [PulseTest]
        public static void Kill_mid_flush_loses_nothing_and_the_resend_is_deduped()
        {
            using (var rig = new Rig())
            {
                var hung = new List<Sent>();
                rig.Transport.Responder = s => { hung.Add(s); return null; }; // request out, no answer: process dies
                PulseCore a = rig.Start();
                Play(a, 6);                 // 2 (install, session) + 24 events
                a.Flush();
                Check.Equal(1, hung.Count, "one request in flight when the process is killed");
                a.LevelStart(7);            // recorded after the flush started, also before the kill
                int expected = 2 + 24 + 1;
                // The in-flight request did reach the server; only its answer was lost.
                rig.Collector.Receive(hung[0]);

                rig.Transport.Responder = FakeTransport.Ok;
                PulseCore b = rig.Restart(); // abandons `a` without quit or close, like a kill
                b.Flush();
                for (int i = 0; i < 20; i++) rig.Step(100);
                var seqs = rig.Collector.Seqs(b.GetInstallId());
                // second launch adds session_start + pulse_exit (unclean)
                Check.Equal(expected + 2, seqs.Count, "every event arrived once");
                Check.True(seqs.SequenceEqual(Enumerable.Range(1, expected + 2).Select(x => (long)x)), "seq 1..n without gaps");
                Check.True(rig.Collector.Duplicates >= 26, "the in-flight batch was re-sent and deduped (" + rig.Collector.Duplicates + ")");
                Check.True(rig.Collector.Names().Contains("pulse_exit"), "previous process died in the foreground -> pulse_exit");
                Check.Equal(0, b.GetStats().Queued, "queue empty");
                Check.Equal(0, rig.QueueFiles().Count(f => f.EndsWith(".seg")), "segment files deleted after 2xx");
            }
        }

        [PulseTest]
        public static void Torn_last_line_is_repaired_on_load()
        {
            using (var rig = new Rig())
            {
                rig.Transport.Responder = s => null;
                PulseCore a = rig.Start();
                Play(a, 1);
                string seg = rig.QueueFiles().First(f => f.EndsWith(".seg"));
                File.AppendAllText(seg, "{\"id\":\"0190f0e2-7c1a-7cc3-9b1e-0000000"); // a write cut by power loss
                rig.Transport.Responder = FakeTransport.Ok;
                PulseCore b = rig.Restart();
                b.Flush();
                rig.Drain();
                Check.Equal(0, rig.Collector.Rows.Count(r => ((string)r["id"]).Length != 36), "no broken event sent");
                Check.True(rig.Collector.Rows.Count >= 6, "the complete events were sent");
            }
        }

        [PulseTest]
        public static void Offline_for_24_hours_then_everything_is_replayed_in_order()
        {
            using (var rig = new Rig())
            {
                bool online = false;
                rig.Transport.Responder = s => online ? FakeTransport.Ok(s) : FakeTransport.Status(0);
                PulseCore core = rig.Start();
                long t0 = rig.Clock.Now;
                // 24 sessions: 10 minutes of play (heartbeats while idle), 50 minutes in the background
                for (int h = 0; h < 24; h++)
                {
                    if (h > 0) core.OnForeground();
                    for (int m = 0; m < 10; m++)
                    {
                        core.LevelStart(h * 10 + m);
                        rig.Step(30_000, 2);
                        core.LevelComplete(h * 10 + m);
                    }
                    core.OnBackground();
                    rig.Step(60_000, 50);
                }
                Check.True(rig.Clock.Now - t0 >= 24 * 3600_000L, "a day passed");
                int attempts = rig.Transport.Batches.Count;
                Check.True(attempts > 500, "kept retrying with backoff while offline (" + attempts + ")");
                var stats = core.GetStats();
                Check.Equal(0L, stats.Sent, "nothing sent offline");
                int queued = stats.Queued;
                online = true;
                core.OnForeground();
                for (int i = 0; i < 200 && core.GetStats().Queued > 0; i++) rig.Step(1_000);
                var seqs = rig.Collector.Seqs();
                Check.Equal(0, core.GetStats().Queued, "queue drained");
                Check.True(seqs.SequenceEqual(Enumerable.Range(1, seqs.Count).Select(x => (long)x)), "no gaps after replay");
                Check.True(seqs.Count >= queued, "everything queued arrived");
                long oldest = rig.Collector.Rows.Min(r => (long)r["ts"]);
                Check.Equal(t0, oldest, "the first event is a day old and still delivered");
                Check.Equal(25, rig.Collector.Names().Count(n => n == "pulse_session_start"), "one session per hour (30-min timeout) + the one coming back online");
                Check.True(rig.Transport.Batches.Last().Events.Count <= 100, "batches stay within MaxBatchEvents");
            }
        }

        [PulseTest]
        public static void Status_413_halves_the_batch_until_it_fits()
        {
            using (var rig = new Rig())
            {
                rig.Transport.Responder = s => s.Events.Count > 10 ? FakeTransport.Status(413, -1, "{\"ok\":false,\"error\":\"too_many_events\"}") : FakeTransport.Ok(s);
                rig.Options.FlushEvents = 1000;
                PulseCore core = rig.Start();
                Play(core, 10); // 42 events
                core.Flush();
                for (int i = 0; i < 30; i++) rig.Drain();
                var sizes = rig.Transport.Batches.Select(b => b.Events.Count).ToList();
                Check.Equal(42, sizes[0], "first try sends everything");
                Check.Equal(21, sizes[1], "413 -> half");
                Check.Equal(10, sizes[2], "413 -> half again");
                Check.True(sizes.Skip(2).All(n => n <= 10), "stays small afterwards");
                Check.Equal(42, rig.Collector.Seqs().Count, "nothing lost");
            }
        }

        [PulseTest]
        public static void Status_400_drops_the_batch_and_keeps_the_reason()
        {
            using (var rig = new Rig())
            {
                int n = 0;
                rig.Transport.Responder = s => ++n == 1 ? FakeTransport.Status(400, -1, "{\"ok\":false,\"error\":\"ctx.uuid:install_id\"}") : FakeTransport.Ok(s);
                PulseCore core = rig.Start();
                Play(core, 1);
                core.Flush();
                rig.Drain();
                var st = core.GetStats();
                Check.Equal(6L, st.Rejected, "the whole batch counted as rejected");
                Check.True(st.LastError.Contains("ctx.uuid:install_id"), "reason kept: " + st.LastError);
                Check.Equal(0, st.Queued, "dropped, not retried");
                core.LevelStart(2);
                core.Flush();
                rig.Drain();
                Check.Equal(1, rig.Collector.Rows.Count, "later batches still go out");
            }
        }

        [PulseTest]
        public static void Status_401_disables_sending_until_the_next_launch()
        {
            using (var rig = new Rig())
            {
                rig.Transport.Responder = s => FakeTransport.Status(401, -1, "{\"ok\":false,\"error\":\"unknown_write_key\"}");
                PulseCore core = rig.Start();
                Play(core, 1);
                core.Flush();
                rig.Drain();
                Check.True(core.GetStats().Disabled, "disabled after 401");
                int before = rig.Transport.Requests.Count;
                Play(core, 10);
                core.Flush();
                rig.Step(120_000, 30);
                Check.Equal(before, rig.Transport.Requests.Count, "no request after 401");
                Check.True(core.GetStats().Queued > 40, "events are kept on disk");
                rig.Transport.Responder = FakeTransport.Ok;
                PulseCore next = rig.Restart();
                next.Flush();
                for (int i = 0; i < 10; i++) rig.Drain();
                Check.True(rig.Collector.Rows.Count > 40, "the next launch sends the kept events");
                Check.True(!next.GetStats().Disabled, "re-enabled on the next launch");
            }
        }

        [PulseTest]
        public static void Status_429_and_5xx_back_off_exponentially_with_jitter()
        {
            using (var rig = new Rig())
            {
                int[] codes = { 429, 503, 500, 0, 502, 503, 503, 503, 503 };
                int k = 0;
                rig.Transport.Responder = s => k < codes.Length ? FakeTransport.Status(codes[k++]) : FakeTransport.Ok(s);
                PulseCore core = rig.Start();
                Play(core, 1);
                core.Flush();
                long[] baseMs = { 2000, 4000, 8000, 16000, 32000, 60000, 60000, 60000, 60000 };
                for (int i = 0; i < codes.Length; i++)
                {
                    long sentAt = rig.Clock.Mono;
                    rig.Step(1); // the answer is applied on the next tick
                    long wait = core.RetryAtMs - (sentAt + 1);
                    Check.InRange(wait, baseMs[i] + 1000 - 1, baseMs[i] + 5000, "retry " + (i + 1) + " after " + codes[i]);
                    int sentBefore = rig.Transport.Requests.Count;
                    rig.Clock.Advance(wait - 2);
                    core.Tick();
                    Check.Equal(sentBefore, rig.Transport.Requests.Count, "no retry before the backoff ends");
                    rig.Clock.Advance(2);
                    core.Tick();
                    Check.Equal(sentBefore + 1, rig.Transport.Requests.Count, "retry when the backoff ends");
                }
                rig.Drain();
                var seqs = rig.Collector.Seqs();
                Check.True(seqs.Count >= 6 && seqs.SequenceEqual(Enumerable.Range(1, seqs.Count).Select(x => (long)x)), "delivered after the server recovered");
            }
        }

        [PulseTest]
        public static void Retry_after_header_is_honoured()
        {
            using (var rig = new Rig())
            {
                int k = 0;
                rig.Transport.Responder = s => k++ == 0 ? FakeTransport.Status(429, 30) : FakeTransport.Ok(s);
                PulseCore core = rig.Start();
                core.Flush();
                long sent = rig.Clock.Mono;
                rig.Step(1);
                Check.True(core.RetryAtMs - sent >= 30_000, "waits at least Retry-After");
            }
        }

        [PulseTest]
        public static void Queue_cap_drops_the_oldest_segments_and_counts_them()
        {
            using (var rig = new Rig())
            {
                rig.Transport.Responder = s => FakeTransport.Status(0);
                rig.Options.QueueMaxEvents = 50;
                rig.Options.SegmentMaxEvents = 10;
                rig.Options.FlushEvents = 100000;
                PulseCore core = rig.Start();
                for (int i = 0; i < 120; i++) core.Screen("s" + i);
                var st = core.GetStats();
                Check.True(st.Queued <= 50, "queue within the cap (" + st.Queued + ")");
                Check.True(st.Dropped >= 70, "dropped counted (" + st.Dropped + ")");
                Check.Equal(122L, st.Queued + st.Dropped, "queued + dropped = recorded");
                rig.Transport.Responder = FakeTransport.Ok;
                core.Flush();
                for (int i = 0; i < 10; i++) rig.Drain();
                var seqs = rig.Collector.Seqs();
                Check.Equal(122L, seqs.Last(), "the newest events survive");
                Check.True(seqs.First() > 1, "the oldest were dropped");
                PulseCore again = rig.Restart();
                Check.True(again.GetStats().Dropped >= 70, "the dropped counter is persisted");
            }
        }

        [PulseTest]
        public static void Gzip_only_above_1_kb()
        {
            using (var rig = new Rig())
            {
                rig.Options.FlushEvents = 1000;
                PulseCore core = rig.Start();
                core.Flush();
                rig.Drain();
                Check.True(!rig.Transport.Batches[0].Gzip, "two events: plain JSON");
                Play(core, 5);
                core.Flush();
                rig.Drain();
                Check.True(rig.Transport.Batches[1].Gzip, "bigger batch: gzip");
                Check.True(rig.Transport.Batches[1].Raw.Length < rig.Transport.Batches[1].Json.Length / 2, "and it is smaller");
            }
        }

        [PulseTest]
        public static void Flush_triggers_20_events_30_seconds_and_background()
        {
            using (var rig = new Rig())
            {
                PulseCore core = rig.Start();
                rig.Step(1000);
                Check.Equal(0, rig.Transport.Batches.Count, "2 events: wait");
                for (int i = 0; i < 18; i++) core.Screen("s");
                rig.Step(1);
                Check.Equal(1, rig.Transport.Batches.Count, "20 events: flush");
                core.Screen("a");
                rig.Step(10_000);
                Check.Equal(1, rig.Transport.Batches.Count, "1 event, 10 s: wait");
                rig.Step(20_000);
                Check.Equal(2, rig.Transport.Batches.Count, "30 s: flush");
                core.Screen("b");
                core.OnBackground();
                Check.Equal(3, rig.Transport.Batches.Count, "background: flush at once");
                Check.True(rig.Transport.Batches[2].Json.Contains("pulse_background"), "with the background event");
            }
        }
    }

    public static class SessionTests
    {
        [PulseTest]
        public static void Session_rolls_over_after_30_minutes_in_the_background()
        {
            using (var rig = new Rig())
            {
                PulseCore core = rig.Start();
                string sid1 = core.SessionId;
                core.LevelStart(5);
                rig.Step(20_000);
                core.OnBackground();
                rig.Step(29 * 60_000);
                core.OnForeground();
                Check.Equal(sid1, core.SessionId, "29 minutes away: same session");
                rig.Step(10_000);
                core.OnBackground();
                rig.Step(31 * 60_000);
                core.OnForeground();
                Check.True(core.SessionId != sid1, "31 minutes away: new session");
                core.Flush();
                rig.Drain();
                var rows = rig.Collector.Rows;
                var starts = rows.Where(r => (string)r["name"] == "pulse_session_start").ToList();
                Check.Equal(2, starts.Count, "two sessions");
                var p = (Dictionary<string, object>)starts[1]["p"];
                Check.Equal(false, (bool)p["cold"], "warm start");
                Check.Equal(31L * 60, (long)p["since_last_s"], "since_last_s");
                Check.Equal(2L, (long)starts[1]["snum"], "snum 2");
                var quit = rows.Single(r => (string)r["name"] == "level_quit");
                var qp = (Dictionary<string, object>)quit["p"];
                Check.Equal("background_timeout", (string)qp["reason"], "open level closed by the timeout");
                Check.Equal(30_000L, (long)qp["duration_ms"], "level time counts foreground only");
                Check.Equal(sid1, (string)quit["sid"], "level_quit belongs to the old session");
                Check.Equal(1, rows.Count(r => (string)r["name"] == "pulse_foreground"), "one plain foreground");
                var bg = rows.Where(r => (string)r["name"] == "pulse_background").Select(r => (long)((Dictionary<string, object>)r["p"])["fg_ms"]).ToList();
                Check.True(bg.SequenceEqual(new long[] { 20_000, 30_000 }), "fg_ms counts foreground time: " + string.Join(",", bg));
            }
        }

        [PulseTest]
        public static void Session_never_spans_more_than_24_hours()
        {
            using (var rig = new Rig())
            {
                PulseCore core = rig.Start();
                string sid = core.SessionId;
                rig.Step(3600_000, 25);
                Check.True(core.SessionId != sid, "a new session after 24 h");
            }
        }

        [PulseTest]
        public static void Heartbeat_only_when_nothing_else_was_sent_for_a_minute()
        {
            using (var rig = new Rig())
            {
                PulseCore core = rig.Start();
                for (int i = 0; i < 10; i++) { rig.Step(30_000); core.Screen("x"); }
                rig.Step(30_000, 4);
                core.OnBackground();
                rig.Step(300_000);
                core.Flush();
                rig.Drain();
                var hb = rig.Collector.Rows.Where(r => (string)r["name"] == "pulse_heartbeat").ToList();
                Check.Equal(2, hb.Count, "busy minutes: none; 2 idle minutes: 2; background: none");
            }
        }

        [PulseTest]
        public static void Seq_is_monotonic_across_restarts()
        {
            using (var rig = new Rig())
            {
                int n = 0;
                rig.Transport.Responder = s => (++n % 3 == 0) ? FakeTransport.Status(503) : FakeTransport.Ok(s);
                for (int launch = 0; launch < 6; launch++)
                {
                    PulseCore core = rig.Restart();
                    for (int i = 0; i < 25; i++) core.Screen("s" + i);
                    rig.Step(5_000, 3);
                    if (launch % 2 == 0) core.OnBackground(); // odd launches are killed in the foreground
                }
                rig.Transport.Responder = FakeTransport.Ok;
                PulseCore last = rig.Restart();
                last.Flush();
                for (int i = 0; i < 100 && last.GetStats().Queued > 0; i++) rig.Step(61_000);
                var seqs = rig.Collector.Seqs();
                Check.True(seqs.SequenceEqual(Enumerable.Range(1, seqs.Count).Select(x => (long)x)), "1..n, no gap, no repeat (" + seqs.Count + ")");
                Check.True(seqs.Count > 150, "all launches delivered");
                Check.Equal(1, rig.Collector.Rows.Select(r => (string)r["install_id"]).Distinct().Count(), "one install id");
            }
        }

        [PulseTest]
        public static void Install_update_and_exit_reasons()
        {
            using (var rig = new Rig())
            {
                PulseCore a = rig.Start();
                a.OnQuit(); // clean exit
                rig.Device.AppVersion = "1.5.0";
                PulseCore b = rig.Restart(); // update, after a clean exit: no pulse_exit
                b.LevelStart(1);
                // b is killed in the foreground; the OS says native crash
                rig.PreviousExit = () => new PulseExitInfo { Reason = PulseContract.ExitReason.CrashNative, TimestampMs = rig.Clock.Now, RssKb = 512000 };
                rig.Step(1000);
                PulseCore c = rig.Restart();
                c.Flush();
                rig.Drain();
                var names = rig.Collector.Names();
                Check.Equal(1, names.Count(x => x == "pulse_install"), "one install");
                Check.Equal(1, names.Count(x => x == "pulse_app_update"), "one update");
                var exits = rig.Collector.Rows.Where(r => (string)r["name"] == "pulse_exit").ToList();
                Check.Equal(1, exits.Count, "one exit report");
                var p = (Dictionary<string, object>)exits[0]["p"];
                Check.Equal("crash_native", (string)p["reason"], "OS reason wins");
                Check.Equal("1.5.0", (string)p["prev_version"], "prev_version");
                string bSid = (string)rig.Collector.Rows.First(r => (string)r["name"] == "level_start")["sid"];
                Check.Equal(bSid, (string)p["prev_sid"], "prev_sid is the session of the process that died");
                Check.True((string)exits[0]["sid"] != bSid, "pulse_exit itself is in the new session");
                Check.Equal(512000L, (long)p["rss_kb"], "rss_kb");
                var upd = rig.Collector.Rows.Single(r => (string)r["name"] == "pulse_app_update");
                Check.Equal("1.4.0", (string)((Dictionary<string, object>)upd["p"])["from_version"], "from_version");
            }
        }

        [PulseTest]
        public static void Backup_restore_rotates_the_install_id()
        {
            using (var rig = new Rig())
            {
                rig.NoBackupDir = Path.Combine(rig.Dir, "nobackup");
                PulseCore a = rig.Start();
                string id1 = a.GetInstallId();
                a.OnBackground();
                PulseCore b = rig.Restart();
                Check.Equal(id1, b.GetInstallId(), "same device: same id");
                // the data directory comes back from a backup on a new phone: the no-backup marker does not
                Directory.Delete(rig.NoBackupDir, true);
                PulseCore c = rig.Restart();
                c.Flush();
                rig.Drain();
                Check.True(c.GetInstallId() != id1, "restored: new id");
                var inst = rig.Collector.Rows.Where(r => (string)r["name"] == "pulse_install").ToList();
                Check.Equal(2, inst.Count, "a second pulse_install");
                Check.Equal(true, (bool)((Dictionary<string, object>)inst[1]["p"])["restored"], "restored=true");
                Check.Equal(1L, (long)inst[1]["seq"], "seq restarts for the new id");
            }
        }

        [PulseTest]
        public static void Level_attempts_and_durations()
        {
            using (var rig = new Rig())
            {
                PulseCore core = rig.Start();
                core.LevelStart(3);
                rig.Step(12_000);
                core.LevelFail(3, "hit_spike", 0.1, 0.9, 0.4);
                core.LevelStart(3);
                rig.Step(5_000);
                core.OnBackground();
                rig.Step(60_000); // background time is not level time
                core.OnForeground();
                rig.Step(3_000);
                core.LevelComplete(3, 900, 2);
                core.LevelStart(3);
                core.Flush();
                rig.Drain();
                var lv = rig.Collector.Rows.Where(r => ((string)r["name"]).StartsWith("level_")).Select(r => (Dictionary<string, object>)r["p"]).ToList();
                Check.Equal(1L, (long)lv[0]["attempt"], "first start");
                Check.Equal(12_000L, (long)lv[1]["duration_ms"], "fail duration");
                Check.Equal("norm", (string)lv[1]["pos_space"], "pos_space default");
                Check.Equal(2L, (long)lv[2]["attempt"], "retry is attempt 2");
                Check.Equal(8_000L, (long)lv[3]["duration_ms"], "complete: foreground time only");
                Check.Equal(2L, (long)lv[3]["attempt"], "complete on attempt 2");
                Check.Equal(1L, (long)lv[4]["attempt"], "attempts reset after a win");
            }
        }
    }

    public static class PrivacyTests
    {
        [PulseTest]
        public static void Opt_out_wipes_the_queue_and_keeps_only_the_flag()
        {
            using (var rig = new Rig())
            {
                rig.Transport.Responder = s => FakeTransport.Status(0);
                PulseCore core = rig.Start();
                QueueTestsShared.Fill(core, 30);
                Check.True(rig.QueueFiles().Length > 0, "events on disk");
                core.OptOut();
                Check.Equal(0, rig.QueueFiles().Length, "queue wiped");
                string state = File.ReadAllText(Path.Combine(rig.DataDir, "state.json"));
                Check.True(state.Contains("\"opted_out\":true") && !state.Contains("install_id"), "only the flag: " + state);
                int requests = rig.Transport.Requests.Count;
                core.Screen("after");
                rig.Transport.Responder = FakeTransport.Ok;
                core.Flush();
                rig.Step(120_000, 5);
                Check.Equal(requests, rig.Transport.Requests.Count, "nothing sent after opt-out");
                PulseCore again = rig.Restart();
                again.Screen("next launch");
                again.Flush();
                rig.Drain();
                Check.Equal(0, rig.QueueFiles().Length, "still opted out after a restart");
                Check.True(again.GetStats().OptedOut, "flag persisted");
                Check.True(again.GetInstallId() == null, "no install id");
                again.SetConsent(true);
                again.Flush();
                rig.Drain();
                Check.True(rig.Collector.Names().Contains("pulse_install"), "opting in again starts a new install");
                Check.True(rig.Collector.Names().Contains("pulse_consent"), "consent event");
            }
        }

        [PulseTest]
        public static void Consent_required_writes_nothing_until_granted()
        {
            using (var rig = new Rig())
            {
                rig.Options.ConsentRequired = true;
                PulseCore core = rig.Start();
                core.LevelStart(1);
                core.Flush();
                rig.Step(120_000, 3);
                Check.Equal(0, rig.Transport.Requests.Count, "nothing sent");
                Check.True(!Directory.Exists(rig.DataDir) || Directory.GetFiles(rig.DataDir, "*", SearchOption.AllDirectories).Length == 0, "nothing on disk");
                Check.True(core.GetInstallId() == null, "no id before consent");
                core.SetConsent(true);
                core.Flush();
                rig.Drain();
                var b = rig.Transport.Batches.Single();
                Check.True(b.Json.Contains("\"consent\":\"granted\""), "ctx.consent granted");
                var names = rig.Collector.Names();
                Check.True(names.Take(3).SequenceEqual(new[] { "pulse_install", "pulse_session_start", "level_start" }), "pre-consent events kept in order: " + string.Join(",", names));
                Check.Equal("pulse_consent", names.Last(), "then pulse_consent");
            }
        }

        [PulseTest]
        public static void Consent_refused_drops_the_buffered_events()
        {
            using (var rig = new Rig())
            {
                rig.Options.ConsentRequired = true;
                PulseCore core = rig.Start();
                core.LevelStart(1);
                core.SetConsent(false);
                rig.Step(120_000, 3);
                Check.Equal(0, rig.Transport.Requests.Count, "nothing sent");
                Check.Equal(0, rig.QueueFiles().Length, "no queue");
                PulseCore again = rig.Restart();
                again.LevelStart(2);
                Check.True(again.GetStats().OptedOut, "the refusal is remembered");
            }
        }

        [PulseTest]
        public static void Data_deletion_request_is_sent_and_the_id_rotates()
        {
            using (var rig = new Rig())
            {
                int deletes = 0;
                rig.Transport.Responder = s => s.Url.EndsWith("/v2/delete") && deletes++ == 0 ? FakeTransport.Status(503) : FakeTransport.Ok(s);
                PulseCore core = rig.Start();
                string old = core.GetInstallId();
                core.Screen("menu");
                core.RequestDataDeletion();
                Check.True(core.GetInstallId() != old, "new id");
                for (int i = 0; i < 10; i++) rig.Step(10_000);
                var dels = rig.Transport.Requests.Where(r => r.Url.EndsWith("/v2/delete")).ToList();
                Check.Equal(2, dels.Count, "retried after 503");
                Check.True(dels[1].Json.Contains("\"install_id\":\"" + old + "\""), "the old id is deleted: " + dels[1].Json);
                core.Flush();
                rig.Drain();
                Check.True(rig.Collector.Rows.All(r => (string)r["install_id"] != old), "no event of the old id was sent after the request");
                Check.Equal(1, rig.Collector.Rows.Count(r => (string)r["name"] == "pulse_install"), "the new id has its own install event");
            }
        }

        [PulseTest]
        public static void Reset_install_id_keeps_queued_events_under_the_old_id()
        {
            using (var rig = new Rig())
            {
                PulseCore core = rig.Start();
                string a = core.GetInstallId();
                core.Screen("x");
                core.ResetInstallId();
                core.Screen("y");
                core.Flush();
                for (int i = 0; i < 5; i++) rig.Drain();
                var ids = rig.Collector.Rows.Select(r => (string)r["install_id"]).Distinct().ToList();
                Check.Equal(2, ids.Count, "two ids");
                Check.Equal(a, ids[0], "old events keep the old id");
                Check.Equal(1L, rig.Collector.Seqs(core.GetInstallId()).First(), "seq restarts for the new id");
            }
        }

        [PulseTest]
        public static void Child_mode_strips_uid_and_free_text()
        {
            using (var rig = new Rig())
            {
                rig.Options.ChildDirected = true;
                PulseCore core = rig.Start();
                core.SetUserId("player-42");
                core.LevelStart(1, "Forest of Names");
                core.TutorialStep(2, "drag the gem");
                core.Track("booster_used", new PulseParams().Add("booster", "hammer").Add("count", 2));
                core.ReportError("Exception", "boom", "A.B ()");
                core.Flush();
                rig.Drain();
                string json = rig.Transport.Batches[0].Json;
                Check.True(json.Contains("\"child\":true"), "ctx.child");
                Check.True(!json.Contains("player-42") && !json.Contains("Forest") && !json.Contains("drag the gem") && !json.Contains("hammer") && !json.Contains("boom"), json);
                Check.True(json.Contains("\"count\":2"), "numbers stay");
            }
        }

        [PulseTest]
        public static void Uid_that_looks_like_an_email_is_refused()
        {
            using (var rig = new Rig())
            {
                PulseCore core = rig.Start();
                core.SetUserId("someone@example.com");
                core.Screen("x");
                core.Flush();
                rig.Drain();
                Check.True(!rig.Transport.Batches[0].Json.Contains("example.com"), "not sent");
            }
        }
    }

    static class QueueTestsShared
    {
        public static void Fill(PulseCore core, int n)
        {
            for (int i = 0; i < n; i++) core.Screen("screen_" + i);
        }
    }

    public static class ApiTests
    {
        [PulseTest]
        public static void Errors_are_scrubbed_deduped_and_capped_at_10_per_launch()
        {
            using (var rig = new Rig())
            {
                PulseCore core = rig.Start();
                for (int i = 0; i < 15; i++)
                {
                    core.ReportError("E" + (char)('A' + i) + "Exception", "user@mail.com opened /data/user/0/file.txt 42 times", "Game.Run () (at Assets/Game.cs:" + i + ")");
                    core.ReportError("E" + (char)('A' + i) + "Exception", "user@mail.com opened /data/user/0/file.txt 43 times", "Game.Run ()");
                }
                core.Flush();
                rig.Drain();
                var errs = rig.Collector.Rows.Where(r => (string)r["name"] == "pulse_error").Select(r => (Dictionary<string, object>)r["p"]).ToList();
                Check.Equal(10, errs.Count, "10 unique per launch; repeats within the session dropped");
                Check.Equal("<email> opened <path> # times", (string)errs[0]["msg"], "scrubbed");
                Check.Equal("Game.Run", (string)errs[0]["frame"], "top frame");
                Check.Equal(16, ((string)errs[0]["fp"]).Length, "64-bit fingerprint");
                Check.Equal(PulseErrors.Fingerprint("EAException", "Game.Run", "<email> opened <path> # times"), (string)errs[0]["fp"], "fingerprint");
            }
        }

        [PulseTest]
        public static void Scrub_and_frame_rules()
        {
            Check.Equal("Index # out of range <hex> <id> <path>", PulseErrors.Scrub("Index 12 out of range 0x7ffe 9f86d081884c7d65 C:\\x\\y\nsecond line"), "scrub");
            Check.Equal("Board.Swap", PulseErrors.TopFrame("Board.Swap () (at Assets/Board.cs:42)\nUnityEngine.Debug:Log"), "unity frame");
            Check.Equal("Board.swap", PulseErrors.TopFrame("#0      Board.swap (package:game/board.dart:42:7)"), "dart frame");
            Check.Equal("Game.Program.Main", PulseErrors.TopFrame("   at Game.Program.Main(String[] args) in /src/Program.cs:line 9"), ".NET frame");
            Check.Equal("9f86d081884c7d65".Length, PulseErrors.Fingerprint("a", "b", "c").Length, "fp length");
        }

        [PulseTest]
        public static void Custom_events_are_validated()
        {
            using (var rig = new Rig())
            {
                PulseCore core = rig.Start();
                Check.True(core.Track("booster_used", new PulseParams().Add("booster", "hammer").Add("level", 38).Add("power", 1.5).Add("golden", true)), "valid");
                Check.True(!core.Track("Booster Used"), "bad name");
                Check.True(!core.Track("session_start"), "reserved name");
                Check.True(!core.Track("firebase_x"), "reserved prefix");
                Check.True(!core.Track("pulse_custom"), "SDK prefix");
                var p = new PulseParams();
                for (int i = 0; i < 26; i++) p.Add("p" + i, i);
                Check.True(!core.Track("too_many", p), "26 params");
                Check.True(!core.Track("bad_param", new PulseParams().Add("Bad Key", 1)), "bad param name");
                core.Track("long_text", new PulseParams().Add("t", new string('x', 300)));
                core.Flush();
                rig.Drain();
                Check.Equal(1, rig.Collector.Rows.Count(r => (string)r["name"] == "booster_used"), "sent once");
                var lt = (Dictionary<string, object>)rig.Collector.Rows.Single(r => (string)r["name"] == "long_text")["p"];
                Check.Equal(100, ((string)lt["t"]).Length, "strings cut to 100");
                Check.Equal(6L, core.GetStats().Rejected, "six refused");
            }
        }

        [PulseTest]
        public static void Enum_parameters_are_checked()
        {
            using (var rig = new Rig())
            {
                PulseCore core = rig.Start();
                core.SettingChange("volume", "1", "0");
                core.SettingChange(PulseContract.Setting.Haptics, true, false);
                core.AdEvent("explode", "rewarded");
                core.LevelQuit(1, "rage");
                core.Flush();
                rig.Drain();
                var names = rig.Collector.Names();
                Check.Equal(1, names.Count(n => n == "setting_change"), "unknown setting refused");
                Check.Equal(0, names.Count(n => n == "ad_event"), "unknown action refused");
                var q = (Dictionary<string, object>)rig.Collector.Rows.Single(r => (string)r["name"] == "level_quit")["p"];
                Check.Equal("menu", (string)q["reason"], "unknown quit reason -> menu");
                var s = (Dictionary<string, object>)rig.Collector.Rows.Single(r => (string)r["name"] == "setting_change")["p"];
                Check.Equal("off", (string)s["to"], "bool overload");
            }
        }

        [PulseTest]
        public static void Recording_an_event_allocates_nothing()
        {
            using (var rig = new Rig())
            {
                rig.Transport.Responder = s => null; // keep the queue on disk (no request completes)
                rig.Options.FlushEvents = int.MaxValue;
                rig.Options.FlushIntervalMs = int.MaxValue;
                PulseCore core = rig.Start();
                var p = new PulseParams();
                void Burst(int n)
                {
                    for (int i = 0; i < n; i++)
                    {
                        core.LevelStart(i, null, "forest", "classic");
                        core.LevelFail(i, "out_of_moves", 0.42, 0.77, 0.8);
                        core.EarnCurrency("coins", 50.5, "chest", i);
                        core.AdImpression("applovin_max", "rewarded", 0.0123, "USD", "admob", null, "extra_moves", "exact");
                        p.Clear().Add("booster", "hammer").Add("level", i).Add("power", 1.25);
                        core.Track("booster_used", p);
                        core.Tick();
                    }
                }
                Burst(300); // warm up: buffers grow, JIT, first segment files
                long before = GC.GetAllocatedBytesForCurrentThread();
                Burst(10);
                long after = GC.GetAllocatedBytesForCurrentThread();
                // 50 events (well inside one 100-event segment) and 10 ticks
                Check.Equal(0L, after - before, "bytes allocated by 50 events + 10 ticks");
            }
        }

        [PulseTest]
        public static void Numbers_match_the_shared_table()
        {
            string path = PulseTestRunner.FindRepoFile("contract/golden/sdk_numbers.json");
            var cases = (List<object>)MiniJson.Parse(File.ReadAllText(path));
            var w = new PulseJsonWriter();
            int n = 0;
            foreach (List<object> c in cases)
            {
                double v = BitConverter.Int64BitsToDouble(Convert.ToInt64((string)c[0], 16));
                w.Clear();
                w.Double(v);
                string got = Encoding.ASCII.GetString(w.Buffer, 0, w.Length);
                Check.Equal((string)c[1], got, "double " + c[0]);
                n++;
            }
            Check.True(n > 1000, "table size " + n);
        }

        [PulseTest]
        public static void Strings_are_escaped_and_cut_like_the_dart_sdk()
        {
            var w = new PulseJsonWriter();
            w.String("a\"b\\c\n\t\u0001\u007f é 🎮", 100);
            Check.Equal("\"a\\\"b\\\\c\\n\\t\\u0001\u007f é 🎮\"", Encoding.UTF8.GetString(w.Buffer, 0, w.Length), "escapes");
            w.Clear();
            w.String("ab🎮", 3); // never half a surrogate pair
            Check.Equal("\"ab\"", Encoding.UTF8.GetString(w.Buffer, 0, w.Length), "cut before a pair");
            w.Clear();
            w.String("x\ud800y");
            Check.Equal("\"x\uFFFDy\"", Encoding.UTF8.GetString(w.Buffer, 0, w.Length), "lone surrogate");
            Check.Equal("app.1game", PulseCore.SafeAppId("1game"), "app id must start with a letter");
            Check.Equal("com.my_game", PulseCore.SafeAppId("com.my game"), "invalid characters");
        }

        [PulseTest]
        public static void Store_mapping()
        {
            Check.Equal("google_play", PulseStores.FromAndroidInstaller("com.android.vending"), "play");
            Check.Equal("sideload", PulseStores.FromAndroidInstaller(null), "adb");
            Check.Equal("unknown", PulseStores.FromAndroidInstaller("com.some.store"), "other");
            Check.Equal("testflight", PulseStores.FromIos("sandbox"), "TestFlight");
            Check.Equal("app_store", PulseStores.FromIos("production"), "App Store");
            Check.Equal("sideload", PulseStores.FromIos("xcode"), "Xcode");
        }
    }
}
