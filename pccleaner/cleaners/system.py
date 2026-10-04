"""Windows system junk: temp folders, crash dumps, caches, recycle bin."""
from __future__ import annotations

import os

from .. import winutil
from ..core import Custom, Files, Target
from ..fsutil import children, glob_in
from ..stats import Stats

GROUP = "Windows"


def _recycle_scan() -> Stats:
    size, count = winutil.recycle_bin_info()
    return Stats(files=count, bytes=size)


def _recycle_clean() -> Stats:
    stats = _recycle_scan()
    winutil.empty_recycle_bin()
    return stats


def targets():
    env = os.environ
    windir = env.get("SystemRoot", r"C:\Windows")
    local = env["LOCALAPPDATA"]
    progdata = env.get("ProgramData", r"C:\ProgramData")
    user_temp = env.get("TEMP") or os.path.join(local, "Temp")

    yield Target(
        "win.user_temp", GROUP, "User temporary files",
        f"Contents of {user_temp}. Files changed in the last hour are kept so running "
        "programs are not disturbed.",
        [Files(lambda: children(user_temp), min_age_hours=1)])

    yield Target(
        "win.system_temp", GROUP, "Windows temporary files",
        rf"Contents of {windir}\Temp (older than 1 hour).",
        [Files(lambda: children(os.path.join(windir, "Temp")), min_age_hours=1)],
        admin=True)

    yield Target(
        "win.recycle_bin", GROUP, "Empty Recycle Bin",
        "Permanently deletes everything in the Recycle Bin on all drives.",
        [Custom(_recycle_scan, _recycle_clean)], default=False)

    wer_dirs = [os.path.join(local, r"Microsoft\Windows\WER"),
                os.path.join(progdata, r"Microsoft\Windows\WER")]
    yield Target(
        "win.error_reports", GROUP, "Error reports & crash dumps",
        "Windows Error Reporting archives and application crash dumps.",
        [Files(lambda: [p for d in wer_dirs for p in glob_in(d, "ReportArchive\\*", "ReportQueue\\*", "Temp\\*")]
               + children(os.path.join(local, "CrashDumps")))])

    yield Target(
        "win.shader_cache", GROUP, "DirectX shader cache",
        "Compiled graphics shaders; games and apps rebuild them as needed.",
        [Files(lambda: children(os.path.join(local, "D3DSCache")))])

    yield Target(
        "win.thumbnail_cache", GROUP, "Thumbnail cache",
        "Explorer's cached thumbnails. Files in use by Explorer are skipped; "
        "thumbnails are regenerated when folders are opened.",
        [Files(lambda: glob_in(os.path.join(local, r"Microsoft\Windows\Explorer"),
                               "thumbcache_*.db", "iconcache_*.db"))],
        default=False)

    locallow = os.path.join(os.path.dirname(local), "LocalLow")
    gpu_dirs = ([os.path.join(local, "AMD", d) for d in ("DxCache", "DxcCache", "VkCache", "GLCache", "OglCache")]
                + [os.path.join(local, "NVIDIA", d) for d in ("DXCache", "GLCache", "OptixCache")]
                + [os.path.join(locallow, r"NVIDIA\PerDriverVersion", d) for d in ("DXCache", "GLCache")]
                + [os.path.join(locallow, r"Intel\ShaderCache")])
    if any(map(os.path.isdir, gpu_dirs)):
        yield Target(
            "win.gpu_shader_cache", GROUP, "Graphics driver shader cache",
            "AMD / NVIDIA / Intel driver shader caches. Games may stutter briefly while they are rebuilt.",
            [Files(lambda: [p for d in gpu_dirs for p in children(d)])])

    packages = os.path.join(local, "Packages")
    yield Target(
        "win.store_app_cache", GROUP, "Microsoft Store app caches",
        "Temporary and web-cache folders of Store apps (Media Player, Photos, Calculator, ...). "
        "App settings and data are not touched.",
        [Files(lambda: [p for pkg in children(packages)
                        for d in (r"AC\INetCache", r"AC\Temp", "TempState") for p in children(os.path.join(pkg, d))],
               min_age_hours=1)])

    yield Target(
        "win.logs", GROUP, "Windows log files",
        rf"Component servicing (CBS) and DISM logs in {windir}\Logs older than 1 day.",
        [Files(lambda: glob_in(os.path.join(windir, r"Logs\CBS"), "*.log", "*.cab")
               + glob_in(os.path.join(windir, r"Logs\DISM"), "*.log*"), min_age_hours=24)],
        admin=True)

    do_cache = os.path.join(windir, r"ServiceProfiles\NetworkService\AppData\Local"
                                    r"\Microsoft\Windows\DeliveryOptimization\Cache")
    do_files = Files(lambda: children(do_cache))

    def do_clean() -> Stats:
        before = do_files.scan()
        result = winutil.run_powershell("Delete-DeliveryOptimizationCache -Force")
        after = do_files.scan()
        stats = Stats(files=before.files - after.files, bytes=before.bytes - after.bytes)
        if result.returncode:
            stats.note("Delivery Optimization: " + (result.stderr.strip().splitlines() or ["failed"])[0])
        return stats

    yield Target(
        "win.delivery_optimization", GROUP, "Delivery Optimization cache",
        "Windows Update files kept to share with other PCs. Cleared through Windows' own "
        "Delete-DeliveryOptimizationCache command.",
        [Custom(do_files.scan, do_clean)], admin=True)

    yield Target(
        "win.update_cache", GROUP, "Windows Update download cache",
        rf"Already-installed update packages in {windir}\SoftwareDistribution\Download.",
        [Files(lambda: children(os.path.join(windir, r"SoftwareDistribution\Download")),
               min_age_hours=24)],
        default=False, admin=True)
