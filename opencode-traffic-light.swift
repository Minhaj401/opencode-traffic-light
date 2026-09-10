import AppKit
import Foundation

enum TrafficState: String, Decodable, CaseIterable {
    case red, yellow, green

    static func decode(_ data: Data, statusCode: Int) -> TrafficState? {
        struct Status: Decodable {
            let state: TrafficState
        }
        guard (200...299).contains(statusCode) else { return nil }
        return try? JSONDecoder().decode(Status.self, from: data).state
    }

    func color(alpha: CGFloat = 1) -> CGColor {
        let rgb: (CGFloat, CGFloat, CGFloat)
        switch self {
        case .red: rgb = (199, 68, 48)
        case .yellow: rgb = (197, 205, 45)
        case .green: rgb = (45, 200, 79)
        }
        return CGColor(srgbRed: rgb.0 / 255, green: rgb.1 / 255, blue: rgb.2 / 255, alpha: alpha)
    }

    var ringColor: CGColor {
        let rgb: (CGFloat, CGFloat, CGFloat)
        switch self {
        case .red: rgb = (81, 41, 29)
        case .yellow: rgb = (80, 85, 27)
        case .green: rgb = (27, 79, 43)
        }
        return CGColor(srgbRed: rgb.0 / 255, green: rgb.1 / 255, blue: rgb.2 / 255, alpha: 1)
    }
}

enum WidgetOrientation {
    case horizontal, left, right

    func size(expanded: Bool = false, scale: CGFloat = 1, reveal: CGFloat? = nil) -> CGSize {
        let length = (61 + 23 * (reveal ?? (expanded ? 1 : 0))) * scale
        return self == .horizontal ? CGSize(width: length, height: 24 * scale)
                                  : CGSize(width: 24 * scale, height: length)
    }

    var lampCenters: [CGPoint] {
        if self == .horizontal {
            return [12, 30.5, 49].map { CGPoint(x: $0, y: 12) }
        }
        return [12, 30.5, 49].map { CGPoint(x: 12, y: $0) }
    }

    var closeButtonFrame: CGRect {
        controlFrame(resize: false)
    }

    func controlFrame(resize: Bool, scale: CGFloat = 1) -> CGRect {
        let start: CGFloat = resize ? 75 : 61
        let length: CGFloat = resize ? 9 : 19
        return self == .horizontal ? CGRect(x: start * scale, y: 0, width: length * scale, height: 24 * scale)
                                  : CGRect(x: 0, y: start * scale, width: 24 * scale, height: length * scale)
    }

    func controlContains(_ point: CGPoint, resize: Bool, scale: CGFloat = 1) -> Bool {
        let long = (self == .horizontal ? point.x : point.y) / scale
        let short = (self == .horizontal ? point.y : point.x) / scale
        let closeDistance = hypot(long - 70.5, short - 12)
        return resize ? long >= 75 && hypot(long - 72, short - 12) <= 12 && closeDistance >= 8 : closeDistance <= 6
    }

    func resizeCursorRects(scale: CGFloat = 1) -> [CGRect] {
        // Inscribed cap strips exclude Close and its two-point neutral gap.
        stride(from: CGFloat(1), to: 23, by: 2).map { short in
            let near = max(0, abs(short + 1 - 12) - 1)
            let far = abs(short + 1 - 12) + 1
            let start: CGFloat = near <= 8 ? max(75, 70.5 + sqrt(64 - near * near) + 0.01) : 75
            let end = 72 + sqrt(144 - far * far)
            return self == .horizontal
                ? CGRect(x: start * scale, y: short * scale, width: (end - start) * scale, height: 2 * scale)
                : CGRect(x: short * scale, y: start * scale, width: 2 * scale, height: (end - start) * scale)
        }
    }
}

enum WidgetPreferences {
    static let scaleRange: ClosedRange<CGFloat> = 0.75...2

    private struct Values: Codable {
        let scale: Double
    }

    static func validated(_ scale: CGFloat) -> CGFloat {
        scale.isFinite && scaleRange.contains(scale) ? scale : 1
    }

    static func url(environment: [String: String] = ProcessInfo.processInfo.environment,
                    home: URL = FileManager.default.homeDirectoryForCurrentUser) -> URL {
        let base: URL
        if let stateHome = environment["XDG_STATE_HOME"], !stateHome.isEmpty {
            base = URL(fileURLWithPath: stateHome, isDirectory: true)
        } else {
            base = home.appendingPathComponent(".local/state", isDirectory: true)
        }
        return base.appendingPathComponent("opencode-traffic-light/preferences.json")
    }

