import AppKit
import UserNotifications
import Darwin

// Keep this policy independent of Notification Center so it can be tested without
// launching an app or changing the user's delivered notifications.
struct ExpiryRecord: Codable, Equatable {
    let generation: String
    var deadline: TimeInterval?
}

enum ExpiryPolicy {
    static let maximumDuration: Double = 86400 * 365

    static func duration(_ value: Any?, kind: String) throws -> Double {
        guard let value = value else { return kind == "done" ? 8 : 0 }
        guard let number = value as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID() else {
            throw NSError(domain: "HerdrNotify", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "duration_seconds must be a finite number between 0 and 31536000"])
        }
        let seconds = number.doubleValue
        guard seconds.isFinite, seconds >= 0, seconds <= maximumDuration else {
            throw NSError(domain: "HerdrNotify", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "duration_seconds must be a finite number between 0 and 31536000"])
        }
        return seconds
    }

    static func expired(_ record: ExpiryRecord, now: TimeInterval) -> Bool {
        record.deadline.map { $0 <= now } ?? false
    }

    static func shouldExpire(_ current: ExpiryRecord?, generation: String, now: TimeInterval) -> Bool {
        guard let current = current, current.generation == generation else { return false }
        return expired(current, now: now)
    }

    // JSON null isn't a property-list value accepted by UNNotificationContent.
    // Missing agent_session still has the same meaning to the Python bridge.
    static func propertyList(_ value: Any) -> Any? {
        if value is NSNull { return nil }
        if let dict = value as? [String: Any] { return dict.compactMapValues { propertyList($0) } }
        if let array = value as? [Any] { return array.compactMap { propertyList($0) } }
        return value
    }
}

