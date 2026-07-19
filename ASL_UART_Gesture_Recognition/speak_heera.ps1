param (
    [string]$text
)

Add-Type -AssemblyName System.Speech
$synthesizer = New-Object System.Speech.Synthesis.SpeechSynthesizer

# --- ADJUST SPEECH SPEED HERE ---
# Default is 0. -10 is slowest, 10 is fastest.
# -2 to -3 is perfect for clear, natural sign-language translations.
$synthesizer.Rate = -6

# Query the list of installed OS voices
$voices = $synthesizer.GetInstalledVoices()
$targetVoice = $null

# Check for the specified voice, otherwise fallback gracefully
foreach ($v in $voices) {
    if ($v.VoiceInfo.Name -like "*Heera*") {
        $targetVoice = $v.VoiceInfo.Name
        break
    }
}

if ($targetVoice) {
    $synthesizer.SelectVoice($targetVoice)
}

$synthesizer.Speak($text)