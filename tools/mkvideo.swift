// mkvideo.swift — assemble a directory of sequential JPEG frames into an H.264 .mp4
// usage: mkvideo <frame-dir> <fps> <out.mp4>
import Foundation
import AVFoundation
import CoreGraphics
import ImageIO

let args = CommandLine.arguments
guard args.count == 4, let fps = Int32(args[2]) else {
    FileHandle.standardError.write("usage: mkvideo <frame-dir> <fps> <out.mp4>\n".data(using: .utf8)!)
    exit(2)
}
let dir = args[1], outPath = args[3]
let fm = FileManager.default
let frames = (try? fm.contentsOfDirectory(atPath: dir))?
    .filter { $0.hasSuffix(".jpg") }.sorted() ?? []
guard !frames.isEmpty else { FileHandle.standardError.write("no .jpg frames\n".data(using: .utf8)!); exit(3) }

// dimensions from the first frame
func loadCG(_ path: String) -> CGImage? {
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil) else { return nil }
    return CGImageSourceCreateImageAtIndex(src, 0, nil)
}
guard let first = loadCG(dir + "/" + frames[0]) else { exit(4) }
let W = first.width, H = first.height

try? fm.removeItem(atPath: outPath)
let writer = try! AVAssetWriter(outputURL: URL(fileURLWithPath: outPath), fileType: .mp4)
let settings: [String: Any] = [
    AVVideoCodecKey: AVVideoCodecType.h264,
    AVVideoWidthKey: W, AVVideoHeightKey: H,
]
let input = AVAssetWriterInput(mediaType: .video, outputSettings: settings)
input.expectsMediaDataInRealTime = false
let attrs: [String: Any] = [
    kCVPixelBufferPixelFormatTypeKey as String: Int(kCVPixelFormatType_32BGRA),
    kCVPixelBufferWidthKey as String: W, kCVPixelBufferHeightKey as String: H,
]
let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: attrs)
writer.add(input)
writer.startWriting()
writer.startSession(atSourceTime: .zero)

func pixelBuffer(_ cg: CGImage) -> CVPixelBuffer? {
    var pb: CVPixelBuffer?
    CVPixelBufferCreate(kCFAllocatorDefault, W, H, kCVPixelFormatType_32BGRA, attrs as CFDictionary, &pb)
    guard let buf = pb else { return nil }
    CVPixelBufferLockBaseAddress(buf, [])
    let ctx = CGContext(data: CVPixelBufferGetBaseAddress(buf), width: W, height: H,
                        bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(buf),
                        space: CGColorSpaceCreateDeviceRGB(),
                        bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue)
    ctx?.draw(cg, in: CGRect(x: 0, y: 0, width: W, height: H))
    CVPixelBufferUnlockBaseAddress(buf, [])
    return buf
}

var i: Int32 = 0
for f in frames {
    guard let cg = loadCG(dir + "/" + f), let pb = pixelBuffer(cg) else { continue }
    while !input.isReadyForMoreMediaData { usleep(2000) }
    adaptor.append(pb, withPresentationTime: CMTimeMake(value: Int64(i), timescale: fps))
    i += 1
}
input.markAsFinished()
let sem = DispatchSemaphore(value: 0)
writer.finishWriting { sem.signal() }
sem.wait()
print("wrote \(outPath) — \(i) frames @ \(fps)fps, \(W)x\(H)")
exit(writer.status == .completed ? 0 : 1)
