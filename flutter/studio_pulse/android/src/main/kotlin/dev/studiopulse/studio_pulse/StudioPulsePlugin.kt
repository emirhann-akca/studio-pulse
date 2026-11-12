package dev.studiopulse.studio_pulse

import android.app.ActivityManager
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import io.flutter.embedding.engine.plugins.FlutterPlugin
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import io.flutter.plugin.common.MethodChannel.MethodCallHandler
import io.flutter.plugin.common.MethodChannel.Result

/**
 * Studio Pulse SDK, Android side: app and device context the Dart core cannot read.
 *
 * "info" answers the installer of record (InstallSourceInfo.getInstallingPackageName on
 * API 30+, getInstallerPackageName before), the app version and build, the device model
 * and RAM, the files and no-backup directories (getNoBackupFilesDir is never restored by
 * Auto Backup) and the reason the previous process died (getHistoricalProcessExitReasons,
 * API 30+). No advertising id, Android ID or other device identifier.
 */
class StudioPulsePlugin : FlutterPlugin, MethodCallHandler {
    private lateinit var channel: MethodChannel
    private lateinit var context: Context

    override fun onAttachedToEngine(binding: FlutterPlugin.FlutterPluginBinding) {
        context = binding.applicationContext
        channel = MethodChannel(binding.binaryMessenger, "studio_pulse")
        channel.setMethodCallHandler(this)
    }

    override fun onDetachedFromEngine(binding: FlutterPlugin.FlutterPluginBinding) {
        channel.setMethodCallHandler(null)
    }

    override fun onMethodCall(call: MethodCall, result: Result) {
        when (call.method) {
            "info" -> result.success(info())
            "excludeFromBackup" -> result.success(null) // Android: the no-backup directory does it
            else -> result.notImplemented()
        }
    }

    private fun info(): Map<String, Any?> {
        val out = HashMap<String, Any?>()
        val pkg = context.packageName
        out["app_id"] = pkg
        out["os"] = "android ${Build.VERSION.RELEASE} / api-${Build.VERSION.SDK_INT}"
        out["device_model"] = Build.MODEL
        out["files_dir"] = context.filesDir.absolutePath
        out["no_backup_dir"] = context.noBackupFilesDir.absolutePath
        try {
            val pm = context.packageManager
            @Suppress("DEPRECATION")
            val pi = pm.getPackageInfo(pkg, 0)
            out["app_version"] = pi.versionName
            out["build"] = if (Build.VERSION.SDK_INT >= 28) pi.longVersionCode.toString() else {
                @Suppress("DEPRECATION")
                pi.versionCode.toString()
            }
            out["app_name"] = pm.getApplicationLabel(context.applicationInfo).toString()
            out["installer"] = if (Build.VERSION.SDK_INT >= 30) {
                pm.getInstallSourceInfo(pkg).installingPackageName
            } else {
                @Suppress("DEPRECATION")
                pm.getInstallerPackageName(pkg)
            }
        } catch (e: PackageManager.NameNotFoundException) {
            out["installer_error"] = e.message
        } catch (e: IllegalArgumentException) {
            out["installer_error"] = e.message
        }
        val am = context.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
        val mem = ActivityManager.MemoryInfo()
        am.getMemoryInfo(mem)
        out["ram_mb"] = (mem.totalMem / (1024L * 1024L)).toInt()
        if (Build.VERSION.SDK_INT >= 30) {
            try {
                val exits = am.getHistoricalProcessExitReasons(pkg, 0, 1)
                if (exits.isNotEmpty()) {
                    val e = exits[0]
                    out["exit_reason"] = e.reason
                    out["exit_ts"] = e.timestamp
                    out["exit_rss_kb"] = e.rss
                }
            } catch (e: SecurityException) {
                out["exit_error"] = e.message
            }
        }
        return out
    }
}