    static func load(from url: URL) -> CGFloat {
        guard let data = try? Data(contentsOf: url),
              let values = try? JSONDecoder().decode(Values.self, from: data) else { return 1 }
        return validated(CGFloat(values.scale))
    }

    static func save(_ scale: CGFloat, to url: URL) {
        guard scale.isFinite, scaleRange.contains(scale) else { return }
        do {
            let data = try JSONEncoder().encode(Values(scale: Double(scale)))
            try FileManager.default.createDirectory(at: url.deletingLastPathComponent(),
                                                    withIntermediateDirectories: true)
            try data.write(to: url, options: .atomic)
        } catch {
            // Preferences are optional; an unwritable state directory must not stop the widget.
        }
    }
}

struct WidgetTween {
    private(set) var value: CGFloat
    private(set) var target: CGFloat
    private var from: CGFloat
    private var start: TimeInterval = 0
    private var duration: TimeInterval = 0
    private(set) var isAnimating = false

    init(_ value: CGFloat) {
        self.value = value
        target = value
        from = value
    }

    mutating func advance(at time: TimeInterval, reducedMotion: Bool = false) {
        guard isAnimating else { return }
        let t = reducedMotion ? 1 : max(0, min(1, (time - start) / duration))
        value = from + (target - from) * CGFloat(t * t * (3 - 2 * t))
        if t >= 1 {
            value = target
            isAnimating = false
        }
    }

    mutating func retarget(_ target: CGFloat, at time: TimeInterval,
                           duration: TimeInterval, animated: Bool) {
        advance(at: time)
        from = value
        self.target = target
        start = time
        self.duration = duration
        isAnimating = animated && duration > 0 && value != target
        if !isAnimating { value = target }
    }
}

struct WidgetPlacement: Equatable {
    let frame: CGRect
    let orientation: WidgetOrientation
    let expanded: Bool
    var scale: CGFloat = 1
}

enum WidgetGeometry {
    static let edgeSnap: CGFloat = 80

    static func initial(scale: CGFloat = 1, in bounds: CGRect) -> WidgetPlacement {
        placement(center: CGPoint(x: bounds.midX, y: bounds.maxY - 12 * scale),
                  orientation: .horizontal, scale: scale, in: bounds)
    }

    static func dragged(center: CGPoint, expanded: Bool = false, scale: CGFloat = 1,
                        reveal: CGFloat? = nil, in bounds: CGRect) -> WidgetPlacement {
        let left = center.x - bounds.minX
        let right = bounds.maxX - center.x
        let orientation: WidgetOrientation
        if min(left, right) <= edgeSnap {
            orientation = left <= right ? .left : .right
        } else {
            orientation = .horizontal
        }
        return placement(center: center, orientation: orientation, expanded: expanded,
                         scale: scale, reveal: reveal, in: bounds)
    }

    static func placement(center: CGPoint, orientation: WidgetOrientation,
                          expanded: Bool = false, scale: CGFloat = 1,
                          reveal: CGFloat? = nil, in bounds: CGRect) -> WidgetPlacement {
        let size = orientation.size(expanded: expanded, scale: scale, reveal: reveal)
        let x: CGFloat
        switch orientation {
        case .horizontal: x = center.x - size.width / 2
        case .left: x = bounds.minX
        case .right: x = bounds.maxX - size.width
        }
        let origin = CGPoint(
            x: max(bounds.minX, min(x, bounds.maxX - size.width)),
            y: max(bounds.minY, min(center.y - size.height / 2, bounds.maxY - size.height))
        )
        return WidgetPlacement(frame: CGRect(origin: origin, size: size), orientation: orientation,
                               expanded: expanded, scale: scale)
    }

    static func resized(frame: CGRect, orientation: WidgetOrientation, expanded: Bool,
                        scale: CGFloat = 1, reveal: CGFloat? = nil, in bounds: CGRect) -> WidgetPlacement {
        let size = orientation.size(expanded: expanded, scale: scale, reveal: reveal)
        // Screen coordinates point upward; preserve the leading (top-left) lamp anchor.
        let x = orientation == .right && abs(frame.maxX - bounds.maxX) <= 1
            ? bounds.maxX - size.width : frame.minX
        let origin = CGPoint(x: max(bounds.minX, min(x, bounds.maxX - size.width)),
                             y: max(bounds.minY, min(frame.maxY - size.height, bounds.maxY - size.height)))
        return WidgetPlacement(frame: CGRect(origin: origin, size: size), orientation: orientation,
                               expanded: expanded, scale: scale)
    }

