using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Threading;

namespace Studio.Pulse.Tests
{
    /// <summary>
    /// A realistic play session against a real collector (`pulse collect`) over HTTP with the
    /// real clock: used by the E2E test (PULSE_E2E_URL) and by the console driver.
    /// </summary>
    public static class E2EScenario
    {
        public sealed class Result
        {
            public string InstallId;
            public long Recorded;
            public long Sent;
            public int Queued;
            public int LastStatus;
            public string LastError;
            public long Rejected;
        }

        public static Result Run(string url, string key, string dataDir, string platform, string store, string appId = "com.studio.e2e.unity")
        {
            var dev = new PulseDevice
            {
                AppId = appId,
                Platform = platform,
                AppVersion = "1.0.0",
                Build = "100",
                BuildEnv = "release",
                InstallStore = store,
                InstallerRaw = store == "google_play" ? "com.android.vending" : null,
                AppName = "E2E Unity Game",
                Sdk = "unity/" + PulseCore.Version,
                Engine = "unity 6000.3",
                Os = platform == "ios" ? "ios 18.1" : "android 14",
                DeviceModel = platform == "ios" ? "iPhone15,2" : "SM-A145F",
                DeviceTier = "mid",
                RamMb = 6144,
                Locale = "tr-TR",
                TzOffsetMin = 180,
            };
            var logs = new List<string>();
            var env = new PulseEnvironment
            {
                DataDirectory = dataDir,
                Clock = new SystemClock(),
                Random = new CryptoRandom(),
                Transport = new HttpClientTransport(TimeSpan.FromSeconds(10)),
                Log = m => { lock (logs) logs.Add(m); },
            };
            var core = new PulseCore(key, url, new PulseOptions(), dev, env);
            core.Start();
            core.TutorialBegin();
            core.TutorialStep(1, "tap to swap");
            core.TutorialComplete();
            core.Screen("menu", "MenuScreen");
            core.SettingChange(PulseContract.Setting.Music, "on", "off");
            core.SettingChange(PulseContract.Setting.Sfx, true, false);
            for (int level = 1; level <= 5; level++)
            {
                core.LevelStart(level, null, "forest");
                Thread.Sleep(5);
                if (level >= 3)
                {
                    core.LevelFail(level, "out_of_moves", 0.25 + 0.1 * level, 0.6, 0.7);
                    core.LevelStart(level, null, "forest");
                    Thread.Sleep(5);
                }
                if (level == 5)
                {
                    core.LevelQuit(level, PulseContract.QuitReason.Menu, 0.3, 0.5, 0.5);
                    break;
                }
                core.LevelComplete(level, 1000 * level, 3);
                core.EarnCurrency("coins", 25, "level_reward", level);
            }
            core.SpendCurrency("coins", 40, "booster_hammer");
            core.AdEvent(PulseContract.AdAction.Show, "rewarded", "extra_moves");
            core.AdImpression("applovin_max", "rewarded", 0.0123, "USD", "admob", null, "extra_moves", "exact");
            core.Purchase("e2e-" + Guid.NewGuid().ToString("N").Substring(0, 12), "coins_pack_1", 49_990_000, "TRY", false, false);
            try
            {
                object o = null;
                o.ToString();
            }
            catch (Exception ex)
            {
                core.ReportError(ex.GetType().Name, ex.Message, ex.StackTrace);
            }
            core.Track("booster_used", new PulseParams().Add("booster", "hammer").Add("level", 4));
            core.OnBackground();
            core.OnForeground();
            core.Flush();
            var sw = Stopwatch.StartNew();
            while (sw.ElapsedMilliseconds < 30_000)
            {
                core.Tick();
                core.Flush();
                PulseStats st = core.GetStats();
                if (st.Queued == 0 && st.Sent >= st.Recorded) break;
                Thread.Sleep(20);
            }
            core.OnQuit();
            core.Close();
            PulseStats s = core.GetStats();
            return new Result
            {
                InstallId = core.GetInstallId(),
                Recorded = s.Recorded,
                Sent = s.Sent,
                Queued = s.Queued,
                LastStatus = s.LastStatus,
                LastError = s.LastError,
                Rejected = s.Rejected,
            };
        }

        public static string Get(string url)
        {
            using (var c = new HttpClient { Timeout = TimeSpan.FromSeconds(10) })
            {
                return c.GetStringAsync(url).GetAwaiter().GetResult();
            }
        }
    }

    public static class E2ETests
    {
        [PulseTest]
        public static void Session_reaches_a_real_collector()
        {
            string url = Environment.GetEnvironmentVariable("PULSE_E2E_URL");
            if (string.IsNullOrEmpty(url)) throw new SkipException("PULSE_E2E_URL not set (CI starts `pulse collect` and sets it)");
            string key = Environment.GetEnvironmentVariable("PULSE_E2E_KEY") ?? "sp_pub_local";
            string dir = Path.Combine(Path.GetTempPath(), "pulse-e2e-" + Guid.NewGuid().ToString("N"));
            try
            {
                E2EScenario.Result r = E2EScenario.Run(url, key, dir, "android", "google_play", "com.studio.e2e.dotnet");
                Check.Equal(0, r.Queued, "queue drained (last status " + r.LastStatus + " " + r.LastError + ")");
                Check.Equal(r.Recorded, r.Sent, "every recorded event was accepted");
                Check.Equal(0L, r.Rejected, "nothing quarantined");
                string live = E2EScenario.Get(url.TrimEnd('/') + "/live");
                var o = (Dictionary<string, object>)MiniJson.Parse(live);
                bool found = false;
                foreach (Dictionary<string, object> app in (List<object>)o["apps"])
                {
                    if ((string)app["app_id"] == "com.studio.e2e.dotnet" && Convert.ToInt64(app["active"]) >= 1) found = true;
                }
                Check.True(found, "/live counts the install: " + live);
            }
            finally
            {
                try { Directory.Delete(dir, true); } catch (Exception) { }
            }
        }
    }
}
