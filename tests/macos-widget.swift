import AppKit
import Foundation
import CoreFoundation
import ImageIO

// URLProtocol callbacks run off the main actor; all shared fixture state is locked.
private final class PollFixture: @unchecked Sendable {
    struct Reply: Sendable {
        var body = "{\"state\":\"red\"}"
        var statusCode = 200
        var delay: TimeInterval = 0
        var error: URLError.Code?
        var isHTTP = true
    }

    private let lock = NSLock()
    private var reply = Reply()
    private var requests: [URLRequest] = []
    private var active = 0
    private var peak = 0
    private var completed = 0
    private var cancelled = 0

    func set(_ reply: Reply) {
        lock.lock()
        defer { lock.unlock() }
        self.reply = reply
    }

    func begin(_ request: URLRequest) -> Reply {
        lock.lock()
        defer { lock.unlock() }
        requests.append(request)
        active += 1
        peak = max(peak, active)
        return reply
    }

    func end(cancelled: Bool) {
        lock.lock()
        defer { lock.unlock() }
        active -= 1
        if cancelled { self.cancelled += 1 } else { completed += 1 }
    }

    var snapshot: (requests: [URLRequest], active: Int, peak: Int, completed: Int, cancelled: Int) {
        lock.lock()
        defer { lock.unlock() }
        return (requests, active, peak, completed, cancelled)
    }
}

private final class StubURLProtocol: URLProtocol, @unchecked Sendable {
    static let fixture = PollFixture()
    private let lock = NSLock()
    private var finished = false

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let reply = Self.fixture.begin(request)
        DispatchQueue.global().asyncAfter(deadline: .now() + reply.delay) { [self] in
            lock.lock()
            let shouldFinish = !finished
            finished = true
            lock.unlock()
            guard shouldFinish else { return }
            Self.fixture.end(cancelled: false)
            if let error = reply.error {
                client?.urlProtocol(self, didFailWithError: URLError(error))
            } else {
                let response: URLResponse
                if reply.isHTTP {
                    response = HTTPURLResponse(url: request.url!, statusCode: reply.statusCode,
                                               httpVersion: "HTTP/1.1", headerFields: nil)!
                } else {
                    response = URLResponse(url: request.url!, mimeType: "application/json",
                                           expectedContentLength: -1, textEncodingName: nil)
                }
                client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
                client?.urlProtocol(self, didLoad: Data(reply.body.utf8))
                client?.urlProtocolDidFinishLoading(self)
            }
        }
    }

    override func stopLoading() {
        lock.lock()
        defer { lock.unlock() }
        guard !finished else { return }
        finished = true
        Self.fixture.end(cancelled: true)
    }
}

@MainActor
private final class TestClock {
    var now: TimeInterval = 100
}

@MainActor
private final class TestInput {
    var pointer = CGPoint.zero
    var reducedMotion = false
    var commits: [CGFloat] = []
    var closes = 0
    var eventNumber = 0
}

@main
enum WidgetTests {
    static func testDecoding() {
        for state in TrafficState.allCases {
            for code in [200, 201, 299] {
                assert(TrafficState.decode(Data("{\"state\":\"\(state.rawValue)\"}".utf8),
                                           statusCode: code) == state)
            }
        }
        for body in ["", "not json", "{", "{}", "null", "[]", "\"red\"",
                     "{\"state\":null}", "{\"state\":true}", "{\"state\":1}",
                     "{\"state\":{}}", "{\"state\":[]}", "{\"state\":\"blue\"}",
                     "{\"state\":\"RED\"}", "{\"state\":\" green\"}", "{\"state\":\"red \"}"] {
            assert(TrafficState.decode(Data(body.utf8), statusCode: 200) == nil, body)
        }
        for code in [0, 199, 300, 301, 404, 500] {
            assert(TrafficState.decode(Data("{\"state\":\"red\"}".utf8), statusCode: code) == nil)
        }
        assert(TrafficState.decode(Data([0xFF]), statusCode: 200) == nil)
        assert(TrafficState.decode(Data("{\"state\":\"yellow\",\"extra\":42}".utf8),
                                   statusCode: 200) == .yellow)
        print("PASS: strict state decoding and HTTP status validation")
    }

    static func testGeometry() {
        let bounds = CGRect(x: 0, y: 40, width: 1440, height: 836)
        let initial = WidgetGeometry.initial(in: bounds)
        assert(initial.frame == CGRect(x: 689.5, y: 852, width: 61, height: 24))
        assert(initial.orientation == .horizontal && !initial.expanded)
        assert(WidgetOrientation.horizontal.size() == CGSize(width: 61, height: 24))
        assert(WidgetOrientation.horizontal.size(expanded: true) == CGSize(width: 84, height: 24))
        assert(WidgetOrientation.left.size() == CGSize(width: 24, height: 61))
        assert(WidgetOrientation.left.size(expanded: true) == CGSize(width: 24, height: 84))
        assert(WidgetOrientation.right.size() == WidgetOrientation.left.size())
        assert(WidgetOrientation.right.size(expanded: true) == WidgetOrientation.left.size(expanded: true))
        assert(TrafficState.allCases == [.red, .yellow, .green])
        assert(WidgetDrawing.lamps(expanded: false) == [.red, .yellow, .green])
        assert(WidgetDrawing.lamps(expanded: true) == [.red, .yellow, .green])
        assert(WidgetOrientation.horizontal.lampCenters == [CGPoint(x: 12, y: 12),
                                                             CGPoint(x: 30.5, y: 12),
                                                             CGPoint(x: 49, y: 12)])
        assert(WidgetOrientation.left.lampCenters == [CGPoint(x: 12, y: 12),
                                                     CGPoint(x: 12, y: 30.5),
                                                     CGPoint(x: 12, y: 49)])
        assert(WidgetOrientation.right.lampCenters == WidgetOrientation.left.lampCenters)
        for orientation in [WidgetOrientation.horizontal, .left, .right] {
            let button = orientation.closeButtonFrame
            let expected = orientation == .horizontal ? CGRect(x: 61, y: 0, width: 19, height: 24)
                                                      : CGRect(x: 0, y: 61, width: 24, height: 19)
            assert(button == expected)
            assert(CGRect(origin: .zero, size: orientation.size(expanded: true)).contains(button))
            assert(!CGRect(origin: .zero, size: orientation.size()).intersects(button))
            for center in orientation.lampCenters {
                assert(!button.contains(center))
                assert(!button.intersects(CGRect(x: center.x - 7.5, y: center.y - 7.5,
                                                 width: 15, height: 15)))
            }
        }

        for screen in [bounds, CGRect(x: -1920, y: -1000, width: 1920, height: 1040),
                       CGRect(x: 1500, y: 1000, width: 1200, height: 800)] {
            let y = screen.midY
            let left = WidgetGeometry.dragged(center: CGPoint(x: screen.minX + 80, y: y), in: screen)
            assert(left.orientation == .left && left.frame.minX == screen.minX)
            let right = WidgetGeometry.dragged(center: CGPoint(x: screen.maxX - 80, y: y), in: screen)
            assert(right.orientation == .right && right.frame.maxX == screen.maxX)
            for x in [screen.minX + 80.01, screen.midX, screen.maxX - 80.01] {
                assert(WidgetGeometry.dragged(center: CGPoint(x: x, y: y), in: screen).orientation == .horizontal)
            }
            for point in [CGPoint(x: screen.minX - 500, y: screen.minY - 500),
                          CGPoint(x: screen.maxX + 500, y: screen.maxY + 500),
                          CGPoint(x: screen.midX, y: screen.maxY + 500)] {
                for orientation in [WidgetOrientation.horizontal, .left, .right] {
                    for expanded in [false, true] {
                        let placement = WidgetGeometry.placement(center: point, orientation: orientation,
                                                                  expanded: expanded, in: screen)
                        assert(screen.contains(placement.frame))
                        assert(placement.frame.size == orientation.size(expanded: expanded))
                        assert(placement.expanded == expanded)
                    }
                }
            }
            let drag = WidgetDrag(pointer: CGPoint(x: screen.midX + 20, y: y + 5),
                                  frame: CGRect(x: screen.midX - 30.5, y: y - 12, width: 61, height: 24))
            for (x, expected) in [(screen.minX + 99, WidgetOrientation.left),
                                  (screen.minX + 101, .horizontal), (screen.minX + 99, .left),
                                  (screen.midX, .horizontal), (screen.maxX - 61, .horizontal),
                                  (screen.maxX - 59, .right), (screen.midX, .horizontal)] {
                let pointer = CGPoint(x: x, y: y + 25)
                let center = drag.center(at: pointer)
                assert(center == CGPoint(x: x - 20, y: y + 20))
                for expanded in [false, true, false, true] {
                    let placement = WidgetGeometry.dragged(center: center, expanded: expanded, in: screen)
                    assert(placement.orientation == expected)
                    assert(screen.contains(placement.frame))
                    assert(placement.frame.size == expected.size(expanded: expanded))
                }
            }
            for snapped in [left, right] {
                let pointer = CGPoint(x: snapped.frame.midX + 5, y: snapped.frame.midY + 20)
                let sideDrag = WidgetDrag(pointer: pointer, frame: snapped.frame)
                let moved = CGPoint(x: screen.midX, y: pointer.y + 10)
                let unsnapped = WidgetGeometry.dragged(center: sideDrag.center(at: moved), in: screen)
                assert(unsnapped.orientation == .horizontal)
                assert(unsnapped.frame.midX == moved.x - 5)
                assert(unsnapped.frame.midY == snapped.frame.midY + 10)
            }

            for orientation in [WidgetOrientation.horizontal, .left, .right] {
                // Resize must not re-snap even when a horizontal widget is inside the snap zone.
                let frame = CGRect(x: screen.minX + 30, y: screen.midY, width: orientation.size().width,
                                   height: orientation.size().height)
                let expanded = WidgetGeometry.resized(frame: frame, orientation: orientation,
                                                       expanded: true, in: screen)
                assert(expanded.orientation == orientation && expanded.expanded)
                assert(expanded.frame.minX == frame.minX && expanded.frame.maxY == frame.maxY)
                assert(expanded.frame.size == orientation.size(expanded: true))
                let collapsed = WidgetGeometry.resized(frame: expanded.frame, orientation: orientation,
                                                        expanded: false, in: screen)
                assert(collapsed.frame == frame && !collapsed.expanded)
                for point in [CGPoint(x: screen.maxX - frame.width, y: screen.minY),
                              CGPoint(x: screen.minX - 100, y: screen.maxY + 100)] {
                    let edge = CGRect(origin: point, size: frame.size)
                    let clamped = WidgetGeometry.resized(frame: edge, orientation: orientation,
                                                         expanded: true, in: screen)
                    assert(screen.contains(clamped.frame) && clamped.orientation == orientation)
                    assert(clamped.frame.size == orientation.size(expanded: true))
                    assert(WidgetGeometry.resized(frame: clamped.frame, orientation: orientation,
                                                  expanded: true, in: screen) == clamped)
                    if point.y == screen.minY {
                        assert(clamped.frame.minY == screen.minY)
                        assert(clamped.frame.maxX == screen.maxX)
                    }
                }
            }
        }

        let displays = [CGRect(x: 0, y: 0, width: 1440, height: 900),
                        CGRect(x: -1920, y: -200, width: 1920, height: 1080),
                        CGRect(x: 100, y: 1000, width: 1200, height: 800)]
        assert(WidgetGeometry.screenIndex(at: CGPoint(x: 10, y: 10), frames: displays) == 0)
        assert(WidgetGeometry.screenIndex(at: CGPoint(x: -10, y: 10), frames: displays) == 1)
        assert(WidgetGeometry.screenIndex(at: CGPoint(x: 100, y: 1000), frames: displays) == 2)
        assert(WidgetGeometry.screenIndex(at: CGPoint(x: 500, y: 980), frames: displays) == 2)
        assert(WidgetGeometry.screenIndex(at: CGPoint(x: -1900, y: 0), frames: [displays[0]]) == 0)
        assert(WidgetGeometry.screenIndex(at: .zero, frames: []) == nil)
        let crossDisplayDrag = WidgetDrag(pointer: CGPoint(x: 700, y: 450),
                                           frame: CGRect(x: 689.5, y: 438, width: 61, height: 24))
        for (pointer, expected) in [(CGPoint(x: -5, y: 450), WidgetOrientation.right),
                                    (CGPoint(x: -200, y: 500), .horizontal),
                                    (CGPoint(x: 5, y: 450), .left),
                                    (CGPoint(x: 200, y: 450), .horizontal)] {
            let index = WidgetGeometry.screenIndex(at: pointer, frames: displays)!
            let placement = WidgetGeometry.dragged(center: crossDisplayDrag.center(at: pointer),
                                                    in: displays[index])
            assert(placement.orientation == expected && displays[index].contains(placement.frame))
        }
        let recovered = WidgetGeometry.placement(center: CGPoint(x: -1900, y: -500),
                                                  orientation: .right, in: bounds)
        assert(bounds.contains(recovered.frame) && recovered.frame.maxX == bounds.maxX)
        print("PASS: dimensions, lamps, close-control geometry, snapping, drag anchors, displays, and clamping")
    }