    static func screenIndex(at point: CGPoint, frames: [CGRect]) -> Int? {
        if let index = frames.firstIndex(where: { $0.contains(point) }) { return index }
        // Display gaps and unplugged displays fall back to the nearest remaining screen.
        return frames.indices.min { first, second in
            func distance(to frame: CGRect) -> CGFloat {
                let dx = max(frame.minX - point.x, 0, point.x - frame.maxX)
                let dy = max(frame.minY - point.y, 0, point.y - frame.maxY)
                return dx * dx + dy * dy
            }
            return distance(to: frames[first]) < distance(to: frames[second])
        }
    }
}

struct WidgetDrag {
    let centerOffset: CGPoint

    init(pointer: CGPoint, frame: CGRect) {
        centerOffset = CGPoint(x: pointer.x - frame.midX, y: pointer.y - frame.midY)
    }

    func center(at pointer: CGPoint) -> CGPoint {
        // Never derive this from the snapped/resized frame: that makes dragging stick or oscillate.
        CGPoint(x: pointer.x - centerOffset.x, y: pointer.y - centerOffset.y)
    }
}

struct WidgetResize {
    let pointer: CGPoint
    let frame: CGRect
    let orientation: WidgetOrientation
    let initialScale: CGFloat

    func scale(at point: CGPoint) -> CGFloat {
        let delta = orientation == .horizontal ? point.x - pointer.x : pointer.y - point.y
        return max(WidgetPreferences.scaleRange.lowerBound,
                   min(WidgetPreferences.scaleRange.upperBound, initialScale + delta / 84))
    }
}

enum WidgetDrawing {
    static func lamps(expanded: Bool) -> [TrafficState] {
        [.red, .yellow, .green]
    }

    static func draw(in context: CGContext, orientation: WidgetOrientation, expanded: Bool,
                     state: TrafficState, scale: CGFloat = 1, size: CGSize? = nil,
                     lampAlphas: [CGFloat]? = nil) {
        context.saveGState()
        defer { context.restoreGState() }
        context.scaleBy(x: scale, y: scale)
        let size = size ?? orientation.size(expanded: expanded, scale: scale)
        let bounds = CGRect(x: 0, y: 0, width: size.width / scale, height: size.height / scale)
        context.clear(bounds)
        context.setFillColor(CGColor(srgbRed: 17 / 255, green: 17 / 255, blue: 19 / 255, alpha: 1))
        context.addPath(CGPath(roundedRect: bounds, cornerWidth: 12, cornerHeight: 12, transform: nil))
        context.fillPath()
        for (index, lamp) in lamps(expanded: expanded).enumerated() {
            let center = orientation.lampCenters[index]
            context.setFillColor(lamp.ringColor)
            context.fillEllipse(in: CGRect(x: center.x - 7.5, y: center.y - 7.5, width: 15, height: 15))
            context.setFillColor(lamp.color(alpha: lampAlphas?[index] ?? (lamp == state ? 1 : 0.15)))
            context.fillEllipse(in: CGRect(x: center.x - 5.25, y: center.y - 5.25, width: 10.5, height: 10.5))
        }
    }

    static func drawClose(in context: CGContext, bounds: CGRect, scale: CGFloat = 1) {
        context.saveGState()
        defer { context.restoreGState() }
        context.scaleBy(x: scale, y: scale)
        let center = CGPoint(x: bounds.midX / scale, y: bounds.midY / scale)
        context.setFillColor(CGColor(srgbRed: 1, green: 1, blue: 1, alpha: 1))
        context.fillEllipse(in: CGRect(x: center.x - 5.25, y: center.y - 5.25, width: 10.5, height: 10.5))
        context.setStrokeColor(CGColor(srgbRed: 0, green: 0, blue: 0, alpha: 1))
        context.setLineWidth(0.65)
        context.setLineCap(.butt)
        context.move(to: CGPoint(x: center.x - 1.8, y: center.y - 1.8))
        context.addLine(to: CGPoint(x: center.x + 1.8, y: center.y + 1.8))
        context.move(to: CGPoint(x: center.x - 1.8, y: center.y + 1.8))
        context.addLine(to: CGPoint(x: center.x + 1.8, y: center.y - 1.8))
        context.strokePath()
    }

