#!/bin/sh
# Builds the injectable fix libraries (needs macOS + Xcode). Output: fixes/bin/*.dylib
set -eu
cd "$(dirname "$0")"
mkdir -p bin
SDK=$(xcrun --sdk iphoneos --show-sdk-path)
xcrun --sdk iphoneos clang -arch arm64 -isysroot "$SDK" -miphoneos-version-min=14.0 \
  -dynamiclib -fobjc-arc -O2 -Wall -Werror \
  -framework Foundation -framework UIKit -framework UniformTypeIdentifiers \
  -install_name @executable_path/FilePickerFix.dylib \
  -o bin/FilePickerFix.dylib filepicker/FilePickerFix.m
# ad-hoc signature; zsign re-signs it with the user's certificate when injecting
codesign -f -s - bin/FilePickerFix.dylib
file bin/FilePickerFix.dylib
otool -L bin/FilePickerFix.dylib
