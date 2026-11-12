import Flutter
import StoreKit
import UIKit

/// Studio Pulse SDK, iOS side: app and device context the Dart core cannot read.
///
/// "info" answers at once: the install source known so far (a cached AppTransaction
/// answer, else the receipt file name: sandboxReceipt = TestFlight; the simulator says
/// so), the app version and build, the hardware model, RAM and the Application Support
/// directory, and starts the AppTransaction check (iOS 16+). "appTransaction" answers
/// AppTransaction.environment (production = App Store, sandbox = TestFlight, xcode =
/// development) when StoreKit has it, waiting at most 10 s, else nil; the Dart side asks
/// without blocking app start. AppTransaction.shared never shows a sign-in prompt
/// (refresh() would; it is not called). "excludeFromBackup" sets
/// NSURLIsExcludedFromBackupKey on the SDK's install marker so a restored backup is
/// recognised. No IDFA, IDFV or other device identifier.
public class StudioPulsePlugin: NSObject, FlutterPlugin {
  private let check = AppTransactionCheck()

  public static func register(with registrar: FlutterPluginRegistrar) {
    let channel = FlutterMethodChannel(name: "studio_pulse", binaryMessenger: registrar.messenger())
    registrar.addMethodCallDelegate(StudioPulsePlugin(), channel: channel)
  }

  public func handle(_ call: FlutterMethodCall, result: @escaping FlutterResult) {
    switch call.method {
    case "info":
      check.start()
      result(Self.info(check.environment ?? Self.receiptSource()))
    case "appTransaction":
      check.start()
      check.wait(seconds: 10) { env in DispatchQueue.main.async { result(env) } }
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

  private static func receiptSource() -> String {
    #if targetEnvironment(simulator)
    return "simulator"
    #else
    return Bundle.main.appStoreReceiptURL?.lastPathComponent == "sandboxReceipt" ? "testflight" : "app_store"
    #endif
  }

  private static func info(_ source: String) -> [String: Any?] {
    let b = Bundle.main
    var sys = utsname()
    uname(&sys)
    var model = withUnsafePointer(to: &sys.machine) {
      $0.withMemoryRebound(to: CChar.self, capacity: 1) { String(cString: $0) }
    }
    #if targetEnvironment(simulator)
    model = ProcessInfo.processInfo.environment["SIMULATOR_MODEL_IDENTIFIER"] ?? model // not the host's "arm64"
    #endif
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
}

/// Asks StoreKit once for AppTransaction.environment and hands the answer to waiters.
private final class AppTransactionCheck: @unchecked Sendable {
  private let lock = NSLock()
  private var started = false
  private var done = false
  private var value: String?
  private var waiters: [(String?) -> Void] = []

  var environment: String? {
    lock.lock()
    defer { lock.unlock() }
    return value
  }

  func start() {
    lock.lock()
    let first = !started
    started = true
    lock.unlock()
    guard first else { return }
    #if targetEnvironment(simulator)
    finish(nil)
    #else
    if #available(iOS 16.0, *) {
      Task.detached(priority: .utility) { [self] in
        do {
          let env: AppStore.Environment
          switch try await AppTransaction.shared {
          case .verified(let t): env = t.environment
          case .unverified(let t, _): env = t.environment
          }
          if env == .production { finish("production") }
          else if env == .sandbox { finish("sandbox") }
          else if env == .xcode { finish("xcode") }
          else { finish(nil) }
        } catch {
          finish(nil) // offline or not signed in: the receipt answer stays
        }
      }
    } else {
      finish(nil)
    }
    #endif
  }

  func wait(seconds: Double, _ callback: @escaping (String?) -> Void) {
    lock.lock()
    if done {
      let v = value
      lock.unlock()
      callback(v)
      return
    }
    waiters.append(callback)
    lock.unlock()
    DispatchQueue.global().asyncAfter(deadline: .now() + seconds) { [self] in
      lock.lock()
      let pending = waiters
      waiters = []
      lock.unlock()
      pending.forEach { $0(nil) }
    }
  }

  private func finish(_ v: String?) {
    lock.lock()
    done = true
    value = v
    let pending = waiters
    waiters = []
    lock.unlock()
    pending.forEach { $0(v) }
  }
}
