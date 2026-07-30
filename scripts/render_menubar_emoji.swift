import AppKit
import Foundation

guard CommandLine.arguments.count == 2 else {
    fputs("usage: render_menubar_emoji.swift OUTPUT.png\n", stderr)
    exit(2)
}

let logicalSize = NSSize(width: 32, height: 32)
let pixels = 128
guard let bitmap = NSBitmapImageRep(
    bitmapDataPlanes: nil,
    pixelsWide: pixels,
    pixelsHigh: pixels,
    bitsPerSample: 8,
    samplesPerPixel: 4,
    hasAlpha: true,
    isPlanar: false,
    colorSpaceName: .deviceRGB,
    bytesPerRow: 0,
    bitsPerPixel: 0
) else {
    fatalError("Could not create the bitmap")
}
bitmap.size = logicalSize

NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
NSColor.clear.setFill()
NSRect(origin: .zero, size: logicalSize).fill()

let text = "🎙️" as NSString
let attributes: [NSAttributedString.Key: Any] = [
    .font: NSFont(name: "Apple Color Emoji", size: 24)
        ?? NSFont.systemFont(ofSize: 24),
]
let measured = text.size(withAttributes: attributes)
let point = NSPoint(
    x: (logicalSize.width - measured.width) / 2,
    y: (logicalSize.height - measured.height) / 2 + 1
)
text.draw(at: point, withAttributes: attributes)
NSGraphicsContext.restoreGraphicsState()

guard let png = bitmap.representation(using: .png, properties: [:]) else {
    fatalError("Could not encode PNG")
}
try png.write(to: URL(fileURLWithPath: CommandLine.arguments[1]),
              options: .atomic)