    static func drawResize(in context: CGContext, bounds: CGRect, orientation: WidgetOrientation,
                           scale: CGFloat = 1) {
        context.saveGState()
        defer { context.restoreGState() }
        context.translateBy(x: orientation == .horizontal ? bounds.minX - 2 * scale : bounds.midX,
                            y: orientation == .horizontal ? bounds.midY : bounds.minY - 2 * scale)
        context.scaleBy(x: scale, y: scale)
        context.setStrokeColor(CGColor(srgbRed: 0.55, green: 0.55, blue: 0.57, alpha: 1))
        context.setLineWidth(1.5)
        context.setLineCap(.butt)
        context.addArc(center: .zero, radius: 9,
                       startAngle: (orientation == .horizontal ? -70 : 20) * .pi / 180,
                       endAngle: (orientation == .horizontal ? 70 : 160) * .pi / 180, clockwise: false)
        context.strokePath()
    }
}

@MainActor
final class StatusPoller {
    static let interval: TimeInterval = 0.4
    static let disconnectGrace: TimeInterval = 5
    private let session: URLSession
    private let exitOnDisconnect: Bool
    private let now: @MainActor () -> TimeInterval
    private let onDisconnect: @MainActor () -> Void
    private let onState: @MainActor (TrafficState) -> Void
    private var timer: Timer?
    private var requestTask: Task<Void, Never>?
    private var lastValidResponse: TimeInterval = 0
    private(set) var state: TrafficState = .green

    init(configuration: URLSessionConfiguration = .ephemeral,
         exitOnDisconnect: Bool = true,
         now: @escaping @MainActor () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
         onDisconnect: @escaping @MainActor () -> Void,
         onState: @escaping @MainActor (TrafficState) -> Void) {
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.timeoutIntervalForRequest = 1
        configuration.timeoutIntervalForResource = 1
        session = URLSession(configuration: configuration)
        self.exitOnDisconnect = exitOnDisconnect
        self.now = now
        self.onDisconnect = onDisconnect
        self.onState = onState
    }

    deinit {
        session.invalidateAndCancel()
    }

    func start() {
        guard timer == nil else { return }
        lastValidResponse = now()
        let timer = Timer(timeInterval: Self.interval, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in self?.poll() }
        }
        self.timer = timer
        // Common modes keep polling while AppKit is tracking a mouse drag.
        RunLoop.main.add(timer, forMode: .common)
        poll()
    }

    func stop() {
        timer?.invalidate()
        timer = nil
        requestTask?.cancel()
        requestTask = nil
    }

    func poll() {
        guard timer != nil else { return }
        // Check even while a request is pending; failures must not extend the grace period.
        guard !exitOnDisconnect || now() - lastValidResponse < Self.disconnectGrace else {
            stop()
            onDisconnect()
            return
        }
        guard requestTask == nil else { return }
        var request = URLRequest(url: URL(string: "http://127.0.0.1:4390/status")!,
                                 cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 1)
        request.setValue("no-cache", forHTTPHeaderField: "Cache-Control")
        requestTask = Task { [weak self, session] in
            let next: TrafficState?
            do {
                let (data, response) = try await session.data(for: request)
                next = (response as? HTTPURLResponse).flatMap {
                    TrafficState.decode(data, statusCode: $0.statusCode)
                }
            } catch {
                next = nil
            }
            guard !Task.isCancelled, let self, self.timer != nil else { return }
            self.requestTask = nil
            if let next {
                self.lastValidResponse = self.now()
                if next != self.state {
                    self.state = next
                    self.onState(next)
                }
            }
        }
    }
}

@MainActor
final class TrafficLightPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }

    override func constrainFrameRect(_ frameRect: NSRect, to screen: NSScreen?) -> NSRect {
        // Geometry clamps to the pointer's screen rather than AppKit's previous screen.
        frameRect
    }
}

@MainActor
final class TrafficLightCloseButton: NSButton {
    private(set) var isTrackingMouse = false
    override var isFlipped: Bool { true }
    override var needsPanelToBecomeKey: Bool { false }
    override var acceptsFirstResponder: Bool { false }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func hitTest(_ point: NSPoint) -> NSView? {
        guard let view = superview as? TrafficLightView, view.controlsEnabled,
              view.orientation.controlContains(point, resize: false, scale: view.presentationScale) else { return nil }
        return super.hitTest(point) // AppKit supplies superview coordinates.
    }

    override func mouseDown(with event: NSEvent) {
        isTrackingMouse = true
        defer { isTrackingMouse = false }
        super.mouseDown(with: event)
    }

    override func draw(_ dirtyRect: NSRect) {
        guard let context = NSGraphicsContext.current?.cgContext,
              let view = superview as? TrafficLightView else { return }
        // Keep native button/cell semantics without painting its accessibility title.
        context.saveGState()
        view.clipControl(self, in: context)
        WidgetDrawing.drawClose(in: context, bounds: bounds, scale: view.presentationScale)
        context.restoreGState()
    }