    static func testPalette() {
        // Literal reference colors, independent of production drawing constants.
        for (state, core, ring) in [(TrafficState.red, [0xC7, 0x44, 0x30], [0x51, 0x29, 0x1D]),
                                    (.yellow, [0xC5, 0xCD, 0x2D], [0x50, 0x55, 0x1B]),
                                    (.green, [0x2D, 0xC8, 0x4F], [0x1B, 0x4F, 0x2B])] {
            for alpha: CGFloat in [1, 0.15] {
                let expected = core.map { CGFloat($0) / 255 } + [alpha]
                let actual = state.color(alpha: alpha).components!
                assert(actual.count == 4)
                for (component, target) in zip(actual, expected) {
                    assert(abs(component - target) < 0.000001)
                }
            }
            let actual = state.ringColor.components!
            assert(actual.count == 4)
            for (component, target) in zip(actual, ring.map { CGFloat($0) / 255 } + [1]) {
                assert(abs(component - target) < 0.000001)
            }
        }
        print("PASS: literal sRGB core/ring palette and active/inactive alpha")
    }

    static func testScaleAndPreferences() throws {
        let bounds = CGRect(x: -500, y: 40, width: 1440, height: 836)
        for scale: CGFloat in [0.75, 1, 1.137, 2] {
            assert(WidgetPreferences.validated(scale) == scale)
            let initial = WidgetGeometry.initial(scale: scale, in: bounds)
            assert(initial.frame.maxY == bounds.maxY && initial.scale == scale)
            for orientation in [WidgetOrientation.horizontal, .left, .right] {
                let closed = orientation.size(scale: scale)
                let open = orientation.size(expanded: true, scale: scale)
                assert(closed == (orientation == .horizontal ? CGSize(width: 61 * scale, height: 24 * scale)
                                                            : CGSize(width: 24 * scale, height: 61 * scale)))
                assert(open == (orientation == .horizontal ? CGSize(width: 84 * scale, height: 24 * scale)
                                                          : CGSize(width: 24 * scale, height: 84 * scale)))
                let close = orientation.controlFrame(resize: false, scale: scale)
                let grip = orientation.controlFrame(resize: true, scale: scale)
                assert(close.intersects(grip)) // Bounds overlap; actual targets must not.
                assert(CGRect(origin: .zero, size: open).insetBy(dx: -0.000001, dy: -0.000001).contains(grip))
                assert(orientation == .horizontal ? grip == CGRect(x: 75 * scale, y: 0, width: 9 * scale, height: 24 * scale)
                       : grip == CGRect(x: 0, y: 75 * scale, width: 24 * scale, height: 9 * scale))
                for long in stride(from: CGFloat(60.25), through: 84.25, by: 0.5) {
                    for short in stride(from: CGFloat(0.25), through: 24.25, by: 0.5) {
                        let point = orientation == .horizontal ? CGPoint(x: long * scale, y: short * scale)
                                                              : CGPoint(x: short * scale, y: long * scale)
                        let isClose = hypot(long - 70.5, short - 12) <= 6
                        let isResize = long >= 75 && hypot(long - 72, short - 12) <= 12
                            && hypot(long - 70.5, short - 12) >= 8
                        assert(orientation.controlContains(point, resize: false, scale: scale) == isClose)
                        assert(orientation.controlContains(point, resize: true, scale: scale) == isResize)
                    }
                }
                let cursorRects = orientation.resizeCursorRects(scale: scale)
                assert(!cursorRects.isEmpty)
                let center = orientation == .horizontal ? CGPoint(x: 70.5 * scale, y: 12 * scale)
                                                        : CGPoint(x: 12 * scale, y: 70.5 * scale)
                for rect in cursorRects {
                    let nearest = CGPoint(x: max(rect.minX, min(center.x, rect.maxX)),
                                          y: max(rect.minY, min(center.y, rect.maxY)))
                    assert(hypot(nearest.x - center.x, nearest.y - center.y) > 8 * scale)
                    assert(rect.width > 0 && rect.height > 0)
                    assert(grip.insetBy(dx: -0.000001, dy: -0.000001).contains(rect))
                    for corner in [CGPoint(x: rect.minX, y: rect.minY), CGPoint(x: rect.maxX, y: rect.minY),
                                   CGPoint(x: rect.minX, y: rect.maxY), CGPoint(x: rect.maxX, y: rect.maxY)] {
                        let long = (orientation == .horizontal ? corner.x : corner.y) / scale
                        let short = (orientation == .horizontal ? corner.y : corner.x) / scale
                        assert(long >= 75 - 0.000001 && hypot(long - 72, short - 12) <= 12.000001)
                    }
                }
                for degrees in stride(from: CGFloat(0), to: 360, by: 15) {
                    let angle = degrees * .pi / 180
                    let long = (70.5 + 7 * cos(angle)) * scale
                    let short = (12 + 7 * sin(angle)) * scale
                    let gap = orientation == .horizontal ? CGPoint(x: long, y: short) : CGPoint(x: short, y: long)
                    assert(!orientation.controlContains(gap, resize: false, scale: scale))
                    assert(!orientation.controlContains(gap, resize: true, scale: scale))
                    assert(!cursorRects.contains { $0.contains(gap) })
                }
                let start = WidgetGeometry.placement(center: CGPoint(x: bounds.midX, y: bounds.midY),
                                                     orientation: orientation, expanded: true, in: bounds)
                let resized = WidgetGeometry.resized(frame: start.frame, orientation: orientation,
                                                     expanded: true, scale: scale, in: bounds)
                assert(resized.orientation == orientation && bounds.contains(resized.frame))
                assert(resized.frame.maxY == start.frame.maxY)
                if orientation == .right { assert(resized.frame.maxX == bounds.maxX) }
                else { assert(resized.frame.minX == start.frame.minX) }
                for reveal: CGFloat in [0, 0.25, 0.5, 1] {
                    let frame = WidgetGeometry.resized(frame: start.frame, orientation: orientation,
                                                       expanded: true, scale: scale, reveal: reveal, in: bounds).frame
                    assert(bounds.contains(frame))
                    assert(frame.size == orientation.size(expanded: true, scale: scale, reveal: reveal))
                }
                for corner in [CGPoint(x: bounds.maxX - start.frame.width, y: bounds.minY),
                               CGPoint(x: bounds.minX - 100, y: bounds.maxY + 100)] {
                    let edge = CGRect(origin: corner, size: start.frame.size)
                    let clamped = WidgetGeometry.resized(frame: edge, orientation: orientation,
                                                         expanded: true, scale: scale, in: bounds)
                    assert(bounds.insetBy(dx: -0.000001, dy: -0.000001).contains(clamped.frame))
                    assert(clamped.orientation == orientation && clamped.scale == scale)
                }
                let gesture = WidgetResize(pointer: .zero, frame: start.frame, orientation: orientation,
                                            initialScale: scale)
                for delta: CGFloat in [-1000, -12, 0, 12, 1000] {
                    let point = orientation == .horizontal ? CGPoint(x: delta, y: 999) : CGPoint(x: 999, y: -delta)
                    assert(gesture.scale(at: point) == max(0.75, min(2, scale + delta / 84)))
                }
            }
        }
        for scale: CGFloat in [0, -1, 0.749, 2.001, .infinity, -.infinity, .nan] {
            assert(WidgetPreferences.validated(scale) == 1)
        }
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("widget-preferences-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("nested/preferences.json")
        assert(WidgetPreferences.load(from: url) == 1)
        assert(WidgetPreferences.url(environment: ["XDG_STATE_HOME": directory.path], home: directory)
               == directory.appendingPathComponent("opencode-traffic-light/preferences.json"))
        for environment in [[:], ["XDG_STATE_HOME": ""]] {
            assert(WidgetPreferences.url(environment: environment, home: directory)
                   == directory.appendingPathComponent(".local/state/opencode-traffic-light/preferences.json"))
        }
        for scale: CGFloat in [0.75, 1.137, 2, 1] {
            WidgetPreferences.save(scale, to: url)
            assert(WidgetPreferences.load(from: url) == scale)
            let json = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as! [String: Double]
            assert(json == ["scale": Double(scale)])
            let entries = try FileManager.default.contentsOfDirectory(atPath: url.deletingLastPathComponent().path)
            assert(entries == ["preferences.json"])
        }
        for body in ["", "{", "null", "[]", "{}", "{\"scale\":true}", "{\"scale\":false}",
                     "{\"scale\":null}", "{\"scale\":\"1.5\"}", "{\"scale\":[]}", "{\"scale\":{}}",
                     "{\"scale\":0.749}", "{\"scale\":2.001}", "{\"scale\":NaN}", "{\"scale\":1e999}"] {
            try Data(body.utf8).write(to: url)
            assert(WidgetPreferences.load(from: url) == 1, body)
        }
        WidgetPreferences.save(1.5, to: url)
        for scale: CGFloat in [.nan, .infinity, 0.5, 3] { WidgetPreferences.save(scale, to: url) }
        assert(WidgetPreferences.load(from: url) == 1.5)
        // Deterministic write failures, even when tests run with elevated filesystem permissions.
        WidgetPreferences.save(2, to: url.appendingPathComponent("cannot-create/preferences.json"))
        WidgetPreferences.save(2, to: directory)
        assert(WidgetPreferences.load(from: url) == 1.5)
        print("PASS: scaled 84-point geometry, two-point neutral gap and cursor exclusion, resize sensitivity, anchors and isolated preferences")
    }

    static func testTweens() {
        var tween = WidgetTween(0)
        tween.retarget(1, at: 0, duration: 0.150, animated: true)
        assert(tween.isAnimating && tween.value == 0)
        tween.advance(at: -1)
        assert(tween.value == 0)
        tween.advance(at: 0.0375)
        assert(abs(tween.value - 0.15625) < 0.000001)
        tween.advance(at: 0.075)
        assert(abs(tween.value - 0.5) < 0.000001)
        tween.retarget(0, at: 0.075, duration: 0.150, animated: true)
        assert(abs(tween.value - 0.5) < 0.000001)
        tween.advance(at: 0.150)
        assert(abs(tween.value - 0.25) < 0.000001)
        tween.retarget(1, at: 0.150, duration: 0.150, animated: true)
        for time in stride(from: 0.150, through: 0.310, by: 0.001) {
            tween.advance(at: time)
            assert((0.25...1).contains(tween.value))
        }
        assert(tween.value == 1 && !tween.isAnimating)
        tween.retarget(0, at: 1, duration: 0.150, animated: false)
        assert(tween.value == 0 && !tween.isAnimating)
        tween.retarget(1, at: 2, duration: 0.150, animated: true)
        tween.advance(at: 2.01, reducedMotion: true)
        assert(tween.value == 1 && !tween.isAnimating)
        tween.retarget(1, at: 3, duration: 0.150, animated: true)
        assert(!tween.isAnimating)
        var lamp = WidgetTween(0.15)
        lamp.retarget(1, at: 0, duration: 0.120, animated: true)
        lamp.advance(at: 0.060)
        assert(abs(lamp.value - 0.575) < 0.000001)
        lamp.retarget(0.15, at: 0.060, duration: 0.120, animated: true)
        lamp.advance(at: 0.120)
        assert(abs(lamp.value - 0.3625) < 0.000001)
        lamp.advance(at: 1)
        assert(lamp.value == 0.15 && !lamp.isAnimating)
        print("PASS: smoothstep start/mid/end, bounded reversal/retarget, 120ms lamp fade, reduced motion and idle tweens")
    }

    static func render(orientation: WidgetOrientation, expanded: Bool, state: TrafficState = .green,
                       scale: Int, widgetScale: CGFloat = 1, reveal: CGFloat? = nil) -> CGContext {
        let size = orientation.size(expanded: expanded, scale: widgetScale, reveal: reveal)
        let context = CGContext(data: nil, width: Int(ceil(size.width * CGFloat(scale))),
                                height: Int(ceil(size.height * CGFloat(scale))),
                                bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
                                    | CGBitmapInfo.byteOrder32Big.rawValue)!
        context.translateBy(x: 0, y: CGFloat(context.height))
        context.scaleBy(x: CGFloat(scale), y: -CGFloat(scale))
        WidgetDrawing.draw(in: context, orientation: orientation, expanded: expanded, state: state,
                           scale: widgetScale, size: size)
        if expanded {
            for resize in [false, true] {
                let button = orientation.controlFrame(resize: resize, scale: widgetScale)
                context.saveGState()
                context.addPath(CGPath(roundedRect: CGRect(origin: .zero, size: size), cornerWidth: 12 * widgetScale,
                                      cornerHeight: 12 * widgetScale, transform: nil))
                context.clip()
                context.setAlpha(reveal ?? 1)
                context.translateBy(x: button.minX, y: button.minY)
                if resize {
                    WidgetDrawing.drawResize(in: context, bounds: CGRect(origin: .zero, size: button.size),
                                             orientation: orientation, scale: widgetScale)
                } else {
                    WidgetDrawing.drawClose(in: context, bounds: CGRect(origin: .zero, size: button.size), scale: widgetScale)
                }
                context.restoreGState()
            }
        }
        return context
    }

    static func testRaster() {
        let body = [0x11, 0x11, 0x13, 255]
        let cores: [TrafficState: [Int]] = [.red: [199, 68, 48, 255], .yellow: [197, 205, 45, 255],
                                          .green: [45, 200, 79, 255]]
        let rings: [TrafficState: [Int]] = [.red: [81, 41, 29, 255], .yellow: [80, 85, 27, 255],
                                          .green: [27, 79, 43, 255]]
        let inactive: [TrafficState: [Int]] = [.red: [99, 45, 32, 255], .yellow: [98, 103, 30, 255],
                                             .green: [30, 97, 48, 255]]
        let scale = 20
        for orientation in [WidgetOrientation.horizontal, .left, .right] {
            for expanded in [false, true] {
                for state in TrafficState.allCases {
                    let context = render(orientation: orientation, expanded: expanded, state: state, scale: scale)
                    let bytes = context.data!.assumingMemoryBound(to: UInt8.self)
                    func pixel(_ long: CGFloat, _ short: CGFloat, _ expected: [Int], tolerance: Int = 1) {
                        let x = Int((orientation == .horizontal ? long : short) * CGFloat(scale))
                        let y = Int((orientation == .horizontal ? short : long) * CGFloat(scale))
                        let offset = y * context.bytesPerRow + x * 4
                        let actual = (0..<4).map { Int(bytes[offset + $0]) }
                        assert(zip(actual, expected).allSatisfy { abs($0 - $1) <= tolerance },
                               "\(orientation) expanded=\(expanded) state=\(state) at \(long),\(short): \(actual) != \(expected)")
                    }
                    let length: CGFloat = expanded ? 84 : 61
                    pixel(0.5, 0.5, [0, 0, 0, 0])
                    pixel(0.5, 23.5, [0, 0, 0, 0])
                    pixel(length - 0.5, 0.5, [0, 0, 0, 0])
                    pixel(length - 0.5, 23.5, [0, 0, 0, 0])
                    pixel(3, 3, [0, 0, 0, 0])
                    pixel(4, 4, body)
                    pixel(0.5, 12, body)
                    pixel(length - 0.5, 12, body)
                    pixel(30.5, 2, body)
                    // Literal slot order and center positions, not the production helpers.
                    let order: [TrafficState] = [.red, .yellow, .green]
                    for (lamp, center) in zip(order, [CGFloat(12), 30.5, 49]) {
                        let core = (lamp == state ? cores : inactive)[lamp]!
                        pixel(center, 12, core)
                        pixel(center, 17, core)
                        pixel(center, 17.5, rings[lamp]!)
                        pixel(center, 19.2, rings[lamp]!)
                        pixel(center, 19.8, body)
                    }
                    if expanded {
                        pixel(70.5, 12, [0, 0, 0, 255])
                        pixel(70.5, 17, [255, 255, 255, 255])
                        pixel(70.5, 17.5, body)
                        pixel(72.1, 13.6, [0, 0, 0, 255])
                        pixel(72.5, 14, [255, 255, 255, 255]) // Butt end, no rounded extension.
                        pixel(71.5, 13.3, [0, 0, 0, 255])
                        pixel(71.5, 13.65, [255, 255, 255, 255]) // 0.65-point stroke.
                        pixel(70.5, 15, [255, 255, 255, 255])
                        pixel(62, 12, body)
                        pixel(82, 12, [140, 140, 145, 255])
                        pixel(81.35, 12, [140, 140, 145, 255]) // 1.5-point stroke.
                        pixel(82.65, 12, [140, 140, 145, 255])
                        pixel(81.1, 12, body)
                        pixel(82.9, 12, body)
                        pixel(78.5, 12, body)
                        pixel(68.6, 2, body)
                        pixel(68.6, 22, body)
                    }
                }
            }
        }
        // Fractional sizes and animation frames must round the current capsule, not clip a final-size body.
        for orientation in [WidgetOrientation.horizontal, .left, .right] {
            for widgetScale: CGFloat in [0.75, 1, 1.137, 2] {
                for reveal: CGFloat in [0, 0.25, 0.5, 0.75, 1] {
                    let context = render(orientation: orientation, expanded: true, scale: scale,
                                         widgetScale: widgetScale, reveal: reveal)
                    let bytes = context.data!.assumingMemoryBound(to: UInt8.self)
                    func pixel(_ long: CGFloat, _ short: CGFloat) -> [Int] {
                        let x = Int((orientation == .horizontal ? long : short) * widgetScale * CGFloat(scale))
                        let y = Int((orientation == .horizontal ? short : long) * widgetScale * CGFloat(scale))
                        let offset = y * context.bytesPerRow + x * 4
                        return (0..<4).map { Int(bytes[offset + $0]) }
                    }
                    let length = 61 + 23 * reveal
                    assert(pixel(length - 0.5, 0.5) == [0, 0, 0, 0])
                    assert(pixel(length - 0.5, 23.5) == [0, 0, 0, 0])
                    assert(pixel(length - 0.5, 12)[3] == 255)
                    for (lamp, center) in zip([TrafficState.red, .yellow, .green], [CGFloat(12), 30.5, 49]) {
                        let expected = (lamp == .green ? cores : inactive)[lamp]!
                        assert(zip(pixel(center, 12), expected).allSatisfy { abs($0 - $1) <= 1 })
                    }
                    if reveal == 1 {
                        for (long, short) in [(CGFloat(82), CGFloat(12)), (79.5, 6), (79.5, 18), (81.35, 12), (82.65, 12)] {
                            assert(zip(pixel(long, short), [140, 140, 145, 255]).allSatisfy { abs($0 - $1) <= 1 })
                        }
                        for sign: CGFloat in [-1, 1] {
                            let angle = sign * 70 * .pi / 180
                            let end = CGPoint(x: 73 + 9 * cos(angle), y: 12 + 9 * sin(angle))
                            // Sample just inside/outside the tangent at each exact endpoint.
                            let inward = CGPoint(x: sin(abs(angle)), y: -sign * cos(angle))
                            let cap = pixel(end.x + 0.2 * inward.x, end.y + 0.2 * inward.y)
                            assert(zip(cap, [140, 140, 145, 255]).allSatisfy { abs($0 - $1) <= 1 })
                            assert(pixel(end.x - 0.2 * inward.x, end.y - 0.2 * inward.y) == body)
                            let oldAngle = sign * 80 * .pi / 180
                            assert(pixel(73 + 9 * cos(oldAngle), 12 + 9 * sin(oldAngle)) == body)
                        }
                        assert(pixel(70.5, 15) == [255, 255, 255, 255])
                        assert(pixel(68.2, 2) == body && pixel(68.2, 22) == body)
                    }
                }
            }
        }
        print("PASS: fixed-order/15%-inactive raster, animated capsule caps, white Close and smaller 140-degree grey arc with 1.5-point butt stroke")
    }

    @MainActor static func renderPreviews(to directory: String) {
        let destination = URL(fileURLWithPath: directory, isDirectory: true)
        var isDirectory: ObjCBool = false
        precondition(FileManager.default.fileExists(atPath: destination.path, isDirectory: &isDirectory)
                     && isDirectory.boolValue, "Preview directory must already exist")
        for (name, orientation) in [("horizontal", WidgetOrientation.horizontal), ("vertical", .left)] {
            for expanded in [false, true] {
                for scale in [1, 5] {
                    let context = render(orientation: orientation, expanded: expanded, scale: scale)
                    let url = destination.appendingPathComponent("\(name)-\(expanded ? "expanded" : "collapsed")-\(scale)x.png")
                    let output = CGImageDestinationCreateWithURL(url as CFURL, "public.png" as CFString, 1, nil)!
                    CGImageDestinationAddImage(output, context.makeImage()!, nil)
                    precondition(CGImageDestinationFinalize(output), "Could not write \(url.path)")
                    print(url.path)
                }
            }
        }
        assert(NSApp == nil && StubURLProtocol.fixture.snapshot.requests.isEmpty)
        print("Exported eight green-state PNGs; no application, window, service, or poller started")
    }

    @MainActor static func waitUntil(_ message: String, _ condition: () -> Bool) async {
        let deadline = ProcessInfo.processInfo.systemUptime + 4
        while !condition() {
            assert(ProcessInfo.processInfo.systemUptime < deadline, "Timed out: \(message)")
            try? await Task.sleep(nanoseconds: 10_000_000)
        }
    }

    @MainActor static func testPolling() async {
        let fixture = StubURLProtocol.fixture
        fixture.set(.init(delay: 0.65))
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubURLProtocol.self]
        var changes: [TrafficState] = []
        let poller = StatusPoller(configuration: configuration, onDisconnect: {
            assertionFailure("Unexpected disconnect during polling tests")
        }) { state in
            assert(Thread.isMainThread)
            changes.append(state)
        }
        assert(poller.state == .green)
        assert(StatusPoller.interval == 0.4)
        assert(configuration.urlCache == nil)
        assert(configuration.requestCachePolicy == .reloadIgnoringLocalCacheData)
        assert(configuration.timeoutIntervalForRequest == 1)
        assert(configuration.timeoutIntervalForResource == 1)
        poller.start()
        poller.start()
        await waitUntil("first request") { fixture.snapshot.requests.count == 1 }
        for _ in 0..<10 { poller.poll() }
        try? await Task.sleep(nanoseconds: 450_000_000)
        assert(fixture.snapshot.requests.count == 1)
        assert(poller.state == .green && changes.isEmpty)
        await waitUntil("red state") { poller.state == .red }
        assert(changes == [.red])
        let request = fixture.snapshot.requests[0]
        assert(request.url?.absoluteString == "http://127.0.0.1:4390/status")
        assert(request.timeoutInterval == 1)
        assert(request.cachePolicy == .reloadIgnoringLocalCacheData)
        assert(request.value(forHTTPHeaderField: "Cache-Control") == "no-cache")

        for reply in [PollFixture.Reply(), .init(body: "garbage"),
                      .init(body: "{\"state\":\"blue\"}"),
                      .init(body: "{\"state\":\"green\"}", statusCode: 500),
                      .init(body: "{\"state\":\"green\"}", statusCode: 302),
                      .init(error: .cannotConnectToHost), .init(error: .timedOut),
                      .init(body: "{\"state\":\"green\"}", isHTTP: false)] {
            fixture.set(reply)
            let completed = fixture.snapshot.completed
            poller.poll()
            await waitUntil("invalid response completed") { fixture.snapshot.completed > completed }
            // Let the URLSession continuation process the completed fixture response.
            try? await Task.sleep(nanoseconds: 30_000_000)
            assert(poller.state == .red && changes == [.red])
        }

        fixture.set(.init(body: "{\"state\":\"yellow\"}"))
        await waitUntil("scheduled poll updates yellow") { poller.state == .yellow }
        fixture.set(.init(body: "{\"state\":\"green\"}", statusCode: 201))
        await waitUntil("scheduled poll updates green") { poller.state == .green }
        assert(changes == [.red, .yellow, .green])

        fixture.set(.init(body: "{\"state\":\"red\"}", delay: 0.8))
        await waitUntil("pending request before stop") { fixture.snapshot.active == 1 }
        let count = fixture.snapshot.requests.count
        poller.stop()
        await waitUntil("cancelled request") { fixture.snapshot.cancelled == 1 }
        try? await Task.sleep(nanoseconds: 900_000_000)
        assert(fixture.snapshot.requests.count == count)
        assert(fixture.snapshot.active == 0 && fixture.snapshot.peak == 1)
        assert(poller.state == .green && changes == [.red, .yellow, .green])
        print("PASS: async polling, cache/timeout settings, one in-flight request, retained state, and cancellation")
    }

