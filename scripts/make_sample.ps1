# Generate a sample meeting recording (Windows SAPI TTS) for testing Eclipse.
# Usage:  powershell -File scripts/make_sample.ps1 [-OutPath inbox\2026-06-17-sample.wav]
param(
    [string]$OutPath = "inbox\2026-06-17-acme-standup.wav"
)

$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $OutPath
if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
$full = Join-Path (Get-Location) $OutPath

$script = @"
Okay, let's kick off the Acme weekly standup. Present today are Tom and Jane.
First decision: we agreed to approve the ten percent discount on the renewal.
Action item: Tom will send the revised proposal to Acme by Friday.
Jane, can you review the budget numbers before the next call?
We still need to confirm the contract terms with the legal team. Thanks everyone.
"@

Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.SetOutputToWaveFile($full)
$synth.Speak($script)
$synth.Dispose()
Write-Host "Wrote sample recording to $full"
