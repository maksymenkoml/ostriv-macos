# Bug report for CodeWeavers: winemac window surface forces a per-frame CPU ColorSync conversion on non-sRGB displays

Draft to post on the CrossOver forum / file with CodeWeavers. Self-contained; the
project it comes from is https://github.com/maksymenkoml/ostriv-macos.

---

## Summary

The Mac driver's window surface reaches CoreAnimation as a `CGImage` tagged with an
sRGB colorspace. When the display's ColorSync profile is anything else — a factory
wide-gamut ICC profile (typical for external monitors), a calibration profile, or
Apple's P3 panels — CoreAnimation cannot use its zero-copy path and **re-renders every
window-surface flush through a per-pixel ColorSync conversion on the CPU**, on the
process main thread, single-threaded (and under Rosetta for x86_64 bottles).

For games that present through the GDI window-surface path this becomes the frame-rate
ceiling: at 3008×1692 (~20 MB/frame) the main thread saturates at **~10 fps** no matter
how fast the game renders. Total process CPU looks low (one busy thread), which makes
the bottleneck easy to misattribute.

## Environment

- CrossOver 26.2, macOS 26.5, Apple M5 Max
- Display: BenQ PD3226G at 6016×3384 ("looks like" 3008×1692), factory ICC profile
- Game: Ostriv (Steam appid 773790), rendering via a Mesa `opengl32` (GL→D3D12→D3DMetal)
  that presents through GDI `StretchDIBits` into the winemac window surface. The issue is
  not specific to that setup — anything that flushes a large window surface every frame
  hits it.

## Evidence

`sample <pid>` of the game process while FPS sat at ~10; the main thread spent
2030 of 2131 samples (95%) inside the colorspace conversion:

```
CA::Transaction::commit
  CA::Context::commit_transaction
    CA::Layer::prepare_commit → CA::Render::prepare_image → CA::Render::copy_image
      create_image_by_rendering            ← CA can't use the image as-is
        CGContextDrawImage
          ripc_AcquireRIPImageData → CGSImageDataLock → img_data_lock → img_raw_read
            CGColorTransformConvertUsingCMSConverter   ← 95% of main-thread time
              convert_using_vImageConverter → vImageConverterConvert
```

Switching the display profile to "sRGB IEC61966-2.1" in System Settings (making source
and destination colorspaces match) removes the conversion completely — re-sampling shows
the main thread 94% idle, the CA commit ~50× cheaper, and in-game FPS jumps from ~10 to
30–60 immediately, with the game still running. Switching the profile back restores the
~10 fps ceiling. This isolates the colorspace mismatch as the entire cost.

## Suggested fix

When creating the `CGImage` for the window surface, tag it with the display's actual
colorspace (`CGDisplayCopyColorSpace(displayID)`) instead of a fixed sRGB colorspace.
CoreAnimation then takes its fast path on every profile, and visually this matches what
users get today on sRGB-profiled displays (the pixels are interpreted as display-native
in both cases — no conversion happens either way). Alternatively, expose it as a Mac
Driver registry toggle if strict sRGB tagging must stay the default.

## Workaround we ship meanwhile

Our patcher generates a launcher that sets the display's ColorSync profile to sRGB for
the duration of the game session via `ColorSyncDeviceSetCustomProfiles` and restores the
user's profile on exit. It works, but it changes the user's display rendering globally
while playing — a driver-side fix would make it unnecessary.

## Unrelated note worth flagging

While debugging this we also confirmed (CrossOver 26.2) that a DXGI/D3DMetal swapchain
present from a Mesa d3d12 `opengl32` never gets composited into the winemac window (the
window stays transparent; `D3DM_SHOW_HUD_STATS` shows nothing). We work around it by
forcing Mesa's GDI-compatible present path (`wgl_require_gdi_compat`). Happy to file
separately with details if useful.
