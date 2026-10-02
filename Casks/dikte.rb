cask "dikte" do
  arch arm: "arm64", intel: "x86_64"

  version "2.0.0"
  sha256 arm:   "b4af6730003956c6d8bb4c99a254178d6d14f6f17ee193a72cd71a8b70b13f32",
         intel: "3b8c8015bcea1da557e7eabed11bde8bd2cbba5f4f342c4c8cd3115a2fd1db3e"

  url "https://github.com/yusufipk/dikte/releases/download/v#{version}/Dikte-#{version}-#{arch}.dmg"
  name "Dikte"
  desc "Dictation and transcription from the menu bar"
  homepage "https://github.com/yusufipk/dikte"

  livecheck do
    url :url
    strategy :github_latest
  end

  depends_on macos: :big_sur

  app "Dikte.app"
  binary "#{appdir}/Dikte.app/Contents/MacOS/Dikte", target: "dikte"

  caveats <<~EOS
    Dikte is ad-hoc signed and is not notarized. macOS may block its first launch.
    Review the application in System Settings > Privacy & Security before opening it.
    Microphone and Accessibility access require your consent and may be requested again after updates.

    Quit Dikte and turn off its start-at-login option before uninstalling.
    Settings, history, recordings, models, and existing login items are preserved.
  EOS
end