    @MainActor static func testDisconnectGrace() async {
        assert(StatusPoller.disconnectGrace == 5)
        let fixture = StubURLProtocol.fixture
        for reply in [PollFixture.Reply(body: "garbage"), .init(body: "{\"state\":\"blue\"}"),
                      .init(body: "{\"state\":\"green\"}", statusCode: 500),
                      .init(body: "{\"state\":\"green\"}", statusCode: 302),
                      .init(error: .cannotConnectToHost), .init(error: .timedOut),
                      .init(body: "{\"state\":\"green\"}", isHTTP: false)] {
            fixture.set(reply)
            let configuration = URLSessionConfiguration.ephemeral
            configuration.protocolClasses = [StubURLProtocol.self]
            let clock = TestClock()
            var disconnects = 0
            let poller = StatusPoller(configuration: configuration, now: { clock.now }, onDisconnect: {
                assert(Thread.isMainThread)
                disconnects += 1
            }) { _ in assertionFailure("Invalid response changed state") }
            // The deadline starts at start(), not construction or wall-clock time.
            clock.now = 200
            var completed = fixture.snapshot.completed
            poller.start()
            await waitUntil("initial failed response") { fixture.snapshot.completed > completed }
            try? await Task.sleep(nanoseconds: 30_000_000)
            assert(disconnects == 0)

            clock.now = 204.99
            completed = fixture.snapshot.completed
            poller.start() // Repeated start must not extend the running poller's deadline.
            poller.poll()
            await waitUntil("failed response before grace expires") { fixture.snapshot.completed > completed }
            try? await Task.sleep(nanoseconds: 30_000_000)
            assert(disconnects == 0 && poller.state == .green)

            clock.now = 205
            poller.poll()
            assert(disconnects == 1)
            let count = fixture.snapshot.requests.count
            clock.now = 210
            poller.poll()
            poller.stop()
            try? await Task.sleep(nanoseconds: 30_000_000)
            assert(disconnects == 1 && fixture.snapshot.requests.count == count)
        }
        print("PASS: five-second startup grace; malformed, HTTP, non-HTTP, and network failures never reset it")
    }