    override func rightMouseDown(with event: NSEvent) {
        nextResponder?.rightMouseDown(with: event)
    }
}

@MainActor
final class TrafficLightResizeHandle: NSView {
    private var widget: TrafficLightView? { superview as? TrafficLightView }
    override var isFlipped: Bool { true }
    override var needsPanelToBecomeKey: Bool { false }
    override var acceptsFirstResponder: Bool { false }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func hitTest(_ point: NSPoint) -> NSView? {
        guard let widget, widget.controlsEnabled,
              widget.orientation.controlContains(point, resize: true, scale: widget.presentationScale) else { return nil }
        return super.hitTest(point)
    }

    override func resetCursorRects() {
        if let widget, widget.controlsEnabled {
            for rect in widget.orientation.resizeCursorRects(scale: widget.presentationScale) {
                addCursorRect(convert(rect, from: widget),
                              cursor: widget.orientation == .horizontal ? .resizeLeftRight : .resizeUpDown)
            }
        }
    }

    override func draw(_ dirtyRect: NSRect) {
        guard let context = NSGraphicsContext.current?.cgContext, let widget else { return }
        context.saveGState()
        widget.clipControl(self, in: context)
        WidgetDrawing.drawResize(in: context, bounds: bounds, orientation: widget.orientation,
                                 scale: widget.presentationScale)
        context.restoreGState()
    }

    override func mouseDown(with event: NSEvent) { widget?.beginResize(clickCount: event.clickCount) }
    override func mouseDragged(with event: NSEvent) { widget?.updateResize() }
    override func mouseUp(with event: NSEvent) { widget?.endResize() }
    override func rightMouseDown(with event: NSEvent) { widget?.rightMouseDown(with: event) }
    override func accessibilityPerformIncrement() -> Bool { widget?.adjustScale(by: 0.1) ?? false }
    override func accessibilityPerformDecrement() -> Bool { widget?.adjustScale(by: -0.1) ?? false }
}

@MainActor
final class TrafficLightView: NSView {
    var state: TrafficState = .green {
        didSet {
            guard state != oldValue else { return }
            advanceAnimations()
            for (index, lamp) in TrafficState.allCases.enumerated() {
                lampTweens[index].retarget(lamp == state ? 1 : 0.15, at: now(), duration: 0.120,
                                           animated: !reducedMotion())
            }
            needsDisplay = true
            updateAnimationTimer()
        }
    }
    private(set) var orientation: WidgetOrientation = .horizontal
    private(set) var expanded = false
    private(set) var scale: CGFloat = 1
    var drag: WidgetDrag?
    private(set) var resize: WidgetResize?
    let closeButton = TrafficLightCloseButton(frame: .zero)
    let resizeHandle = TrafficLightResizeHandle(frame: .zero)
    private let pointerLocation: @MainActor () -> CGPoint
    private let now: @MainActor () -> TimeInterval
    private let reducedMotion: @MainActor () -> Bool
    private let onScaleCommit: @MainActor (CGFloat) -> Void
    private let onClose: @MainActor () -> Void
    private var hoverTrackingArea: NSTrackingArea?
    private var revealTween = WidgetTween(0)
    private var scaleTween = WidgetTween(1)
    private var lampTweens = [WidgetTween(0.15), WidgetTween(0.15), WidgetTween(1)]
    private var geometryAnchor: CGRect?
    private var animationTimer: Timer?

    var presentationScale: CGFloat { scaleTween.value }
    var controlOpacity: CGFloat { revealTween.value }
    var lampAlphas: [CGFloat] { lampTweens.map(\.value) }
    var isAnimationTimerRunning: Bool { animationTimer != nil }
    var controlsEnabled: Bool {
        expanded && !revealTween.isAnimating && revealTween.value == 1 && drag == nil && resize == nil
    }

    private var usableBounds: CGRect? {
        guard let window else { return nil }
        let screens = NSScreen.screens
        let center = CGPoint(x: window.frame.midX, y: window.frame.midY)
        let fallback = WidgetGeometry.screenIndex(at: center, frames: screens.map(\.frame)).map { screens[$0] }
        return (window.screen ?? fallback)?.visibleFrame
    }

