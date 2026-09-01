#!/bin/bash

# Validate the exact code-signing properties that Squirrel.Mac/ShipIt uses
# when it decides whether a downloaded update may replace the running app.
# A plain codesign integrity check is insufficient: an implicit ad-hoc
# designated requirement is tied to one build's CDHash and breaks on the next
# version. The fallback requirement below is explicit and version-independent.

set -euo pipefail

APP_PATH="${1:-}"
EXPECTED_BUNDLE_ID="${WORDTTS_MAC_BUNDLE_ID:-com.wordtts.desktop}"

fail() {
    echo "[macOS 更新签名错误] $1" >&2
    exit 1
}

if [ "$(uname -s)" != "Darwin" ]; then
    fail "签名校验只能在 macOS 上运行"
fi
if [ -z "$APP_PATH" ] || [ ! -d "$APP_PATH" ]; then
    fail "未找到待校验的应用: ${APP_PATH:-<empty>}"
fi

bundle_id="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP_PATH/Contents/Info.plist" 2>/dev/null || true)"
if [ "$bundle_id" != "$EXPECTED_BUNDLE_ID" ]; then
    fail "Bundle ID 不匹配，期望 $EXPECTED_BUNDLE_ID，实际 ${bundle_id:-<empty>}"
fi

echo "  校验包体完整性: $APP_PATH"
codesign --verify --deep --strict "$APP_PATH"

signature_details="$(codesign --display --verbose=4 "$APP_PATH" 2>&1)"
signature_identifier="$(printf '%s\n' "$signature_details" | awk -F= '$1 == "Identifier" { print substr($0, index($0, "=") + 1); exit }')"
if [ "$signature_identifier" != "$EXPECTED_BUNDLE_ID" ]; then
    fail "代码签名 Identifier 不匹配，期望 $EXPECTED_BUNDLE_ID，实际 ${signature_identifier:-<empty>}"
fi

requirement_details="$(codesign --display --requirements - "$APP_PATH" 2>&1)"
designated_requirement="$(printf '%s\n' "$requirement_details" | awk '/^(# )?designated => / { print; exit }')"
if [ -z "$designated_requirement" ]; then
    fail "应用没有可读取的 designated requirement"
fi

if printf '%s\n' "$signature_details" | grep -Fq 'Authority=Developer ID Application'; then
    team_identifier="$(printf '%s\n' "$signature_details" | awk -F= '$1 == "TeamIdentifier" { print substr($0, index($0, "=") + 1); exit }')"
    if [ -z "$team_identifier" ] || [ "$team_identifier" = "not set" ]; then
        fail "Developer ID 签名缺少 TeamIdentifier"
    fi
    echo "  更新签名模式: Developer ID Application (Team $team_identifier)"
    echo "  designated requirement: $designated_requirement"
    exit 0
fi

if ! printf '%s\n' "$signature_details" | grep -Fq 'Signature=adhoc'; then
    fail "既不是 Developer ID，也不是预期的 ad-hoc 签名"
fi

canonical_requirement="designated => identifier \"$EXPECTED_BUNDLE_ID\""
if [ "$designated_requirement" != "$canonical_requirement" ]; then
    fail "ad-hoc designated requirement 不稳定；期望 '$canonical_requirement'，实际 '$designated_requirement'"
fi

# This is the same external requirement represented by the old app's explicit
# designated requirement. Verifying the final app against it exercises the
# identity check that ShipIt performs after extracting the update ZIP.
compatibility_requirement="=identifier \"$EXPECTED_BUNDLE_ID\""
codesign --verify --deep --strict -R "$compatibility_requirement" "$APP_PATH"

echo "  更新签名模式: stable ad-hoc"
echo "  designated requirement: $designated_requirement"
echo "  ShipIt 跨版本签名要求验证通过 ✓"