    @MainActor static func testValidResponseKeepalive() async {
        let fixture = StubURLProtocol.fixture
        fixture.set(.init(body: "{\"state\":\"green\"}"))
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubURLProtocol.self]
        let clock = TestClock()
        var disconnects = 0
        var changes: [TrafficState] = []
        let poller = StatusPoller(configuration: configuration, now: { clock.now }, onDisconnect: {
            disconnects += 1
        }) { changes.append($0) }
        let completed = fixture.snapshot.completed
        poller.start()
        await waitUntil("initial unchanged green response") { fixture.snapshot.completed > completed }
        try? await Task.sleep(nanoseconds: 30_000_000)

        func receive(_ reply: PollFixture.Reply, at time: TimeInterval) async {
            fixture.set(reply)
            clock.now = time
            let completed = fixture.snapshot.completed
            poller.poll()
            await waitUntil("keepalive response") { fixture.snapshot.completed > completed }
            try? await Task.sleep(nanoseconds: 30_000_000)
            assert(disconnects == 0)
        }

        await receive(.init(body: "{\"state\":\"green\"}"), at: 104)
        assert(changes.isEmpty)
        await receive(.init(body: "{\"state\":\"red\"}"), at: 108)
        await receive(.init(body: "{\"state\":\"red\"}"), at: 112)
        assert(changes == [.red])
        await receive(.init(body: "{\"state\":\"yellow\"}", statusCode: 201), at: 116)
        assert(changes == [.red, .yellow])
        await receive(.init(body: "garbage"), at: 120)
        await receive(.init(body: "{\"state\":\"green\"}", statusCode: 500), at: 120.99)
        assert(poller.state == .yellow && changes == [.red, .yellow])
        clock.now = 121
        await waitUntil("scheduled disconnect after the last valid response") { disconnects == 1 }
        assert(disconnects == 1)
        poller.poll()
        assert(disconnects == 1)
        print("PASS: valid changed/unchanged colors reset grace; later invalid responses retain state but expire")
    }

    @MainActor static func testStoppedAndPendingDeadline() async {
        let fixture = StubURLProtocol.fixture
        for reply in [PollFixture.Reply(delay: 0.3), .init(delay: 0.3, error: .cannotConnectToHost)] {
            fixture.set(reply)
            let configuration = URLSessionConfiguration.ephemeral
            configuration.protocolClasses = [StubURLProtocol.self]
            let clock = TestClock()
            clock.now = 10
            var disconnects = 0
            var changes: [TrafficState] = []
            let poller = StatusPoller(configuration: configuration, now: { clock.now }, onDisconnect: {
                disconnects += 1
            }) { changes.append($0) }
            var cancelled = fixture.snapshot.cancelled
            poller.start()
            await waitUntil("pending request to stop") { fixture.snapshot.active == 1 }
            clock.now = 15
            poller.stop()
            poller.stop()
            poller.poll()
            await waitUntil("explicit stop cancellation") { fixture.snapshot.cancelled > cancelled }
            let count = fixture.snapshot.requests.count
            try? await Task.sleep(nanoseconds: 450_000_000)
            assert(disconnects == 0 && changes.isEmpty)
            assert(fixture.snapshot.requests.count == count && fixture.snapshot.active == 0)

            // Restart gets a fresh grace period, which also bounds an in-flight request.
            clock.now = 30
            cancelled = fixture.snapshot.cancelled
            poller.start()
            await waitUntil("pending request after restart") { fixture.snapshot.active == 1 }
            clock.now = 34.99
            for _ in 0..<10 { poller.poll() }
            assert(disconnects == 0 && fixture.snapshot.requests.count == count + 1)
            clock.now = 35
            poller.poll()
            assert(disconnects == 1)
            await waitUntil("deadline cancels pending request") { fixture.snapshot.cancelled > cancelled }
            try? await Task.sleep(nanoseconds: 450_000_000)
            poller.poll()
            assert(disconnects == 1 && changes.isEmpty && poller.state == .green)
            assert(fixture.snapshot.requests.count == count + 1 && fixture.snapshot.active == 0)
        }
        print("PASS: stop suppresses late state/close callbacks; restart resets grace; deadline cancels pending requests")
    }

    @MainActor static func testStandalonePolling() async {
        let fixture = StubURLProtocol.fixture
        fixture.set(.init(error: .cannotConnectToHost))
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubURLProtocol.self]
        let clock = TestClock()
        var disconnects = 0
        var changes: [TrafficState] = []
        let poller = StatusPoller(configuration: configuration, exitOnDisconnect: false,
                                  now: { clock.now }, onDisconnect: { disconnects += 1 }) {
            assert(Thread.isMainThread)
            changes.append($0)
        }
        poller.start()
        defer { poller.stop() }

        for (retained, recovered) in [(TrafficState.green, TrafficState.red), (.red, .yellow)] {
            fixture.set(.init(error: .cannotConnectToHost))
            let previousChanges = changes
            clock.now += 3600
            let completed = fixture.snapshot.completed
            // No manual poll or restart: the timer must keep requesting through a long outage.
            await waitUntil("scheduled standalone polls during hour-long outage") {
                fixture.snapshot.completed >= completed + 2
            }
            try? await Task.sleep(nanoseconds: 30_000_000)
            assert(disconnects == 0 && poller.state == retained && changes == previousChanges)

            fixture.set(.init(body: "{\"state\":\"\(recovered.rawValue)\"}"))
            await waitUntil("standalone recovery to \(recovered.rawValue)") { poller.state == recovered }
            assert(disconnects == 0 && changes == previousChanges + [recovered])
        }
        assert(changes == [.red, .yellow] && fixture.snapshot.peak == 1)
        print("PASS: standalone polling survives hour-long startup/later outages, retains state, and recovers without restart")
    }

    // Opt-in only: constructing AppKit controls can connect to WindowServer.
    @MainActor static func testCloseControlGUI() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let wasActive = app.isActive
        let screen = NSScreen.main!
        let usable = screen.visibleFrame
        let origin = CGPoint(x: usable.midX - 30.5, y: usable.midY)
        let panel = TrafficLightPanel(contentRect: CGRect(origin: origin, size: CGSize(width: 61, height: 24)),
                                      styleMask: [.borderless, .nonactivatingPanel],
                                      backing: .buffered, defer: false)
        panel.becomesKeyOnlyIfNeeded = true
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.isMovable = false
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.animationBehavior = .none
        @MainActor final class Pointer {
            var value = CGPoint.zero
        }
        let pointer = Pointer()
        pointer.value = CGPoint(x: usable.minX - 100, y: usable.minY - 100)
        var closes = 0
        let view = TrafficLightView(frame: CGRect(origin: .zero, size: WidgetOrientation.horizontal.size()),
                                    pointerLocation: { pointer.value }, reducedMotion: { true },
                                    onScaleCommit: { _ in assertionFailure("Hover/move must not persist scale") }) {
            closes += 1
        }
        panel.contentView = view
        panel.orderFrontRegardless()
        defer { panel.close() }
        let button = view.closeButton
        assert(button.isHidden && !button.isEnabled)
        assert(button.accessibilityLabel() == "Close traffic light")
        assert(button.image == nil && button.title == "Close traffic light")
        assert(button.target === view && button.action != nil)
        assert(button.acceptsFirstMouse(for: nil) && !button.needsPanelToBecomeKey)
        assert(!button.acceptsFirstResponder && button.focusRingType == .none)
        assert(!panel.canBecomeKey && !panel.canBecomeMain)

        let entered = NSEvent.enterExitEvent(with: .mouseEntered, location: .zero, modifierFlags: [],
                                             timestamp: 0, windowNumber: panel.windowNumber, context: nil,
                                             eventNumber: 0, trackingNumber: 0, userData: nil)!
        let exited = NSEvent.enterExitEvent(with: .mouseExited, location: .zero, modifierFlags: [],
                                            timestamp: 0, windowNumber: panel.windowNumber, context: nil,
                                            eventNumber: 0, trackingNumber: 0, userData: nil)!
        let right = NSEvent.mouseEvent(with: .rightMouseDown, location: .zero, modifierFlags: [],
                                       timestamp: 0, windowNumber: panel.windowNumber, context: nil,
                                       eventNumber: 0, clickCount: 1, pressure: 1)!
        let down = NSEvent.mouseEvent(with: .leftMouseDown, location: .zero, modifierFlags: [],
                                      timestamp: 0, windowNumber: panel.windowNumber, context: nil,
                                      eventNumber: 0, clickCount: 1, pressure: 1)!
        let up = NSEvent.mouseEvent(with: .leftMouseUp, location: .zero, modifierFlags: [],
                                    timestamp: 0, windowNumber: panel.windowNumber, context: nil,
                                    eventNumber: 0, clickCount: 1, pressure: 0)!
        let dragged = NSEvent.mouseEvent(with: .leftMouseDragged, location: .zero, modifierFlags: [],
                                         timestamp: 0, windowNumber: panel.windowNumber, context: nil,
                                         eventNumber: 0, clickCount: 1, pressure: 1)!
        func assertFrame(_ expected: CGRect) {
            let tolerance = 1 / panel.backingScaleFactor + 0.000001
            assert(abs(panel.frame.width - expected.width) <= tolerance && abs(panel.frame.height - expected.height) <= tolerance)
            assert(abs(view.bounds.width - expected.width) <= tolerance && abs(view.bounds.height - expected.height) <= tolerance)
            assert(abs(panel.frame.minX - expected.minX) <= tolerance
                   && abs(panel.frame.minY - expected.minY) <= tolerance,
                   "Window frame \(panel.frame) != \(expected)")
        }
        func pointInView(_ point: CGPoint) -> CGPoint {
            panel.convertPoint(toScreen: view.convert(point, to: nil))
        }
        func hit(_ point: CGPoint) -> NSView? {
            view.hitTest(view.convert(point, to: view.superview))
        }
        for (index, orientation) in [WidgetOrientation.horizontal, .left, .right, .horizontal].enumerated() {
            let placement = WidgetPlacement(frame: CGRect(origin: origin, size: orientation.size()),
                                              orientation: orientation, expanded: false)
            view.apply(placement)
            view.updateTrackingAreas()
            let area = view.trackingAreas[0]
            view.updateTrackingAreas()
            assertFrame(placement.frame)
            assert(button.frame == orientation.closeButtonFrame)
            assert(view.trackingAreas.count == 1)
            assert(view.trackingAreas[0] === area)
            assert(area.owner === view)
            assert(area.options == [.mouseEnteredAndExited, .activeAlways, .inVisibleRect, .enabledDuringMouseDrag])

            pointer.value = CGPoint(x: usable.minX - 100, y: usable.minY - 100)
            view.mouseExited(with: exited)
            button.performClick(nil)
            assert(button.isHidden && !view.expanded && closes == index)
            assert(hit(CGPoint(x: button.frame.midX, y: button.frame.midY)) == nil)
            let collapsed = panel.frame
            pointer.value = pointInView(CGPoint(x: 12, y: 12))
            view.mouseEntered(with: entered)
            assert(!button.isHidden && view.expanded && closes == index)
            assert(view.orientation == orientation)
            let expanded = WidgetGeometry.resized(frame: collapsed, orientation: orientation,
                                                   expanded: true, in: usable)
            assertFrame(expanded.frame)
            // NSButtonCell exposes these methods without conforming to NSAccessibilityProtocol at runtime.
            let accessible = NSAccessibility.unignoredDescendant(of: button)
            if let cell = accessible as? NSButtonCell {
                assert(cell.accessibilityRole()?.rawValue == "AXButton")
                assert(cell.accessibilityLabel() == "Close traffic light")
            } else if let element = accessible as? NSAccessibilityProtocol {
                assert(element.isAccessibilityElement())
                assert(element.accessibilityRole()?.rawValue == "AXButton")
                assert(element.accessibilityLabel() == "Close traffic light")
            } else {
                preconditionFailure("Visible close control must expose an accessibility element")
            }
            for _ in 0..<20 {
                view.updateTrackingAreas()
                view.mouseExited(with: exited) // Stale exit from the pre-expansion rectangle.
                view.mouseEntered(with: entered)
                assert(view.expanded && view.trackingAreas[0] === area)
                assertFrame(expanded.frame)
            }
            let slot = button.frame
            pointer.value = pointInView(CGPoint(x: slot.midX, y: slot.midY))
            view.mouseExited(with: exited)
            assert(view.expanded) // Moving into the new tail must not collapse it.
            for point in [CGPoint(x: slot.midX + 5, y: slot.midY),
                          CGPoint(x: slot.midX, y: slot.midY - 5),
                          CGPoint(x: slot.midX, y: slot.midY)] {
                assert(hit(point) === button)
            }
            for center in orientation.lampCenters { assert(hit(center) === view) }

            // Exercise native view/button overrides, including hidden title, against the pure renderer.
            for state in TrafficState.allCases {
                view.state = state
                let context = render(orientation: orientation, expanded: true, state: state, scale: 5)
                let count = context.bytesPerRow * context.height
                let expected = Data(bytes: context.data!, count: count)
                NSGraphicsContext.saveGraphicsState()
                NSGraphicsContext.current = NSGraphicsContext(cgContext: context, flipped: true)
                view.draw(view.bounds)
                context.saveGState()
                context.translateBy(x: slot.minX, y: slot.minY)
                button.draw(button.bounds)
                context.restoreGState()
                let grip = view.resizeHandle
                context.translateBy(x: grip.frame.minX, y: grip.frame.minY)
                grip.draw(grip.bounds)
                NSGraphicsContext.restoreGraphicsState()
                assert(Data(bytes: context.data!, count: count) == expected)
            }
            pointer.value = CGPoint(x: usable.minX - 100, y: usable.minY - 100)
            view.mouseExited(with: exited)
            assert(button.isHidden && closes == index)
            assertFrame(collapsed)
            view.mouseEntered(with: entered) // Stale enter outside must not reopen the capsule.
            assert(!view.expanded)
            view.rightMouseDown(with: right)
            assert(!button.isHidden && closes == index)
            button.rightMouseDown(with: right)
            assert(!button.isHidden && closes == index)
            assertFrame(expanded.frame)
            pointer.value = pointInView(CGPoint(x: 12, y: 12))
            view.mouseDown(with: down)
            assert(view.drag != nil && closes == index)
            pointer.value = CGPoint(x: usable.minX - 100, y: usable.minY - 100)
            view.mouseExited(with: exited)
            assert(view.expanded && view.drag != nil)
            assertFrame(expanded.frame)
            view.mouseUp(with: up)
            assert(view.drag == nil && !view.expanded && closes == index)
            assertFrame(collapsed)
            pointer.value = pointInView(CGPoint(x: 12, y: 12))
            view.mouseEntered(with: entered)
            view.mouseDown(with: down)
            let drag = view.drag!
            for x in [usable.minX + 20, usable.midX, usable.maxX - 20, usable.midX] {
                pointer.value = CGPoint(x: x, y: usable.midY)
                let expected = WidgetGeometry.dragged(center: drag.center(at: pointer.value), expanded: true, in: usable)
                view.mouseDragged(with: dragged)
                assert(view.expanded && view.drag?.centerOffset == drag.centerOffset)
                assert(view.orientation == expected.orientation)
                assertFrame(expected.frame)
                view.mouseExited(with: exited)
                assert(view.expanded && view.drag != nil)
            }
            pointer.value = pointInView(CGPoint(x: 12, y: 12))
            view.mouseUp(with: up)
            assert(view.drag == nil && view.expanded)
            app.sendAction(button.action!, to: view, from: NSButton(frame: .zero))
            assert(closes == index)
            button.performClick(nil)
            assert(closes == index + 1 && view.drag == nil)
            assert(!panel.isKeyWindow && !panel.isMainWindow && app.isActive == wasActive)
        }
        for orientation in [WidgetOrientation.horizontal, .left, .right] {
            let size = orientation.size()
            let frame = CGRect(x: usable.maxX - size.width, y: usable.minY, width: size.width, height: size.height)
            view.apply(WidgetPlacement(frame: frame, orientation: orientation, expanded: false))
            pointer.value = pointInView(CGPoint(x: 12, y: 12))
            view.mouseEntered(with: entered)
            let expected = WidgetGeometry.resized(frame: frame, orientation: orientation, expanded: true, in: usable)
            assertFrame(expected.frame)
            assert(view.orientation == orientation && view.expanded)
            for _ in 0..<20 {
                view.updateTrackingAreas()
                view.mouseExited(with: exited)
                assertFrame(expected.frame)
                assert(view.expanded && view.orientation == orientation)
            }
            pointer.value = CGPoint(x: usable.minX - 100, y: usable.minY - 100)
            view.mouseExited(with: exited)
            assertFrame(WidgetGeometry.resized(frame: expected.frame, orientation: orientation,
                                                expanded: false, in: usable).frame)
        }
        print("PASS: GUI native cell accessibility, circular Close target, raster parity, stable hover/clamps, drag release, nonactivation")
    }

    @MainActor static func testResizeMotionGUI() {
        let app = NSApplication.shared
        let wasActive = app.isActive
        let usable = NSScreen.main!.visibleFrame
        let clock = TestClock()
        let input = TestInput()
        input.reducedMotion = true
        let panel = TrafficLightPanel(contentRect: WidgetGeometry.initial(in: usable).frame,
                                      styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.isReleasedWhenClosed = false
        panel.isMovable = false
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.animationBehavior = .none
        let view = TrafficLightView(frame: CGRect(origin: .zero, size: panel.frame.size),
                                    pointerLocation: { input.pointer }, now: { clock.now },
                                    reducedMotion: { input.reducedMotion }, onScaleCommit: { input.commits.append($0) },
                                    onClose: { input.closes += 1 })
        panel.contentView = view
        panel.orderFrontRegardless()
        defer { panel.close() }
        let grip = view.resizeHandle
        let close = view.closeButton
        let outside = CGPoint(x: usable.minX - 100, y: usable.minY - 100)
        func event(_ type: NSEvent.EventType, clicks: Int = 1, at local: CGPoint = .zero) -> NSEvent {
            input.eventNumber += 1
            return NSEvent.mouseEvent(with: type, location: view.convert(local, to: nil), modifierFlags: [],
                                      timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: panel.windowNumber,
                                      context: nil, eventNumber: input.eventNumber,
                                      clickCount: clicks, pressure: type == .leftMouseUp ? 0 : 1)!
        }
        func point(_ local: CGPoint) -> CGPoint { panel.convertPoint(toScreen: view.convert(local, to: nil)) }
        func controlPoint(_ long: CGFloat, _ short: CGFloat = 12) -> CGPoint {
            let scale = view.presentationScale
            return view.orientation == .horizontal ? CGPoint(x: long * scale, y: short * scale)
                                                   : CGPoint(x: short * scale, y: long * scale)
        }
        func hit(_ local: CGPoint) -> NSView? { view.hitTest(view.convert(local, to: view.superview)) }
        func near(_ actual: CGFloat, _ expected: CGFloat) { assert(abs(actual - expected) < 0.000001, "\(actual) != \(expected)") }
        func assertFrame(_ expected: CGRect) {
            let tolerance = 1 / panel.backingScaleFactor + 0.000001
            for (actual, target) in [(panel.frame.minX, expected.minX), (panel.frame.maxY, expected.maxY),
                                     (panel.frame.width, expected.width), (panel.frame.height, expected.height),
                                     (view.bounds.width, expected.width), (view.bounds.height, expected.height)] {
                assert(abs(actual - target) <= tolerance, "Native frame \(panel.frame) != \(expected)")
            }
            assert(usable.insetBy(dx: -tolerance, dy: -tolerance).contains(panel.frame))
        }
        func place(_ orientation: WidgetOrientation, scale: CGFloat = 1, expanded: Bool = true) {
            let placement = WidgetGeometry.placement(center: CGPoint(x: usable.midX, y: usable.midY),
                                                      orientation: orientation, expanded: expanded, scale: scale, in: usable)
            view.apply(placement)
            assertFrame(placement.frame)
            input.pointer = point(CGPoint(x: 12 * scale, y: 12 * scale))
        }
        assert(grip.accessibilityLabel() == "Resize traffic light")
        assert(grip.accessibilityRole() == .slider && grip.isAccessibilityElement())
        assert(grip.toolTip == "Drag to resize; double-click to reset to 100%")
        assert(grip.acceptsFirstMouse(for: nil) && !grip.needsPanelToBecomeKey && !grip.acceptsFirstResponder)
        for orientation in [WidgetOrientation.horizontal, .left, .right] {
            for scale: CGFloat in [0.75, 1, 1.137, 2] {
                place(orientation, scale: scale)
                assert(close.frame.intersects(grip.frame))
                for (long, short) in [(CGFloat(76.2), CGFloat(3.6)), (79.5, 6), (82, 12), (79.5, 18), (76.2, 20.4)] {
                    let local = controlPoint(long, short)
                    assert(hit(local) === grip)
                    assert(grip.hitTest(local) === grip && close.hitTest(local) == nil)
                }
                for (long, short) in [(CGFloat(70.5), CGFloat(12)), (75.5, 12), (70.5, 7), (65.5, 12)] {
                    let local = controlPoint(long, short)
                    assert(hit(local) === close) // Overlay passes the overlapping portion of the Close circle through.
                    assert(grip.hitTest(local) == nil && close.hitTest(local) === close)
                }
                for (long, short) in [(CGFloat(62), CGFloat(2)), (68, 2), (79.9, 0.1)] {
                    let local = controlPoint(long, short)
                    assert(hit(local) !== close && hit(local) !== grip)
                    input.pointer = point(local)
                    view.mouseDown(with: event(.leftMouseDown, at: local))
                    assert(view.drag == nil && view.resize == nil && input.closes == 0)
                }
                for degrees in stride(from: CGFloat(0), to: 360, by: 15) {
                    let angle = degrees * .pi / 180
                    let gap = controlPoint(70.5 + 7 * cos(angle), 12 + 7 * sin(angle))
                    assert(hit(gap) === view)
                    assert(!orientation.resizeCursorRects(scale: scale).contains { $0.contains(gap) })
                    input.pointer = point(gap)
                    view.mouseDown(with: event(.leftMouseDown, at: gap))
                    assert(view.drag == nil && view.resize == nil && input.closes == 0)
                }
                grip.resetCursorRects()
            }
            place(orientation)
            near(grip.accessibilityValue() as! CGFloat, 100)
            assert(grip.accessibilityValueDescription() == "100%")
            input.pointer = point(controlPoint(82))
            let initialPointer = input.pointer
            let initialFrame = panel.frame
            let commits = input.commits.count
            grip.mouseDown(with: event(.leftMouseDown))
            assert(view.resize != nil && view.drag == nil && !view.controlsEnabled)
            for delta: CGFloat in [11.508, 84, 1000, -21, -1000, 0, 42] {
                input.pointer = orientation == .horizontal
                    ? CGPoint(x: initialPointer.x + delta, y: initialPointer.y + 500)
                    : CGPoint(x: initialPointer.x + 500, y: initialPointer.y - delta)
                grip.mouseDragged(with: event(.leftMouseDragged))
                let scale = max(0.75, min(2, 1 + delta / 84))
                near(view.scale, scale)
                near(view.presentationScale, scale)
                assert(view.orientation == orientation && view.drag == nil && view.expanded)
                assert(!view.isAnimationTimerRunning && input.commits.count == commits)
                assertFrame(WidgetGeometry.resized(frame: initialFrame, orientation: orientation,
                                                   expanded: true, scale: scale, in: usable).frame)
                view.updateHover() // Pointer is deliberately outside; no collapse while captured.
                view.mouseDown(with: event(.leftMouseDown))
                close.performClick(nil)
                assert(view.resize != nil && view.drag == nil && view.expanded && input.closes == 0)
            }
            input.pointer = point(controlPoint(70.5)) // Resize capture must not activate Close on release.
            grip.mouseUp(with: event(.leftMouseUp))
            assert(view.resize == nil && view.expanded && input.commits.count == commits + 1 && input.closes == 0)
            near(input.commits.last!, 1.5)
            input.pointer = point(controlPoint(82))
            grip.mouseDown(with: event(.leftMouseDown, clicks: 2))
            grip.mouseUp(with: event(.leftMouseUp))
            near(view.scale, 1)
            near(view.presentationScale, 1)
            assert(input.commits.count == commits + 2 && input.commits.last == 1 && view.resize == nil)
            assert(!view.isAnimationTimerRunning)
            assert(grip.accessibilityPerformIncrement())
            near(view.scale, 1.1)
            assert(grip.accessibilityPerformDecrement())
            near(view.scale, 1)
            input.pointer = outside
            view.updateHover()
            assert(grip.isHidden && !view.controlsEnabled && !grip.accessibilityPerformIncrement())
            grip.mouseDown(with: event(.leftMouseDown))
            assert(view.resize == nil && view.drag == nil)
        }

        // Animated reveal, reversal and retarget use a MainActor clock/reference, never mutable captures.
        input.reducedMotion = false
        for orientation in [WidgetOrientation.horizontal, .left, .right] {
            place(orientation, scale: 1.137, expanded: false)
            let anchor = panel.frame
            let commits = input.commits.count
            view.updateHover()
            assert(view.expanded && view.isAnimationTimerRunning && !view.controlsEnabled)
            near(view.controlOpacity, 0)
            clock.now += 0.075
            view.advanceAnimations()
            near(view.controlOpacity, 0.5)
            near(close.alphaValue, 0.5)
            near(grip.alphaValue, 0.5)
            assertFrame(WidgetGeometry.resized(frame: anchor, orientation: orientation, expanded: true,
                                               scale: 1.137, reveal: 0.5, in: usable).frame)
            let middle = panel.frame
            grip.mouseDown(with: event(.leftMouseDown))
            close.performClick(nil)
            assert(view.resize == nil && input.closes == 0 && !view.controlsEnabled)
            input.pointer = point(CGPoint(x: close.frame.midX, y: close.frame.midY))
            view.mouseDown(with: event(.leftMouseDown))
            assert(view.drag == nil) // Partially revealed controls do not become move handles.
            input.pointer = outside
            view.updateHover()
            assert(!view.expanded && !view.controlsEnabled)
            assertFrame(middle)
            clock.now += 0.075
            view.advanceAnimations()
            near(view.controlOpacity, 0.25)
            input.pointer = point(CGPoint(x: 12, y: 12))
            view.updateHover()
            near(view.controlOpacity, 0.25)
            clock.now += 0.2
            view.advanceAnimations()
            assert(view.controlsEnabled && !view.isAnimationTimerRunning)
            assertFrame(WidgetGeometry.resized(frame: anchor, orientation: orientation, expanded: true,
                                               scale: 1.137, in: usable).frame)
            input.pointer = outside
            view.updateHover()
            assert(!view.controlsEnabled && !close.isEnabled && !grip.accessibilityPerformDecrement())
            close.performClick(nil)
            assert(input.closes == 0 && input.commits.count == commits)
            // Reduce Motion changes during an active transition take effect on the next frame.
            input.reducedMotion = true
            view.advanceAnimations()
            assert(grip.isHidden && !view.isAnimationTimerRunning)
            near(view.controlOpacity, 0)
            input.reducedMotion = false
        }

        place(.horizontal, scale: 2)
        let resetAnchor = panel.frame
        input.pointer = point(controlPoint(82))
        grip.mouseDown(with: event(.leftMouseDown, clicks: 2))
        assert(view.resize == nil && view.scale == 1 && input.commits.last == 1)
        near(view.presentationScale, 2)
        clock.now += 0.075
        view.advanceAnimations()
        near(view.presentationScale, 1.5)
        assertFrame(WidgetGeometry.resized(frame: resetAnchor, orientation: .horizontal,
                                           expanded: true, scale: 1.5, in: usable).frame)
        clock.now += 0.2
        view.advanceAnimations()
        near(view.presentationScale, 1)
        assert(view.controlsEnabled && !view.isAnimationTimerRunning)
        assertFrame(WidgetGeometry.resized(frame: resetAnchor, orientation: .horizontal,
                                           expanded: true, in: usable).frame)
        // Interrupt reset with direct resizing from the currently visible scale.
        place(.horizontal, scale: 2)
        input.pointer = point(controlPoint(82))
        grip.mouseDown(with: event(.leftMouseDown, clicks: 2))
        clock.now += 0.075
        view.advanceAnimations()
        input.pointer = point(controlPoint(82))
        let resizeStart = input.pointer
        grip.mouseDown(with: event(.leftMouseDown))
        near(view.resize!.initialScale, 1.5)
        assert(!view.isAnimationTimerRunning)
        input.pointer.x = resizeStart.x + 21
        grip.mouseDragged(with: event(.leftMouseDragged))
        near(view.presentationScale, 1.75)
        grip.mouseUp(with: event(.leftMouseUp))
        near(input.commits.last!, 1.75)
        input.reducedMotion = true
        view.advanceAnimations()
        place(.horizontal)
        input.reducedMotion = false
        let lampFrame = panel.frame
        view.state = .red
        assert(view.isAnimationTimerRunning && view.lampAlphas == [0.15, 0.15, 1])
        clock.now += 0.060
        view.advanceAnimations()
        for (actual, target) in zip(view.lampAlphas, [CGFloat(0.575), 0.15, 0.575]) { near(actual, target) }
        view.state = .yellow
        for (actual, target) in zip(view.lampAlphas, [CGFloat(0.575), 0.15, 0.575]) { near(actual, target) }
        clock.now += 0.060
        view.advanceAnimations()
        for (actual, target) in zip(view.lampAlphas, [CGFloat(0.3625), 0.575, 0.3625]) { near(actual, target) }
        assertFrame(lampFrame)
        input.reducedMotion = true
        view.advanceAnimations()
        assert(view.lampAlphas == [0.15, 1, 0.15] && !view.isAnimationTimerRunning)
        view.state = .green
        assert(view.lampAlphas == [0.15, 0.15, 1] && !view.isAnimationTimerRunning)

        input.reducedMotion = false
        place(.horizontal, expanded: false)
        view.updateHover()
        clock.now += 0.075
        view.advanceAnimations()
        view.mouseDown(with: event(.leftMouseDown))
        assert(view.drag != nil && !view.isAnimationTimerRunning)
        near(view.controlOpacity, 0.5)
        input.pointer = outside
        view.updateHover()
        clock.now += 1
        view.advanceAnimations()
        near(view.controlOpacity, 0.5)
        view.mouseUp(with: event(.leftMouseUp))
        assert(view.drag == nil && !view.expanded && view.isAnimationTimerRunning)
        clock.now += 0.2
        view.advanceAnimations()
        assert(!view.isAnimationTimerRunning && grip.isHidden)
        input.pointer = point(CGPoint(x: 12, y: 12))
        view.updateHover()
        assert(view.isAnimationTimerRunning)
        clock.now += 0.2
        let deadline = Date().addingTimeInterval(1)
        while view.isAnimationTimerRunning && Date() < deadline {
            _ = RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
        }
        assert(view.controlsEnabled && !view.isAnimationTimerRunning)

        assert(input.closes == 0)
        input.reducedMotion = true
        for orientation in [WidgetOrientation.horizontal, .left, .right] {
            for scale: CGFloat in [0.75, 1, 1.137, 2] {
                place(orientation, scale: scale)
                let closes = input.closes
                panel.displayIfNeeded()
                // Drain pending WindowServer moves before injecting window-relative events;
                // otherwise AppKit rebases their queued locations using the previous frame.
                let settled = Date().addingTimeInterval(0.1)
                while Date() < settled {
                    if let pending = app.nextEvent(matching: .any, until: settled, inMode: .default, dequeue: true) {
                        app.sendEvent(pending)
                    }
                }
                // Prequeue drag/up for the real NSButton.mouseDown/NSButtonCell tracking path.
                // A background watchdog exits the test process if AppKit ever fails to consume them.
                for (long, short) in [(CGFloat(82), CGFloat(12)), (76.2, 3.6), (77.5, 12), (62, 2), (70.5, 12)] {
                    let release = controlPoint(long, short)
                    let timeout = DispatchWorkItem {
                        print("FAIL: native Close tracking did not consume its queued mouse-up")
                        exit(1)
                    }
                    DispatchQueue.global().asyncAfter(deadline: .now() + 3, execute: timeout)
                    input.pointer = point(release)
                    let down = event(.leftMouseDown, at: controlPoint(70.5))
                    let dragged = event(.leftMouseDragged, at: release)
                    let up = event(.leftMouseUp, at: release)
                    app.postEvent(up, atStart: true)
                    app.postEvent(dragged, atStart: true)
                    app.sendEvent(down)
                    // Cells may stop tracking on exit before consuming mouse-up. Finish that gesture
                    // through AppKit instead of leaving its release queued for the next mouse-down.
                    while let pending = app.nextEvent(matching: [.leftMouseDragged, .leftMouseUp],
                                                       until: .distantPast, inMode: .eventTracking, dequeue: true) {
                        assert(pending.eventNumber == dragged.eventNumber || pending.eventNumber == up.eventNumber)
                        app.sendEvent(pending)
                    }
                    timeout.cancel()
                    assert(!close.isTrackingMouse && view.resize == nil && view.drag == nil)
                    assert(input.closes == closes + (long == 70.5 ? 1 : 0),
                           "\(orientation) scale=\(scale) release=\(long),\(short) closes=\(input.closes) baseline=\(closes) expanded=\(view.expanded) enabled=\(close.isEnabled)")
                }
            }
        }
        let closes = input.closes
        input.pointer = outside
        close.performClick(nil)
        assert(input.closes == closes + 1) // Programmatic activation ignores pointer position.
        let accessible = NSAccessibility.unignoredDescendant(of: close)
        if let cell = accessible as? NSButtonCell {
            // Native button cells expose AXPress through the informal action selector.
            let press = NSSelectorFromString("accessibilityPerformAction:")
            assert(cell.responds(to: press))
            _ = cell.perform(press, with: NSAccessibility.Action.press.rawValue)
        } else if let element = accessible as? NSAccessibilityProtocol {
            assert(element.accessibilityPerformPress())
        } else {
            preconditionFailure("Close must expose an accessible press action")
        }
        assert(input.closes == closes + 2)
        print("PASS: GUI padded arc targets, neutral gap at all scales/orientations, resize-to-Close safety, native Close-to-arc/gap cancellation and programmatic/AX activation")
        input.reducedMotion = false
        view.state = .red
        assert(view.isAnimationTimerRunning)
        panel.contentView = nil
        assert(!view.isAnimationTimerRunning)
        assert(!panel.isKeyWindow && !panel.isMainWindow && app.isActive == wasActive)
        print("PASS: GUI arc accessibility, immediate anchored resize/reset commits, fractional frames, motion reversal/gating, lamp retarget, gesture freeze and timer cleanup")
    }

    @MainActor static func main() {
        if let index = CommandLine.arguments.firstIndex(of: "--render-previews") {
            precondition(CommandLine.arguments.count == index + 2, "Usage: --render-previews <existing-directory>")
            renderPreviews(to: CommandLine.arguments[index + 1])
            return
        }
        testDecoding()
        testGeometry()
        do { try testScaleAndPreferences() } catch { preconditionFailure("Preference fixtures: \(error)") }
        testTweens()
        testPalette()
        testRaster()
        let guiSmoke = CommandLine.arguments.contains("--gui-smoke")
        if !guiSmoke {
            print("SKIP: AppKit control interactions (opt in with --gui-smoke)")
        }
        Task { @MainActor in
            await testPolling()
            await testDisconnectGrace()
            await testValidResponseKeepalive()
            await testStoppedAndPendingDeadline()
            await testStandalonePolling()
            // Run AppKit tracking only after async tests; it can stop a CFRunLoop iteration.
            if guiSmoke {
                testCloseControlGUI()
                testResizeMotionGUI()
            } else {
                assert(NSApp == nil, "Headless tests must not create NSApplication")
            }
            print("All macOS widget tests passed")
            CFRunLoopStop(CFRunLoopGetMain())
        }
        // Default mode exercises real Timer scheduling without NSApplication or windows.
        CFRunLoopRun()
    }
}
