import Flutter
import StoreKit
import UIKit

/// Studio Pulse SDK, iOS side: app and device context the Dart core cannot read.
///
/// "info" answers the install source (AppTransaction.environment on iOS 16+: production =
/// App Store, sandbox = TestFlight, xcode = development; waited for at most 1.5 s, else
/// the receipt file name: sandboxReceipt = TestFlight; the simulator says so), the app
/// version and build, the hardware model, RAM and the Application Support directory.
/// "excludeFromBackup" sets NSURLIsExcludedFromBackupKey on the SDK's install marker so a
/// restored backup is recognised. No IDFA, IDFV or other device identifier.
public class StudioPulsePlugin: NSObject, FlutterPlugin {
  public static func register(with registrar: FlutterPluginRegistrar) {
    let channel = FlutterMethodChannel(name: "studio_pulse", binaryMessenger: registrar.messenger())
    registrar.addMethodCallDelegate(StudioPulsePlugin(), channel: channel)
  }

  public func handle(_ call: FlutterMethodCall, result: @escaping FlutterResult) {
    switch call.method {
    case "info":
      installSource { source in
        DispatchQueue.main.async { result(Self.info(source)) }
      }
    case "excludeFromBackup":
      if let path = call.arguments as? String {
        var url = URL(fileURLWithPath: path)
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        try? url.setResourceValues(values)
      }
      result(nil)
    default:
      result(FlutterMethodNotImplemented)
    }
  }

  private static func info(_ source: String) -> [String: Any?] {
    let b = Bundle.main
    var sys = utsname()
    uname(&sys)
    let model = withUnsafePointer(to: &sys.machine) {
      $0.withMemoryRebound(to: CChar.self, capacity: 1) { String(cString: $0) }
    }
    let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first?.path
    return [
      "app_id": b.bundleIdentifier,
      "app_version": b.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String,
      "build": b.object(forInfoDictionaryKey: "CFBundleVersion") as? String,
      "app_name": (b.object(forInfoDictionaryKey: "CFBundleDisplayName") ?? b.object(forInfoDictionaryKey: "CFBundleName")) as? String,
      "os": "ios " + UIDevice.current.systemVersion,
      "device_model": model,
      "ram_mb": Int(ProcessInfo.processInfo.physicalMemory / (1024 * 1024)),
      "files_dir": support,
      "no_backup_dir": support,
      "install_source": source,
    ]
  }

  private func installSource(_ done: @escaping (String) -> Void) {
    #if targetEnvironment(simulator)
    done("simulator")
    #else
    let receipt = Bundle.main.appStoreReceiptURL?.lastPathComponent == "sandboxReceipt" ? "testflight" : "app_store"
    if #available(iOS 16.0, *) {
      let once = Once(done)
      DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { once.fire(receipt) }
      Task.detached(priority: .utility) {
        do {
          let env: AppStore.Environment
          switch try await AppTransaction.shared {
          case .verified(let t): env = t.environment
          case .unverified(let t, _): env = t.environment
          }
          if env == .production { once.fire("production") }
          else if env == .sandbox { once.fire("sandbox") }
          else if env == .xcode { once.fire("xcode") }
          else { once.fire(receipt) }
        } catch {
          once.fire(receipt) // offline or not signed in: AppTransaction.shared never prompts
        }
      }
    } else {
      done(receipt)
    }
    #endif
  }
}

/// Calls the callback once, from whichever answer comes first.
private final class Once: @unchecked Sendable {
  private let lock = NSLock()
  private var callback: ((String) -> Void)?

  init(_ callback: @escaping (String) -> Void) { self.callback = callback }

  func fire(_ value: String) {
    lock.lock()
    let cb = callback
    callback = nil
    lock.unlock()
    cb?(value)
  }
}