#if EXPIRY_TESTS
// Invoked only by a separately compiled test executable; never starts NSApplication.
func check(_ condition: Bool, _ message: String) {
    if !condition { fatalError(message) }
}
check(try ExpiryPolicy.duration(nil, kind: "done") == 8, "done default")
check(try ExpiryPolicy.duration(nil, kind: "blocked") == 0, "blocked default")
for invalid: Any in [-1, Double.infinity, Double.nan, 31536001, true, "8", NSNull()] {
    do { _ = try ExpiryPolicy.duration(invalid, kind: "done"); fatalError("accepted invalid duration") }
    catch { }
}
check(try ExpiryPolicy.duration(0, kind: "done") == 0, "zero disables expiry")
check(try ExpiryPolicy.duration(31536000, kind: "done") == 31536000, "upper boundary")
let old = ExpiryRecord(generation: "old", deadline: 100)
let newer = ExpiryRecord(generation: "new", deadline: 200)
check(ExpiryPolicy.expired(old, now: 100), "deadline inclusive")
check(!ExpiryPolicy.expired(newer, now: 100), "replacement not yet expired")
check(!ExpiryPolicy.shouldExpire(newer, generation: old.generation, now: 300), "old timer cannot remove replacement")
check(!ExpiryPolicy.shouldExpire(nil, generation: old.generation, now: 300), "cancelled timer is harmless")
check(ExpiryPolicy.shouldExpire(newer, generation: newer.generation, now: 300), "current generation expires")
check(!ExpiryPolicy.expired(ExpiryRecord(generation: "blocked", deadline: nil), now: 999), "persistent alert")
let encoded = try JSONEncoder().encode(["id": old])
check(try JSONDecoder().decode([String: ExpiryRecord].self, from: encoded)["id"] == old, "restart recovery")
let route = ExpiryPolicy.propertyList(["agent_session": NSNull(), "nested": [NSNull(), "ok"]]) as! [String: Any]
check(route["agent_session"] == nil, "null removed")
check(PropertyListSerialization.propertyList(route, isValidFor: .binary), "valid userInfo")
print("Expiry policy tests passed")
#else
// All commands, async Notification Center callbacks, timer work and clicks run
// through one main-thread FIFO. A callback cannot race a newer alert with its ID.
final class Helper: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    let center = UNUserNotificationCenter.current()
    let socketQueue = DispatchQueue(label: "herdr.notifications.ipc")
    let replyQueue = DispatchQueue(label: "herdr.notifications.replies", attributes: .concurrent)
    var listener: Int32 = -1
    var lockFD: Int32 = -1
    let kind = Bundle.main.object(forInfoDictionaryKey: "HerdrNotificationKind") as! String
    var stateURL: URL!
    var records: [String: ExpiryRecord] = [:]
    var operations: [(@escaping () -> Void) -> Void] = []
    var busy = false
    var sweepQueued = false
    var timer: Timer?

    func enqueue(_ operation: @escaping (@escaping () -> Void) -> Void) {
        dispatchPrecondition(condition: .onQueue(.main))
        operations.append(operation)
        drain()
    }

    func drain() {
        guard !busy, !operations.isEmpty else { return }
        busy = true
        let operation = operations.removeFirst()
        operation {
            dispatchPrecondition(condition: .onQueue(.main))
            self.busy = false
            self.drain()
        }
    }

    func audit(_ event: String, ids: [String]) {
        let url = stateURL.deletingLastPathComponent().appendingPathComponent("\(kind)-events.log")
        let line = "\(Date().timeIntervalSince1970) \(event) \(ids.joined(separator: ","))\n"
        do {
            var data = (try? Data(contentsOf: url)) ?? Data()
            if data.count > 262144 { data = Data(data.suffix(131072)) }
            data.append(Data(line.utf8))
            try data.write(to: url, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        } catch { NSLog("Herdr audit: %@", error.localizedDescription) }
    }

    func persist() throws {
        try JSONEncoder().encode(records).write(to: stateURL, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: stateURL.path)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // A failed focus child must not terminate the long-lived helper on write.
        signal(SIGPIPE, SIG_IGN)
        center.delegate = self
        let dir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Caches/herdr-native-notify")
        do {
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            let attributes = try FileManager.default.attributesOfItem(atPath: dir.path)
            guard attributes[.type] as? FileAttributeType == .typeDirectory,
                  (attributes[.ownerAccountID] as? NSNumber)?.uint32Value == getuid(),
                  (attributes[.posixPermissions] as? NSNumber)?.intValue == 0o700 else { exit(1) }
            lockFD = Darwin.open(dir.appendingPathComponent("\(kind).lock").path, O_CREAT | O_RDWR | O_NOFOLLOW, 0o600)
            guard lockFD >= 0, flock(lockFD, LOCK_EX | LOCK_NB) == 0 else { exit(0) }
            stateURL = dir.appendingPathComponent("\(kind)-deadlines.json")
            if FileManager.default.fileExists(atPath: stateURL.path) {
                records = try JSONDecoder().decode([String: ExpiryRecord].self, from: Data(contentsOf: stateURL))
            }
        } catch { NSLog("Herdr %@ expiry recovery failed: %@", kind, error.localizedDescription); exit(1) }
        let path = dir.appendingPathComponent("\(kind).sock").path
        guard path.utf8.count < 104 else { exit(1) }
        unlink(path)
        listener = socket(AF_UNIX, SOCK_STREAM, 0)
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        withUnsafeMutableBytes(of: &address.sun_path) { $0.copyBytes(from: Array(path.utf8) + [0]) }
        let result = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.bind(listener, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        }
        guard result == 0, listen(listener, 16) == 0 else { exit(1) }
        chmod(path, 0o600)
        // Wall-clock checks recover overdue deadlines after restart and sleep.
        sweep()
        timer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { _ in self.sweep() }
        NSWorkspace.shared.notificationCenter.addObserver(self, selector: #selector(woke), name: NSWorkspace.didWakeNotification, object: nil)
        socketQueue.async { self.acceptCommands() }
    }

    @objc func woke() { DispatchQueue.main.async { self.sweep() } }

    func acceptCommands() {
        while true {
            let fd = accept(listener, nil, nil)
            if fd < 0 { continue }
            var timeout = timeval(tv_sec: 5, tv_usec: 0)
            setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
            setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
            var noSignal: Int32 = 1
            setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout<Int32>.size))
            var data = Data()
            var byte: UInt8 = 0
            while data.count < 65536 && read(fd, &byte, 1) == 1 && byte != 10 { data.append(byte) }
            guard byte == 10, let request = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { close(fd); continue }
            DispatchQueue.main.async {
                self.enqueue { finished in
                    self.handle(request) { response in
                        // Socket writes never block the state machine/main thread.
                        self.replyQueue.async {
                            var output = (try? JSONSerialization.data(withJSONObject: response)) ?? Data()
                            output.append(10)
                            output.withUnsafeBytes { ptr in
                                var offset = 0
                                while offset < output.count {
                                    let count = write(fd, ptr.baseAddress!.advanced(by: offset), output.count - offset)
                                    if count <= 0 { break }
                                    offset += count
                                }
                            }
                            close(fd)
                        }
                        finished()
                    }
                }
            }
        }
    }

    // Removal has no system completion callback. Confirm absence using both
    // public query APIs, retry briefly, and never claim success while still present.
    func withdraw(_ ids: [String], attempt: Int = 0, completion: @escaping ([String]) -> Void) {
        center.removePendingNotificationRequests(withIdentifiers: ids)
        center.removeDeliveredNotifications(withIdentifiers: ids)
        center.getPendingNotificationRequests { pending in
            self.center.getDeliveredNotifications { delivered in
                DispatchQueue.main.async {
                    let present = Set(pending.map { $0.identifier } + delivered.map { $0.request.identifier })
                    let remaining = ids.filter { present.contains($0) }
                    if !remaining.isEmpty && attempt < 3 {
                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) {
                            self.withdraw(remaining, attempt: attempt + 1, completion: completion)
                        }
                    } else { completion(remaining) }
                }
            }
        }
    }

    func remove(_ ids: [String], reply: @escaping ([String: Any]) -> Void) {
        let prior = records
        let existing = ids.filter { records[$0] != nil }
        if !existing.isEmpty { audit("remove", ids: existing) }
        for id in ids {
            records[id] = ExpiryRecord(generation: records[id]?.generation ?? UUID().uuidString, deadline: Date().timeIntervalSince1970)
        }
        do { try persist() }
        catch { records = prior; reply(["error": "cannot persist removal: \(error.localizedDescription)"]); return }
        withdraw(ids) { remaining in
            for id in ids where !remaining.contains(id) { self.records.removeValue(forKey: id) }
            do {
                try self.persist()
                var response: [String: Any] = ["ok": remaining.isEmpty, "removed": ids.filter { !remaining.contains($0) }, "remaining": remaining]
                if !remaining.isEmpty { response["error"] = "Notification Center has not confirmed removal" }
                reply(response)
            } catch { reply(["error": "removal confirmed but state cleanup failed: \(error.localizedDescription)", "remaining": remaining]) }
        }
    }

    func schedule(_ id: String, record: ExpiryRecord) {
        guard let deadline = record.deadline else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + max(0, deadline - Date().timeIntervalSince1970)) {
            self.enqueue { finished in
                guard ExpiryPolicy.shouldExpire(self.records[id], generation: record.generation,
                                                now: Date().timeIntervalSince1970) else { finished(); return }
                self.audit("expiry", ids: [id])
                self.remove([id]) { response in
                    if let error = response["error"] { NSLog("Herdr expiry: %@", String(describing: error)) }
                    finished()
                }
            }
        }
    }

    func sweep() {
        guard !sweepQueued else { return }
        sweepQueued = true
        enqueue { finished in
            let ids = self.records.filter { ExpiryPolicy.expired($0.value, now: Date().timeIntervalSince1970) }.map { $0.key }
            guard !ids.isEmpty else { self.sweepQueued = false; finished(); return }
            self.remove(ids) { response in
                if let error = response["error"] { NSLog("Herdr %@ expiry: %@", self.kind, String(describing: error)) }
                self.sweepQueued = false
                finished()
            }
        }
    }

    func handle(_ request: [String: Any], reply: @escaping ([String: Any]) -> Void) {
        switch request["op"] as? String {
        case "authorize":
            center.requestAuthorization(options: [.alert, .sound]) { allowed, error in
                DispatchQueue.main.async { reply(["allowed": allowed, "error": error?.localizedDescription ?? ""]) }
            }
        case "status":
            center.getNotificationSettings { settings in
                DispatchQueue.main.async {
                    reply(["authorization": settings.authorizationStatus.rawValue, "style": settings.alertStyle.rawValue,
                           "frontmost": NSWorkspace.shared.frontmostApplication?.bundleIdentifier ?? ""])
                }
            }
        case "list":
            center.getDeliveredNotifications { delivered in
                DispatchQueue.main.async { reply(["ids": delivered.map { $0.request.identifier }]) }
            }
        case "remove":
            guard let ids = request["ids"] as? [String] else { reply(["error": "ids must be an array of strings"]); return }
            remove(Array(Set(ids)), reply: reply)
        case "notify":
            guard let id = request["id"] as? String, !id.isEmpty, let title = request["title"] as? String,
                  let route = request["route"] as? [String: Any] else { reply(["error": "missing notification fields"]); return }
            let duration: Double
            do { duration = try ExpiryPolicy.duration(request["duration_seconds"], kind: kind) }
            catch { reply(["error": error.localizedDescription]); return }
            center.getNotificationSettings { settings in
                DispatchQueue.main.async {
                    guard settings.authorizationStatus == .authorized else { reply(["error": "notifications not authorized"]); return }
                    let content = UNMutableNotificationContent()
                    content.title = title
                    content.subtitle = request["subtitle"] as? String ?? ""
                    content.body = request["body"] as? String ?? ""
                    // Notification Center stacks an app's alerts by thread: one stack per Herdr space.
                    if let thread = request["thread"] as? String { content.threadIdentifier = thread }
                    var info = ExpiryPolicy.propertyList(route) as! [String: Any]
                    let generation = UUID().uuidString
                    info["_herdr_generation"] = generation
                    content.userInfo = info
                    let previous = self.records[id]
                    let deadline = duration == 0 ? nil : Date().timeIntervalSince1970 + duration
                    self.records[id] = ExpiryRecord(generation: generation, deadline: deadline)
                    do { try self.persist() }
                    catch { self.records[id] = previous; reply(["error": "cannot persist notification: \(error.localizedDescription)"]); return }
                    self.center.add(UNNotificationRequest(identifier: id, content: content, trigger: nil)) { error in
                        DispatchQueue.main.async {
                            if let error = error {
                                self.records[id] = previous
                                do { try self.persist() }
                                catch { NSLog("Herdr expiry rollback failed: %@", error.localizedDescription) }
                                reply(["error": error.localizedDescription])
                            } else {
                                self.audit("notify duration=\(duration)", ids: [id])
                                self.schedule(id, record: self.records[id]!)
                                reply(["ok": true, "generation": generation, "duration_seconds": duration])
                            }
                        }
                    }
                }
            }
        default: reply(["error": "unknown operation"])
        }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        completionHandler()
        guard response.actionIdentifier == UNNotificationDefaultActionIdentifier else { return }
        DispatchQueue.main.async {
            self.enqueue { finished in
                let notification = response.notification.request
                NSLog("Herdr %@ clicked notification %@", self.kind, notification.identifier)
                self.audit("click", ids: [notification.identifier])
                let generation = notification.content.userInfo["_herdr_generation"] as? String
                // The user may click an old banner just as its ID is reused.
                // Do not withdraw the replacement or route the obsolete click.
                guard let current = self.records[notification.identifier], current.generation == generation else {
                    NSLog("Herdr ignored superseded click %@", notification.identifier)
                    finished(); return
                }
                self.remove([notification.identifier]) { result in
                    if let error = result["error"] { NSLog("Herdr click removal: %@", String(describing: error)) }
                    self.focus(notification.content.userInfo)
                    finished()
                }
            }
        }
    }

    func focus(_ info: [AnyHashable: Any]) {
        var route = info
        route.removeValue(forKey: "_herdr_generation")
        guard let data = try? JSONSerialization.data(withJSONObject: route),
              let script = Bundle.main.path(forResource: "bridge", ofType: "py") else {
            NSLog("Herdr click failed: invalid route or missing embedded bridge"); return
        }
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        task.arguments = [script, "focus"]
        let input = Pipe()
        let output = Pipe()
        task.standardInput = input
        task.standardOutput = output
        task.standardError = output
        output.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            NSLog("Herdr focus child: %@", String(decoding: data, as: UTF8.self))
        }
        task.terminationHandler = { process in
            NSLog("Herdr focus child exited status %d (reason %ld)", process.terminationStatus, process.terminationReason.rawValue)
        }
        do {
            try task.run()
            try input.fileHandleForWriting.write(contentsOf: data)
            try input.fileHandleForWriting.close()
        } catch {
            try? input.fileHandleForWriting.close()
            if !task.isRunning { output.fileHandleForReading.readabilityHandler = nil }
            NSLog("Herdr notification click failed: %@", error.localizedDescription)
        }
    }
}
let app = NSApplication.shared
let delegate = Helper()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
#endif
