// Renders the helper's app icon (the image macOS shows beside its notifications)
// as an .iconset directory for iconutil: an image (a .png path) clipped to the
// macOS rounded square, or an SF Symbol on a color.
//   Icon <image.png | symbol> <hex color> <output.iconset>
import AppKit

let args = CommandLine.arguments
guard args.count == 4, let rgb = UInt32(args[2], radix: 16) else {
    FileHandle.standardError.write("usage: Icon <symbol> <rrggbb> <output.iconset>\n".data(using: .utf8)!)
    exit(2)
}
let color = NSColor(srgbRed: CGFloat((rgb >> 16) & 0xff) / 255, green: CGFloat((rgb >> 8) & 0xff) / 255,
                    blue: CGFloat(rgb & 0xff) / 255, alpha: 1)
let output = URL(fileURLWithPath: args[3])
let artwork: NSImage? = args[1].hasSuffix(".png") ? NSImage(contentsOfFile: args[1]) : nil
if args[1].hasSuffix(".png") && artwork == nil { FileHandle.standardError.write("cannot read \(args[1])\n".data(using: .utf8)!); exit(1) }

func render(_ pixels: Int) -> Data? {
    guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels, bitsPerSample: 8,
                                     samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                                     bytesPerRow: 0, bitsPerPixel: 0) else { return nil }
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let size = CGFloat(pixels)
    // macOS icon grid: the shape fills about 80% of the canvas.
    let inset = size * 0.1
    let tile = NSRect(x: inset, y: inset, width: size - 2 * inset, height: size - 2 * inset)
    let shape = NSBezierPath(roundedRect: tile, xRadius: tile.width * 0.225, yRadius: tile.width * 0.225)
    if let image = artwork {
        NSGraphicsContext.current?.imageInterpolation = .high
        shape.addClip()
        image.draw(in: tile)
        NSGraphicsContext.restoreGraphicsState()
        return rep.representation(using: .png, properties: [:])
    }
    NSGradient(starting: color.blended(withFraction: 0.18, of: .white)!, ending: color.blended(withFraction: 0.12, of: .black)!)!
        .draw(in: shape, angle: -90)
    let config = NSImage.SymbolConfiguration(pointSize: tile.width * 0.52, weight: .bold)
        .applying(.init(paletteColors: [.white]))
    if let symbol = NSImage(systemSymbolName: args[1], accessibilityDescription: nil)?.withSymbolConfiguration(config) {
        let s = symbol.size
        symbol.draw(in: NSRect(x: tile.midX - s.width / 2, y: tile.midY - s.height / 2, width: s.width, height: s.height))
    }
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])
}

try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
for points in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let name = scale == 1 ? "icon_\(points)x\(points).png" : "icon_\(points)x\(points)@2x.png"
        guard let data = render(points * scale) else { exit(1) }
        try data.write(to: output.appendingPathComponent(name))
    }
}