    override var isFlipped: Bool { true }
    override var needsPanelToBecomeKey: Bool { false }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    init(frame: CGRect, pointerLocation: @escaping @MainActor () -> CGPoint = { NSEvent.mouseLocation },
         now: @escaping @MainActor () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
         reducedMotion: @escaping @MainActor () -> Bool = { NSWorkspace.shared.accessibilityDisplayShouldReduceMotion },
         onScaleCommit: @escaping @MainActor (CGFloat) -> Void = { _ in },
         onClose: @escaping @MainActor () -> Void) {
        self.pointerLocation = pointerLocation
        self.now = now
        self.reducedMotion = reducedMotion
        self.onScaleCommit = onScaleCommit
        self.onClose = onClose
        super.init(frame: frame)
        closeButton.frame = orientation.closeButtonFrame
        closeButton.setButtonType(.momentaryPushIn)
        closeButton.isBordered = false
        closeButton.title = "Close traffic light"
        closeButton.cell?.setAccessibilityLabel("Close traffic light")
        closeButton.focusRingType = .none
        closeButton.setAccessibilityLabel("Close traffic light")
        closeButton.toolTip = "Close traffic light"
        closeButton.target = self
        closeButton.action = #selector(closeClicked(_:))
        addSubview(closeButton)
        resizeHandle.setAccessibilityElement(true)
        resizeHandle.setAccessibilityRole(.slider)
        resizeHandle.setAccessibilityLabel("Resize traffic light")
        resizeHandle.setAccessibilityMinValue(75)
        resizeHandle.setAccessibilityMaxValue(200)
        resizeHandle.toolTip = "Drag to resize; double-click to reset to 100%"
        resizeHandle.setAccessibilityHelp(resizeHandle.toolTip)
        addSubview(resizeHandle)
        layoutControls()
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func apply(_ placement: WidgetPlacement) {
        expanded = placement.expanded
        scale = WidgetPreferences.validated(placement.scale)
        scaleTween = WidgetTween(scale)
        revealTween = WidgetTween(expanded ? 1 : 0)
        geometryAnchor = nil
        applyFrame(placement)
        updateAnimationTimer()
    }

    private func applyFrame(_ placement: WidgetPlacement) {
        orientation = placement.orientation
        if let window {
            // AppKit rounds fractional content sizes upward to whole points. Round first so
            // error stays within one Retina backing pixel, retaining the leading/docked edge.
            let frame = placement.frame
            let size = CGSize(width: frame.width.rounded(), height: frame.height.rounded())
            let x = orientation == .right ? frame.maxX.rounded() - size.width : frame.minX.rounded()
            window.setFrame(CGRect(x: x, y: frame.maxY.rounded() - size.height,
                                   width: size.width, height: size.height), display: true)
        } else {
            setFrameSize(placement.frame.size)
        }
        layoutControls()
        needsDisplay = true
    }

    private func layoutControls() {
        closeButton.frame = orientation.controlFrame(resize: false, scale: presentationScale)
        resizeHandle.frame = orientation.controlFrame(resize: true, scale: presentationScale)
        for control in [closeButton as NSView, resizeHandle] {
            control.isHidden = controlOpacity == 0
            control.alphaValue = controlOpacity
            control.needsDisplay = true
        }
        closeButton.isEnabled = controlsEnabled
        resizeHandle.setAccessibilityEnabled(controlsEnabled)
        resizeHandle.setAccessibilityValue(presentationScale * 100)
        resizeHandle.setAccessibilityValueDescription("\(Int((presentationScale * 100).rounded()))%")
        window?.invalidateCursorRects(for: resizeHandle)
    }

    func clipControl(_ control: NSView, in context: CGContext) {
        let capsule = CGRect(x: -control.frame.minX, y: -control.frame.minY,
                             width: bounds.width, height: bounds.height)
        context.addPath(CGPath(roundedRect: capsule, cornerWidth: 12 * presentationScale,
                              cornerHeight: 12 * presentationScale, transform: nil))
        context.clip()
    }

    private func renderGeometry() {
        guard let window, let usableBounds else { return }
        applyFrame(WidgetGeometry.resized(frame: geometryAnchor ?? window.frame, orientation: orientation,
                                          expanded: expanded, scale: presentationScale,
                                          reveal: controlOpacity, in: usableBounds))
        if !revealTween.isAnimating && !scaleTween.isAnimating { geometryAnchor = nil }
    }

    func advanceAnimations() {
        let time = now()
        let reduced = reducedMotion()
        let geometryChanged = revealTween.isAnimating || scaleTween.isAnimating
        revealTween.advance(at: time, reducedMotion: reduced)
        scaleTween.advance(at: time, reducedMotion: reduced)
        for index in lampTweens.indices { lampTweens[index].advance(at: time, reducedMotion: reduced) }
        if geometryChanged { renderGeometry() }
        needsDisplay = true
        updateAnimationTimer()
    }

    private func updateAnimationTimer() {
        let animating = revealTween.isAnimating || scaleTween.isAnimating || lampTweens.contains { $0.isAnimating }
        guard animating, window != nil else {
            animationTimer?.invalidate()
            animationTimer = nil
            return
        }
        guard animationTimer == nil else { return }
        let timer = Timer(timeInterval: 1 / 60, repeats: true) { [weak self] timer in
            // This timer is installed only on the main run loop, including mouse tracking modes.
            let alive = MainActor.assumeIsolated {
                guard let self else { return false }
                self.advanceAnimations()
                return true
            }
            if !alive { timer.invalidate() }
        }
        animationTimer = timer
        RunLoop.main.add(timer, forMode: .common)
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        updateAnimationTimer()
    }

    private func setExpanded(_ expanded: Bool) {
        guard drag == nil, resize == nil, let window else { return }
        advanceAnimations()
        let target: CGFloat = expanded ? 1 : 0
        guard self.expanded != expanded || revealTween.target != target else { return }
        self.expanded = expanded
        geometryAnchor = window.frame
        revealTween.retarget(target, at: now(), duration: 0.150, animated: !reducedMotion())
        renderGeometry()
        updateAnimationTimer()
    }

    func updateHover() {
        guard drag == nil, resize == nil, let window else { return }
        // Resize can leave queued tracking events. Trust the current pointer, not their old rectangle.
        setExpanded(window.frame.contains(pointerLocation()))
    }

    override func updateTrackingAreas() {
        // inVisibleRect tracks resizes automatically; replacing it resets enter/exit state.
        if hoverTrackingArea == nil {
            let area = NSTrackingArea(rect: .zero,
                                      options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect,
                                                .enabledDuringMouseDrag],
                                      owner: self, userInfo: nil)
            addTrackingArea(area)
            hoverTrackingArea = area
        }
        super.updateTrackingAreas()
    }

