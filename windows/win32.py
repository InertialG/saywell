"""win32: every Win32 call the saywell tray makes, behind one class so tests can swap in a fake.

Nothing here runs at import time: ctypes.windll and winreg are touched only when a Win32 object
is created, so the module imports (and the tests run their fakes) on any platform.
"""

import ctypes
import os
from ctypes import wintypes

WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN = 0x10, 0x11, 0x12, 0x5B, 0x5C
# Modifiers the user may still be holding when the popup inserts (Ctrl+Shift+Enter).
MODIFIER_KEYS = (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN)
# An unassigned virtual key: tapped between Alt down and up so the Alt press opens no menu bar
# (AutoHotkey's "menu mask key").
VK_MASK = 0xE8

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
ERROR_ALREADY_EXISTS = 183
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x1, 0x2
SW_RESTORE = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ULONG_PTR = ctypes.c_size_t


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    # The mouse member makes the union, and so INPUT, the size SendInput checks for.
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def message_at(address):
    """The MSG a native event filter receives as an address."""
    return wintypes.MSG.from_address(int(address))


class Win32:
    """The real thing. Window handles are plain ints; 0 means none."""

    def __init__(self):
        import winreg
        self.winreg = winreg
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u, k = self.user32, self.kernel32
        H = wintypes.HWND
        for name, args, res in (
                ("RegisterHotKey", [H, ctypes.c_int, wintypes.UINT, wintypes.UINT], wintypes.BOOL),
                ("UnregisterHotKey", [H, ctypes.c_int], wintypes.BOOL),
                ("GetForegroundWindow", [], H),
                ("SetForegroundWindow", [H], wintypes.BOOL),
                ("BringWindowToTop", [H], wintypes.BOOL),
                ("ShowWindow", [H, ctypes.c_int], wintypes.BOOL),
                ("IsIconic", [H], wintypes.BOOL),
                ("IsWindow", [H], wintypes.BOOL),
                ("GetClassNameW", [H, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
                ("GetWindowThreadProcessId", [H, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
                ("AttachThreadInput", [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL], wintypes.BOOL),
                ("GetAsyncKeyState", [ctypes.c_int], ctypes.c_short),
                ("MapVirtualKeyW", [wintypes.UINT, wintypes.UINT], wintypes.UINT),
                ("SendInput", [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int], wintypes.UINT)):
            fn = getattr(u, name)
            fn.argtypes, fn.restype = args, res
        for name, args, res in (
                ("GetCurrentThreadId", [], wintypes.DWORD),
                ("OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
                ("QueryFullProcessImageNameW", [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
                ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
                ("CreateMutexW", [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE)):
            fn = getattr(k, name)
            fn.argtypes, fn.restype = args, res

    # ---- hotkey -----------------------------------------------------------

    def register_hotkey(self, hotkey_id, mods, vk):
        """Register a thread hotkey (WM_HOTKEY with no window). Returns 0, or the Win32 error code."""
        if self.user32.RegisterHotKey(None, hotkey_id, mods, vk):
            return 0
        return ctypes.get_last_error() or 1

    def unregister_hotkey(self, hotkey_id):
        self.user32.UnregisterHotKey(None, hotkey_id)

    # ---- windows ----------------------------------------------------------

    def foreground_window(self):
        return self.user32.GetForegroundWindow() or 0

    def is_window(self, hwnd):
        return bool(hwnd) and bool(self.user32.IsWindow(hwnd))

    def window_class(self, hwnd):
        buf = ctypes.create_unicode_buffer(256)
        n = self.user32.GetClassNameW(hwnd, buf, len(buf))
        return buf.value[:n]

    def window_pid(self, hwnd):
        pid = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def window_process(self, hwnd):
        """The window's executable name, lowercased (e.g. "windowsterminal.exe"); "" when unknown."""
        handle = self.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, self.window_pid(hwnd))
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if not self.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return ""
            return os.path.basename(buf.value).lower()
        finally:
            self.kernel32.CloseHandle(handle)

    def focus_window(self, hwnd):
        """Make hwnd the foreground window despite the foreground lock. True if it is now."""
        u = self.user32
        if not self.is_window(hwnd):
            return False
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, SW_RESTORE)
        if u.SetForegroundWindow(hwnd) and self.foreground_window() == hwnd:
            return True
        # Share input state with the current foreground thread (and the target's), which lets
        # SetForegroundWindow through; if it still refuses, hold Alt while asking again.
        me = self.kernel32.GetCurrentThreadId()
        fg = self.foreground_window()
        threads = {u.GetWindowThreadProcessId(w, None) for w in (fg, hwnd) if w} - {0, me}
        attached = [t for t in threads if u.AttachThreadInput(me, t, True)]
        try:
            if not u.SetForegroundWindow(hwnd):
                self.send_keys([(VK_MENU, False, False)])
                u.SetForegroundWindow(hwnd)
                self.send_keys([(VK_MASK, False, False), (VK_MASK, True, False), (VK_MENU, True, False)])
            u.BringWindowToTop(hwnd)
        finally:
            for t in attached:
                u.AttachThreadInput(me, t, False)
        return self.foreground_window() == hwnd

    # ---- keyboard ---------------------------------------------------------

    def keys_down(self, vks):
        """The keys of vks physically or logically held right now."""
        return [vk for vk in vks if self.user32.GetAsyncKeyState(vk) & 0x8000]

    def send_keys(self, events):
        """Inject (vk, key_up, extended) events in order. Returns how many were sent."""
        inputs = (INPUT * len(events))()
        for inp, (vk, up, extended) in zip(inputs, events):
            inp.type = INPUT_KEYBOARD
            inp.u.ki = KEYBDINPUT(vk, self.user32.MapVirtualKeyW(vk, 0),
                                  (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if extended else 0),
                                  0, 0)
        return self.user32.SendInput(len(events), inputs, ctypes.sizeof(INPUT))

    # ---- single instance --------------------------------------------------

    def create_mutex(self, name):
        """Create (or open) a named mutex. Returns (handle, already_existed); the handle lives
        as long as the process, which is what makes it a single-instance guard."""
        handle = self.kernel32.CreateMutexW(None, False, name)
        return handle, ctypes.get_last_error() == ERROR_ALREADY_EXISTS

    # ---- HKCU Run key -----------------------------------------------------

    def run_value(self, name):
        """The command registered to start at logon under name, or None."""
        try:
            with self.winreg.OpenKey(self.winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                return self.winreg.QueryValueEx(key, name)[0]
        except FileNotFoundError:
            return None

    def set_run_value(self, name, command):
        with self.winreg.CreateKey(self.winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            self.winreg.SetValueEx(key, name, 0, self.winreg.REG_SZ, command)

    def delete_run_value(self, name):
        try:
            with self.winreg.OpenKey(self.winreg.HKEY_CURRENT_USER, RUN_KEY, 0, self.winreg.KEY_SET_VALUE) as key:
                self.winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass
