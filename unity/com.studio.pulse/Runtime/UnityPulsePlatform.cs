using System;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using UnityEngine;

namespace Studio.Pulse
{
    /// <summary>
    /// What the SDK reads from Unity and the OS: app and device context, the install store
    /// (Android installer of record through JNI, iOS AppTransaction / receipt through the Swift
    /// plug-in in Plugins/iOS), the previous process's exit reason (Android 11+) and the
    /// directory that is never restored from a backup. No advertising or device identifier.
    /// </summary>
    public static class UnityPulsePlatform
    {
        public static string DataDirectory
        {
            get { return Path.Combine(Application.persistentDataPath, "studio-pulse"); }
        }

        public static string PlatformName(RuntimePlatform p)
        {
            switch (p)
            {
                case RuntimePlatform.Android: return PulseContract.Platform.Android;
                case RuntimePlatform.IPhonePlayer: return PulseContract.Platform.Ios;
                case RuntimePlatform.WebGLPlayer: return PulseContract.Platform.Webgl;
                case RuntimePlatform.WindowsPlayer: return PulseContract.Platform.Windows;
                case RuntimePlatform.OSXPlayer: return PulseContract.Platform.Macos;
                case RuntimePlatform.LinuxPlayer: return PulseContract.Platform.Linux;
                default: return PulseContract.Platform.Editor; // editors and platforms the contract does not list
            }
        }

        public static PulseDevice Device()
        {
            var d = new PulseDevice
            {
                AppId = Application.identifier,
                Platform = PlatformName(Application.platform),
                AppVersion = Application.version,
                BuildEnv = Application.isEditor ? PulseContract.BuildEnv.Editor
                    : (Debug.isDebugBuild ? PulseContract.BuildEnv.Debug : PulseContract.BuildEnv.Release),
                AppName = Application.productName,
                Sdk = PulseCore.SdkName,
                Engine = "unity " + Application.unityVersion,
                Os = OsName(),
                DeviceModel = SystemInfo.deviceModel,
                DeviceTier = Studio.Pulse.DeviceTier.Current,
                RamMb = SystemInfo.systemMemorySize > 0 ? SystemInfo.systemMemorySize : -1,
                Locale = Locale(),
                TzOffsetMin = (int)TimeZoneInfo.Local.GetUtcOffset(DateTime.UtcNow).TotalMinutes,
            };
            if (string.IsNullOrEmpty(d.AppId)) d.AppId = Application.productName;
            if (Application.isEditor)
            {
                d.InstallStore = PulseContract.InstallStore.Editor;
                return d;
            }
#if UNITY_ANDROID
            AndroidInfo(d);
#elif UNITY_IOS
            IosInfo(d);
#endif
            return d;
        }

        static string OsName()
        {
            string os = SystemInfo.operatingSystem ?? "";
            // "Android OS 14 / API-34 (UP1A...)" -> "android 14 / api-34"; "iOS 18.1" -> "ios 18.1"
            int paren = os.IndexOf('(');
            if (paren > 0) os = os.Substring(0, paren);
            os = os.Replace("Android OS", "android").Trim().ToLowerInvariant();
            return os.Length > 50 ? os.Substring(0, 50) : os;
        }

        static string Locale()
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            try
            {
                using (var loc = new AndroidJavaClass("java.util.Locale"))
                using (var def = loc.CallStatic<AndroidJavaObject>("getDefault"))
                {
                    string tag = def.Call<string>("toLanguageTag");
                    if (!string.IsNullOrEmpty(tag)) return tag;
                }
            }
            catch (Exception)
            {
            }
#endif
            try
            {
                string name = CultureInfo.CurrentCulture.Name;
                if (!string.IsNullOrEmpty(name)) return name;
            }
            catch (Exception)
            {
            }
            return LanguageCode(Application.systemLanguage);
        }

        static string LanguageCode(SystemLanguage l)
        {
            switch (l)
            {
                case SystemLanguage.Turkish: return "tr";
                case SystemLanguage.English: return "en";
                case SystemLanguage.German: return "de";
                case SystemLanguage.French: return "fr";
                case SystemLanguage.Spanish: return "es";
                case SystemLanguage.Portuguese: return "pt";
                case SystemLanguage.Italian: return "it";
                case SystemLanguage.Russian: return "ru";
                case SystemLanguage.Japanese: return "ja";
                case SystemLanguage.Korean: return "ko";
                case SystemLanguage.Arabic: return "ar";
                case SystemLanguage.ChineseSimplified: return "zh-Hans";
                case SystemLanguage.ChineseTraditional: return "zh-Hant";
                default: return null;
            }
        }

#if UNITY_ANDROID
        static AndroidJavaObject Activity()
        {
            using (var player = new AndroidJavaClass("com.unity3d.player.UnityPlayer"))
            {
                return player.GetStatic<AndroidJavaObject>("currentActivity");
            }
        }

        static int SdkInt()
        {
            using (var v = new AndroidJavaClass("android.os.Build$VERSION")) return v.GetStatic<int>("SDK_INT");
        }

        /// <summary>Installer of record (InstallSourceInfo, API 30+; getInstallerPackageName before) and the build number.</summary>
        static void AndroidInfo(PulseDevice d)
        {
            try
            {
                using (AndroidJavaObject activity = Activity())
                using (AndroidJavaObject pm = activity.Call<AndroidJavaObject>("getPackageManager"))
                {
                    string pkg = activity.Call<string>("getPackageName");
                    int sdk = SdkInt();
                    string installer;
                    if (sdk >= 30)
                    {
                        using (AndroidJavaObject info = pm.Call<AndroidJavaObject>("getInstallSourceInfo", pkg))
                        {
                            installer = info.Call<string>("getInstallingPackageName");
                        }
                    }
                    else
                    {
                        installer = pm.Call<string>("getInstallerPackageName", pkg);
                    }
                    d.InstallerRaw = installer;
                    d.InstallStore = PulseStores.FromAndroidInstaller(installer);
                    using (AndroidJavaObject pi = pm.Call<AndroidJavaObject>("getPackageInfo", pkg, 0))
                    {
                        d.Build = sdk >= 28
                            ? pi.Call<long>("getLongVersionCode").ToString(CultureInfo.InvariantCulture)
                            : pi.Get<int>("versionCode").ToString(CultureInfo.InvariantCulture);
                    }
                }
            }
            catch (Exception ex)
            {
                d.InstallStore = PulseContract.InstallStore.Unknown;
                Debug.LogWarning("[Studio.Pulse] install source: " + ex.Message);
            }
        }

        /// <summary>getNoBackupFilesDir(): never restored by Auto Backup, so a marker there tells a restore from the original install.</summary>
        public static string AndroidNoBackupDirectory()
        {
            try
            {
                using (AndroidJavaObject activity = Activity())
                using (AndroidJavaObject dir = activity.Call<AndroidJavaObject>("getNoBackupFilesDir"))
                {
                    return dir.Call<string>("getAbsolutePath");
                }
            }
            catch (Exception)
            {
                return null;
            }
        }

        /// <summary>ActivityManager.getHistoricalProcessExitReasons (API 30+): the most recent process death.</summary>
        public static PulseExitInfo AndroidPreviousExit()
        {
            if (SdkInt() < 30) return null;
            using (AndroidJavaObject activity = Activity())
            using (AndroidJavaObject am = activity.Call<AndroidJavaObject>("getSystemService", "activity"))
            using (AndroidJavaObject list = am.Call<AndroidJavaObject>("getHistoricalProcessExitReasons", activity.Call<string>("getPackageName"), 0, 1))
            {
                if (list == null || list.Call<int>("size") == 0) return null;
                using (AndroidJavaObject e = list.Call<AndroidJavaObject>("get", 0))
                {
                    return new PulseExitInfo
                    {
                        Reason = ExitReason(e.Call<int>("getReason")),
                        TimestampMs = e.Call<long>("getTimestamp"),
                        RssKb = e.Call<long>("getRss"),
                    };
                }
            }
        }
#endif

        /// <summary>ApplicationExitInfo.REASON_* -> contract exit_reason.</summary>
        public static string ExitReason(int reason)
        {
            switch (reason)
            {
                case 3: return PulseContract.ExitReason.LowMemory;   // REASON_LOW_MEMORY
                case 4: return PulseContract.ExitReason.Crash;       // REASON_CRASH (Java/Kotlin)
                case 5: return PulseContract.ExitReason.CrashNative; // REASON_CRASH_NATIVE
                case 6: return PulseContract.ExitReason.Anr;         // REASON_ANR
                case 1:                                              // REASON_EXIT_SELF
                case 10:                                             // REASON_USER_REQUESTED
                case 11: return PulseContract.ExitReason.User;       // REASON_USER_STOPPED
                default: return PulseContract.ExitReason.Unknown;
            }
        }

#if UNITY_IOS && !UNITY_EDITOR
        [DllImport("__Internal")] static extern IntPtr StudioPulse_InstallSource();
        [DllImport("__Internal")] static extern IntPtr StudioPulse_BuildNumber();
        [DllImport("__Internal")] static extern void StudioPulse_StartAppTransactionCheck();
        [DllImport("__Internal")] static extern IntPtr StudioPulse_AppTransactionEnvironment();
        [DllImport("__Internal")] static extern void StudioPulse_Free(IntPtr p);

        static string Take(IntPtr p)
        {
            if (p == IntPtr.Zero) return null;
            string s = Marshal.PtrToStringAnsi(p);
            StudioPulse_Free(p);
            return s;
        }

        static void IosInfo(PulseDevice d)
        {
            try
            {
                StudioPulse_StartAppTransactionCheck();
                d.Build = Take(StudioPulse_BuildNumber());
                string native = Take(StudioPulse_InstallSource()); // receipt / simulator answer, or a cached AppTransaction one
                d.InstallStore = PulseStores.FromIos(native);
            }
            catch (Exception ex)
            {
                d.InstallStore = PulseContract.InstallStore.Unknown;
                Debug.LogWarning("[Studio.Pulse] install source: " + ex.Message);
            }
        }

        /// <summary>AppTransaction.environment once StoreKit answered (iOS 16+), else null.</summary>
        public static string IosAppTransactionStore()
        {
            try
            {
                string env = Take(StudioPulse_AppTransactionEnvironment());
                return env == null ? null : PulseStores.FromIos(env);
            }
            catch (Exception)
            {
                return null;
            }
        }
#else
        public static string IosAppTransactionStore()
        {
            return null;
        }
#endif
    }
}
