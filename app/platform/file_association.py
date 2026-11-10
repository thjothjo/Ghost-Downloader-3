from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from app.config.constants import DESKTOP_ID
from app.config.paths import EXECUTABLE_DIR, EXECUTABLE_PATH

if sys.platform == "win32":
    import ctypes
    import winreg

if TYPE_CHECKING:
    from app.models.pack import FileType

CLASSES_KEY = r"Software\Classes"
PROG_ID_PREFIX = "GhostDownloader."
SCHEME_NAME_PREFIX = "Ghost Downloader URL"


def save(fileTypes: list[FileType], schemes: list[str]) -> None:
    """fileTypes 和 schemes 是最终的完整状态，未列出的旧关联会被注销"""
    try:
        if sys.platform == "win32":
            saveWindows(fileTypes, schemes)
        elif sys.platform == "linux":
            saveLinux({ft.mimeType for ft in fileTypes} | {f"x-scheme-handler/{s}" for s in schemes})
    except Exception as e:
        logger.opt(exception=e).error("关联保存失败")


def saveWindows(fileTypes: list[FileType], schemes: list[str]) -> None:
    command = f'"{EXECUTABLE_PATH}" "%1"'
    progIds = set()
    for fileType in fileTypes:
        iconPath = str(EXECUTABLE_DIR / "app" / "assets" / "file_icons" / f"{fileType.icon}.ico").replace("/", "\\")
        for ext in fileType.extensions:
            progId = f"GhostDownloader{ext}"
            progIds.add(progId)
            for regPath, regValue in (
                (rf"{CLASSES_KEY}\{progId}", fileType.displayName),
                (rf"{CLASSES_KEY}\{progId}\DefaultIcon", iconPath),
                (rf"{CLASSES_KEY}\{progId}\shell\open\command", command),
                (rf"{CLASSES_KEY}\{ext}", progId),
            ):
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, regPath) as key:
                    winreg.SetValueEx(key, "", 0, winreg.REG_SZ, regValue)
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"{CLASSES_KEY}\{ext}\OpenWithProgids") as key:
                winreg.SetValueEx(key, progId, 0, winreg.REG_NONE, b"")

    for scheme in schemes:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"{CLASSES_KEY}\{scheme}") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f"{SCHEME_NAME_PREFIX} ({scheme})")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"{CLASSES_KEY}\{scheme}\shell\open\command") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)

    # 只删本程序写入的项，靠 ProgId 前缀和协议描述识别
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CLASSES_KEY) as classes:
        names = [winreg.EnumKey(classes, i) for i in range(winreg.QueryInfoKey(classes)[0])]
    for name in names:
        if name.startswith(PROG_ID_PREFIX) and name not in progIds:
            deleteProgId(name)
        elif not name.startswith(".") and name not in schemes and readDefaultValue(name).startswith(SCHEME_NAME_PREFIX):
            deleteKeyTree(rf"{CLASSES_KEY}\{name}")

    ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)


def deleteProgId(progId: str) -> None:
    ext = "." + progId.removeprefix(PROG_ID_PREFIX)
    deleteKeyTree(rf"{CLASSES_KEY}\{progId}")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{CLASSES_KEY}\{ext}\OpenWithProgids", 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, progId)
    except FileNotFoundError:
        pass
    if readDefaultValue(ext) == progId:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{CLASSES_KEY}\{ext}", 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, "")


def readDefaultValue(name: str) -> str:
    try:
        return winreg.QueryValue(winreg.HKEY_CURRENT_USER, rf"{CLASSES_KEY}\{name}")
    except OSError:
        return ""


def deleteKeyTree(path: str) -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            children = [winreg.EnumKey(key, i) for i in range(winreg.QueryInfoKey(key)[0])]
    except FileNotFoundError:
        return
    for child in children:
        deleteKeyTree(rf"{path}\{child}")
    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)


def saveLinux(mimes: set[str]) -> None:
    # 改写 .desktop 或 dbus 服务文件会让桌面重建缓存，启动时反复改写会卡住 KWin #1045
    desktopDir = Path.home() / ".local/share/applications"
    desktopFile = desktopDir / f"{DESKTOP_ID}.desktop"
    serviceFile = Path.home() / ".local/share/dbus-1/services" / f"{DESKTOP_ID}.service"

    oldMimes = loadMimeTypes(desktopFile)
    isDesktopChanged = saveFile(desktopFile, buildDesktopEntry(mimes) if mimes else None)
    saveFile(serviceFile, buildDBusService() if mimes else None)

    if isDesktopChanged:
        try:
            subprocess.run(["update-desktop-database", str(desktopDir)], check=False, capture_output=True)
        except FileNotFoundError:
            logger.warning("缺少 update-desktop-database, 跳过")

    # 只为新增类型设默认程序，不覆盖用户自己改过的选择
    for mime in sorted(mimes - oldMimes):
        try:
            subprocess.run(["xdg-mime", "default", f"{DESKTOP_ID}.desktop", mime], check=False, capture_output=True)
        except FileNotFoundError:
            logger.warning("缺少 xdg-mime, 跳过文件关联")
            break


def loadMimeTypes(desktopFile: Path) -> set[str]:
    if not desktopFile.exists():
        return set()
    for line in desktopFile.read_text(encoding="utf-8").splitlines():
        if line.startswith("MimeType="):
            return {m for m in line[9:].split(";") if m}
    return set()


def buildDesktopEntry(mimes: set[str]) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Ghost Downloader\n"
        f'Exec="{EXECUTABLE_PATH}" %U\n'
        "Icon=ghost-downloader\n"
        "Terminal=false\n"
        "Categories=Network;Utility;\n"
        "DBusActivatable=true\n"
        f"MimeType={';'.join(sorted(mimes))};\n"
    )


def buildDBusService() -> str:
    return (
        "[D-BUS Service]\n"
        f"Name={DESKTOP_ID}\n"
        f'Exec="{EXECUTABLE_PATH}"\n'
    )


def saveFile(file: Path, text: str | None) -> bool:
    """text 为 None 时删除文件。返回磁盘是否有变化"""
    oldText = file.read_text(encoding="utf-8") if file.exists() else None
    if oldText == text:
        return False
    if text is None:
        file.unlink()
    else:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8")
    return True