    override func mouseEntered(with event: NSEvent) {
        updateHover()
    }

    override func mouseExited(with event: NSEvent) {
        updateHover()
    }

    override func mouseDown(with event: NSEvent) {
        guard resize == nil, let window else { return }
        let point = convert(window.convertPoint(fromScreen: pointerLocation()), from: nil)
        guard (orientation == .horizontal ? point.x : point.y) < 61 * presentationScale else { return }
        advanceAnimations()
        // Freeze reveal during a move, but finish any already-committed reset immediately.
        revealTween = WidgetTween(controlOpacity)
        scaleTween = WidgetTween(scale)
        geometryAnchor = window.frame
        renderGeometry()
        drag = WidgetDrag(pointer: pointerLocation(), frame: window.frame)
        layoutControls()
        updateAnimationTimer()
    }

    override func mouseDragged(with event: NSEvent) {
        guard let drag else { return }
        let pointer = pointerLocation()
        let screens = NSScreen.screens
        guard let index = WidgetGeometry.screenIndex(at: pointer, frames: screens.map(\.frame)) else {
            return
        }
        applyFrame(WidgetGeometry.dragged(center: drag.center(at: pointer), expanded: expanded,
                                          scale: presentationScale, reveal: controlOpacity,
                                          in: screens[index].visibleFrame))
    }

    override func mouseUp(with event: NSEvent) {
        drag = nil
        layoutControls()
        updateHover()
    }

    func beginResize(clickCount: Int) {
        guard controlsEnabled, let window else { return }
        if clickCount == 2 {
            commitScale(1, animated: true)
            return
        }
        advanceAnimations()
        scale = presentationScale
        scaleTween = WidgetTween(scale)
        geometryAnchor = nil
        resize = WidgetResize(pointer: pointerLocation(), frame: window.frame,
                              orientation: orientation, initialScale: scale)
        layoutControls()
        updateAnimationTimer()
    }

    func updateResize() {
        guard let resize, let usableBounds else { return }
        scale = resize.scale(at: pointerLocation())
        scaleTween = WidgetTween(scale)
        applyFrame(WidgetGeometry.resized(frame: resize.frame, orientation: resize.orientation,
                                          expanded: true, scale: scale, in: usableBounds))
    }

    func endResize() {
        guard resize != nil else { return }
        resize = nil
        onScaleCommit(scale)
        layoutControls()
        updateHover()
    }

