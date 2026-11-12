// Studio Pulse iOS plug-in: where this install came from, for the SDK's ctx.install_store.
//
// AppTransaction.environment (iOS 16+, StoreKit 2): production = App Store, sandbox =
// TestFlight, xcode = a development install. It is asynchronous and may need the network,
// so the answer is cached and polled; until it arrives the receipt file name decides
// (sandboxReceipt = TestFlight). appStoreReceiptURL is deprecated from iOS 18 but still
// answers. AppTransaction.shared never shows a sign-in prompt (refresh() would; it is not
// called). Strings are returned with strdup(); C# frees them with StudioPulse_Free.
import Foundation
import StoreKit

private let lock = NSLock()
nonisolated(unsafe) private var cachedEnvironment: String? // guarded by lock
nonisolated(unsafe) private var checkStarted = false // guarded by lock

private func setCached(_ name: String) {
    lock.lock()
    cachedEnvironment = name
    lock.unlock()
}

private func copyString(_ s: String?) -> UnsafeMutablePointer<CChar>? {
    guard let s = s else { return nil }
    return strdup(s)
}

@_cdecl("StudioPulse_Free")
public func StudioPulse_Free(_ p: UnsafeMutablePointer<CChar>?) {
    free(p)
}

@_cdecl("StudioPulse_InstallSource")
public func StudioPulse_InstallSource() -> UnsafeMutablePointer<CChar>? {
    #if targetEnvironment(simulator)
    return copyString("simulator")
    #else
    lock.lock()
    let env = cachedEnvironment
    lock.unlock()
    if let env = env { return copyString(env) }
    guard let url = Bundle.main.appStoreReceiptURL else { return copyString("unknown") }
    return copyString(url.lastPathComponent == "sandboxReceipt" ? "testflight" : "app_store")
    #endif
}

@_cdecl("StudioPulse_BuildNumber")
public func StudioPulse_BuildNumber() -> UnsafeMutablePointer<CChar>? {
    return copyString(Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String)
}

@_cdecl("StudioPulse_StartAppTransactionCheck")
public func StudioPulse_StartAppTransactionCheck() {
    lock.lock()
    let started = checkStarted
    checkStarted = true
    lock.unlock()
    if started { return }
    if #available(iOS 16.0, *) {
        Task.detached(priority: .utility) {
            do {
                let result = try await AppTransaction.shared
                let env: AppStore.Environment
                switch result {
                case .verified(let t): env = t.environment
                case .unverified(let t, _): env = t.environment
                }
                let name: String
                if env == .production { name = "production" }
                else if env == .sandbox { name = "sandbox" }
                else if env == .xcode { name = "xcode" }
                else { name = "unknown" }
                setCached(name)
            } catch {
                // offline or not signed in to the App Store: keep the receipt answer
            }
        }
    }
}

@_cdecl("StudioPulse_AppTransactionEnvironment")
public func StudioPulse_AppTransactionEnvironment() -> UnsafeMutablePointer<CChar>? {
    lock.lock()
    defer { lock.unlock() }
    return copyString(cachedEnvironment)
}
