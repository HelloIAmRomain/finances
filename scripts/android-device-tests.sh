#!/usr/bin/env bash
# Tests instrumentés de l'application Android sur un vrai système Android (émulateur API 34,
# accélération KVM), dans l'image du SDK : rien à installer sur le poste.
# Usage : scripts/android-device-tests.sh    (depuis la racine du dépôt ; /dev/kvm requis)
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
[[ -e /dev/kvm ]] || {
	echo "/dev/kvm absent : émulateur impossible sans virtualisation matérielle" >&2
	exit 1
}
uid="$(id -u)"
gid="$(id -g)"
exec docker run --rm --device /dev/kvm -v "$root/android:/p" -w /p \
	-v finances-android-sdk:/opt/android-sdk-linux \
	-e GRADLE_USER_HOME=/p/.gradle-home \
	ghcr.io/cirruslabs/android-sdk:35 bash -c "
set -euo pipefail
trap 'chown -R $uid:$gid /p/.gradle-home /p/.gradle /p/app/build 2>/dev/null || true' EXIT
image='system-images;android-34;google_apis;x86_64'
yes | sdkmanager --licenses > /dev/null 2>&1 || true
sdkmanager --install emulator platform-tools \"\$image\" > /dev/null
echo no | avdmanager create avd --force -n essai -k \"\$image\" -d pixel_6 > /dev/null
emulator -avd essai -no-window -no-audio -no-boot-anim -no-snapshot -gpu swiftshader_indirect \
	-memory 3072 > /tmp/emulateur.log 2>&1 &
adb wait-for-device
until [ \"\$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')\" = 1 ]; do sleep 3; done
for animation in window_animation_scale transition_animation_scale animator_duration_scale; do
	adb shell settings put global \$animation 0
done
echo \"émulateur prêt : \$(adb shell getprop ro.build.version.release | tr -d '\r')\"
./gradlew --no-daemon -q connectedDebugAndroidTest
"
