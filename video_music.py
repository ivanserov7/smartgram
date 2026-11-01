"""Optional local prepared-track adapter; rights, hash, ducking and fixed voice clock."""

from pathlib import Path
from commons_api import accepted_license
from media_tools import run_command
from production_state import ProductionError, inside, read, sha


def prepare_music(root, audio, duration, settings, media, journal):
    root = Path(root)
    asset_id = settings.get("asset_id")
    if (
        not isinstance(asset_id, str)
        or not asset_id
        or "/" in asset_id
        or "\\" in asset_id
    ):
        raise ProductionError("Enabled music requires a prepared asset_id.")
    manifest = read(inside(root, f"music/{asset_id}.json"))
    track = inside(root, manifest["path"])
    if (
        manifest.get("approved_rights") is not True
        or not manifest.get("author")
        or not manifest.get("title")
        or not manifest.get("source_url")
        or not accepted_license(
            manifest.get("license", ""), manifest.get("license_url", "")
        )
    ):
        raise ProductionError("Music rights/attribution not approved.")
    if sha(track) != manifest.get("sha256"):
        raise ProductionError("Music track hash mismatch.")
    target = root / "mixed_audio.wav"
    gain = settings["relative_gain_db"]
    if gain > 0 or gain < -80:
        raise ProductionError("Music gain must be between -80 and 0 dB.")
    filters = (
        f"[1:a]volume={gain}dB,atrim=duration={duration},asetpts=PTS-STARTPTS[music];"
    )
    if settings["duck_under_voice"]:
        filters += "[0:a]asplit[voice][key];[music][key]sidechaincompress=threshold=0.02:ratio=8:attack=20:release=250[duck];[voice][duck]amix=inputs=2:duration=first:normalize=0[out]"
    else:
        filters += "[0:a][music]amix=inputs=2:duration=first:normalize=0[out]"
    run_command(
        [
            media.ffmpeg,
            "-hide_banner",
            "-y",
            "-i",
            audio,
            "-stream_loop",
            "-1",
            "-i",
            track,
            "-filter_complex",
            filters,
            "-map",
            "[out]",
            "-c:a",
            "pcm_s16le",
            "-ar",
            "48000",
            target,
        ],
        timeout=300,
        journal=journal,
    )
    from file_io import atomic_write_json

    atomic_write_json(root / "music_attribution.json", manifest)
    return target
