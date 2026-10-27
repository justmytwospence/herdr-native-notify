#!/usr/bin/env python3
"""Build clean, staged, ad-hoc signed helpers with stable notification identities."""
from pathlib import Path
import json
import plistlib
import shutil
import subprocess
import tempfile

from install import APPS, LEGACY, PREFIX, RETIRED, rename_bundle, validate_app

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / 'build'
# One sender for every alert, so Notification Center stacks them by Herdr space
# (the thread) rather than by state. The app icon is Herdr's logo (assets/,
# from https://herdr.dev/assets/logo.png) in the macOS rounded square; each
# alert's title leads with Herdr's state glyph (bridge.py).
APP_ICON = (str(ROOT / 'assets/herdr-logo.png'), '000000')
DISPLAY_NAME = 'Herdr'


ACTOOL = '/Applications/Xcode.app/Contents/Developer/usr/bin/actool'


def compile_asset_icon(iconset, catalog, resources):
    """Assets.car with an AppIcon set from the rendered iconset PNGs (needs Xcode's actool).

    Without full Xcode the helper still works, with only the .icns icon (which
    macOS 26 Notification Center may show blank).
    """
    if not Path(ACTOOL).exists():
        print('build: ' + ACTOOL + ' missing; skipping the asset-catalog icon')
        return
    icon = catalog / 'AppIcon.appiconset'
    icon.mkdir(parents=True)
    (catalog / 'Contents.json').write_text(json.dumps({'info': {'author': 'xcode', 'version': 1}}))
    images = []
    for points in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            name = 'icon_%dx%d%s.png' % (points, points, '' if scale == 1 else '@2x')
            shutil.copy2(iconset / name, icon / name)
            images.append({'idiom': 'mac', 'size': '%dx%d' % (points, points), 'scale': '%dx' % scale, 'filename': name})
    (icon / 'Contents.json').write_text(json.dumps({'images': images, 'info': {'author': 'xcode', 'version': 1}}))
    subprocess.run([ACTOOL, '--compile', str(resources), '--platform', 'macosx', '--minimum-deployment-target', '13.0',
                    '--app-icon', 'AppIcon', '--output-partial-info-plist', str(catalog.parent / (catalog.stem + '-icon.plist')),
                    str(catalog)], check=True, capture_output=True)


def build(root=None, output=None):
    root = Path(root) if root is not None else ROOT
    output = Path(output) if output is not None else BUILD
    if output.is_symlink() or (output.exists() and not output.is_dir()):
        raise RuntimeError('Unsafe build directory: ' + str(output))
    # Refuse foreign bundles, including the pre-rename generated helper.
    for kind, name in APPS:
        app = output / (name + '.app')
        if app.exists() or app.is_symlink():
            validate_app(app, kind)
    legacy = output / LEGACY
    if legacy.exists() or legacy.is_symlink():
        validate_app(legacy, 'blocked')
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.herdr-build-', dir=output) as temporary:
        stage = Path(temporary)
        executable = stage / 'Helper'
        subprocess.run(['swiftc', '-swift-version', '5', '-O', str(root / 'Helper.swift'),
                        '-o', str(executable)], check=True)
        renderer = stage / 'Icon'
        subprocess.run(['swiftc', '-swift-version', '5', '-O', str(root / 'Icon.swift'),
                        '-o', str(renderer)], check=True)
        for kind, name in APPS:
            app = stage / (name + '.app')
            (app / 'Contents/MacOS').mkdir(parents=True)
            (app / 'Contents/Resources').mkdir()
            shutil.copy2(executable, app / 'Contents/MacOS/Helper')
            # The helper invokes this embedded script's "focus" command on clicks.
            shutil.copy2(root / 'bridge.py', app / 'Contents/Resources/bridge.py')
            shutil.copy2(root / 'remote_connection.py', app / 'Contents/Resources/remote_connection.py')
            iconset = stage / (kind + '.iconset')
            subprocess.run([str(renderer), *APP_ICON, str(iconset)], check=True)
            subprocess.run(['/usr/bin/iconutil', '-c', 'icns', str(iconset), '-o',
                            str(app / 'Contents/Resources/AppIcon.icns')], check=True)
            # macOS 26 Notification Center takes the icon from an asset catalog
            # (CFBundleIconName, Assets.car); a bare .icns showed a blank tile.
            compile_asset_icon(iconset, stage / (kind + '.xcassets'), app / 'Contents/Resources')
            info = {
                'CFBundleIdentifier': PREFIX + kind,
                'CFBundleName': DISPLAY_NAME, 'CFBundleDisplayName': DISPLAY_NAME,
                'CFBundleExecutable': 'Helper', 'CFBundlePackageType': 'APPL',
                'CFBundleVersion': '5', 'CFBundleShortVersionString': '0.5.0',
                'LSUIElement': True, 'LSMinimumSystemVersion': '13.0',
                'HerdrNotificationKind': kind, 'CFBundleIconFile': 'AppIcon', 'CFBundleIconName': 'AppIcon',
                'NSAppleEventsUsageDescription': 'Focus the Ghostty terminal containing the selected Herdr agent.',
            }
            with (app / 'Contents/Info.plist').open('wb') as stream:
                plistlib.dump(info, stream)
            subprocess.run(['/usr/bin/codesign', '--sign', '-', '--identifier', PREFIX + kind,
                            str(app)], check=True)
            validate_app(app, kind)
        # Never publish partially assembled or unsigned bundles.
        for kind, name in APPS:
            staged = stage / (name + '.app')
            target = output / staged.name
            replacing = target.exists() or target.is_symlink()
            if replacing:
                validate_app(target, kind)
            rename_bundle(staged, target, exchange=replacing)
            try:
                validate_app(target, kind)
            except Exception:
                rename_bundle(target, staged, exchange=replacing)
                raise
            print(target)
        if legacy.exists():
            validate_app(legacy, 'blocked')
            shutil.rmtree(legacy)
        for kind, name in RETIRED:
            retired = output / (name + '.app')
            if retired.exists():
                validate_app(retired, kind)
                shutil.rmtree(retired)


if __name__ == '__main__':
    build()