    @discardableResult func adjustScale(by delta: CGFloat) -> Bool {
        guard controlsEnabled else { return false }
        let next = max(WidgetPreferences.scaleRange.lowerBound,
                       min(WidgetPreferences.scaleRange.upperBound, scale + delta))
        commitScale(next, animated: true)
        return true
    }

    private func commitScale(_ scale: CGFloat, animated: Bool) {
        advanceAnimations()
        self.scale = scale
        geometryAnchor = window?.frame
        scaleTween.retarget(scale, at: now(), duration: 0.150, animated: animated && !reducedMotion())
        renderGeometry()
        updateAnimationTimer()
        onScaleCommit(scale)
    }

    func cancelGestures() {
        drag = nil
        resize = nil
    }

    override func rightMouseDown(with event: NSEvent) {
        setExpanded(true)
    }

    @objc private func closeClicked(_ sender: NSButton) {
        guard sender === closeButton, controlsEnabled else { return }
        if closeButton.isTrackingMouse {
            // NSButtonCell tracks its rectangle; a mouse release on the surrounding arc must cancel.
            guard let window else { return }
            let point = convert(window.convertPoint(fromScreen: pointerLocation()), from: nil)
            guard orientation.controlContains(point, resize: false, scale: presentationScale) else { return }
        }
        onClose()
    }

    override func draw(_ dirtyRect: NSRect) {
        guard let context = NSGraphicsContext.current?.cgContext else { return }
        WidgetDrawing.draw(in: context, orientation: orientation, expanded: expanded, state: state,
                           scale: presentationScale, size: bounds.size, lampAlphas: lampAlphas)
    }
}

@MainActor
final class WidgetController: NSObject, NSApplicationDelegate {
    private var panel: TrafficLightPanel?
    private var poller: StatusPoller?
    private let preferencesURL: URL
    private let exitOnDisconnect: Bool

    init(preferencesURL: URL = WidgetPreferences.url(), exitOnDisconnect: Bool = false) {
        self.preferencesURL = preferencesURL
        self.exitOnDisconnect = exitOnDisconnect
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard let screen = NSScreen.main ?? NSScreen.screens.first else {
            NSApplication.shared.terminate(nil)
            return
        }
        let placement = WidgetGeometry.initial(scale: WidgetPreferences.load(from: preferencesURL),
                                                in: screen.visibleFrame)
        let panel = TrafficLightPanel(contentRect: placement.frame,
                                      styleMask: [.borderless, .nonactivatingPanel],
                                      backing: .buffered, defer: false)
        panel.level = .floating
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.isMovable = false
        panel.animationBehavior = .none
        panel.isReleasedWhenClosed = false
        let view = TrafficLightView(frame: CGRect(origin: .zero, size: placement.frame.size),
                                    onScaleCommit: { [weak self] scale in self?.commitScale(scale) }) {
            [weak self] in self?.close()
        }
        panel.contentView = view
        view.apply(placement)
        self.panel = panel
        panel.orderFrontRegardless()

        let poller = StatusPoller(exitOnDisconnect: exitOnDisconnect, onDisconnect: { [weak self] in self?.close() }) {
            [weak view] state in view?.state = state
        }
        self.poller = poller
        poller.start()
        NotificationCenter.default.addObserver(self, selector: #selector(screensChanged),
                                               name: NSApplication.didChangeScreenParametersNotification,
                                               object: nil)
    }

    private func close() {
        poller?.stop()
        NSApplication.shared.terminate(nil)
    }

    private func commitScale(_ scale: CGFloat) {
        WidgetPreferences.save(scale, to: preferencesURL)
    }

    @objc private func screensChanged(_ notification: Notification) {
        guard let panel, let view = panel.contentView as? TrafficLightView else { return }
        let center = CGPoint(x: panel.frame.midX, y: panel.frame.midY)
        let screens = NSScreen.screens
        guard let index = WidgetGeometry.screenIndex(at: center, frames: screens.map(\.frame)) else {
            return
        }
        view.cancelGestures()
        view.apply(WidgetGeometry.placement(center: center, orientation: view.orientation,
                                            expanded: view.expanded, scale: view.scale,
                                            in: screens[index].visibleFrame))
        view.updateHover()
    }

    func applicationWillTerminate(_ notification: Notification) {
        poller?.stop()
        NotificationCenter.default.removeObserver(self)
    }
}

#if !WIDGET_TESTING
@main
enum TrafficLightApp {
    @MainActor static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let controller = WidgetController(exitOnDisconnect: CommandLine.arguments.contains("--exit-on-disconnect"))
        app.delegate = controller
        withExtendedLifetime(controller) { app.run() }
    }
}
#endif
