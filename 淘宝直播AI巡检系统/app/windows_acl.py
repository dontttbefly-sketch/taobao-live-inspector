from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path


SidReader = Callable[[], str]
SddlReader = Callable[[Path], str]


def current_user_sid() -> str:
    """Return the current Windows token SID without a shell helper."""
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.argtypes = ()
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel.LocalFree.restype = ctypes.c_void_p
    advapi.OpenProcessToken.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    )
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = (
        ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR),
    )
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(
        kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)
    ):
        raise OSError(ctypes.get_last_error(), "OpenProcessToken failed")
    try:
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            raise OSError(
                ctypes.get_last_error(), "GetTokenInformation failed"
            )
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(
            token, 1, buffer, size, ctypes.byref(size)
        ):
            raise OSError(
                ctypes.get_last_error(), "GetTokenInformation failed"
            )

        class SidAndAttributes(ctypes.Structure):
            _fields_ = (
                ("sid", ctypes.c_void_p),
                ("attributes", wintypes.DWORD),
            )

        class TokenUser(ctypes.Structure):
            _fields_ = (("user", SidAndAttributes),)

        sid_pointer = ctypes.cast(
            buffer, ctypes.POINTER(TokenUser)
        ).contents.user.sid
        text = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid_pointer, ctypes.byref(text)):
            raise OSError(
                ctypes.get_last_error(), "ConvertSidToStringSidW failed"
            )
        try:
            return str(text.value or "")
        finally:
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel.CloseHandle(token)


def acl_sddl(path: Path) -> str:
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel.LocalFree.restype = ctypes.c_void_p
    advapi.GetNamedSecurityInfoW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    )
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = (
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD),
    )
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = (
        wintypes.BOOL
    )
    descriptor = ctypes.c_void_p()
    result = advapi.GetNamedSecurityInfoW(
        str(path), 1, 0x00000001 | 0x00000004, None, None, None, None,
        ctypes.byref(descriptor),
    )
    if result:
        raise OSError(int(result), "GetNamedSecurityInfoW failed")
    text = wintypes.LPWSTR()
    length = wintypes.DWORD()
    try:
        if not advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, 1, 0x00000001 | 0x00000004,
            ctypes.byref(text), ctypes.byref(length),
        ):
            raise OSError(
                ctypes.get_last_error(),
                "ConvertSecurityDescriptorToStringSecurityDescriptorW failed",
            )
        return str(text.value or "")
    finally:
        if text:
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        if descriptor:
            kernel.LocalFree(descriptor)


def acl_is_private(
    path: Path,
    *,
    sddl_reader: SddlReader = acl_sddl,
    sid_reader: SidReader = current_user_sid,
) -> bool:
    """Require a protected, current-user-owned allow-listed DACL."""
    try:
        sddl = sddl_reader(Path(path))
        current_sid_value = sid_reader()
    except (AttributeError, OSError):
        return False
    if not current_sid_value or "NO_ACCESS_CONTROL" in sddl:
        return False
    owner_match = re.search(r"O:(.+?)(?=[GDS]:)", sddl)
    dacl_marker = sddl.find("D:")
    if (
        owner_match is None
        or owner_match.group(1) != current_sid_value
        or dacl_marker < 0
    ):
        return False
    dacl = sddl[dacl_marker + 2:].split("S:", 1)[0]
    first_ace = dacl.find("(")
    if first_ace < 0:
        return False
    flags, encoded_aces = dacl[:first_ace], dacl[first_ace:]
    if (
        "P" not in flags
        or re.fullmatch(r"(?:\([^()]*\))+", encoded_aces) is None
    ):
        return False
    allowed = {
        current_sid_value, "SY", "BA", "S-1-5-18", "S-1-5-32-544",
    }
    current_user_allowed = False
    for encoded in re.findall(r"\(([^()]*)\)", encoded_aces):
        fields = encoded.split(";")
        if len(fields) != 6 or fields[0] != "A" or fields[5] not in allowed:
            return False
        current_user_allowed = (
            current_user_allowed or fields[5] == current_sid_value
        )
    return current_user_allowed


def private_sddl(sid: str) -> str:
    if not sid.startswith("S-") or any(character.isspace() for character in sid):
        raise ValueError("invalid Windows SID")
    return (
        "D:P"
        f"(A;OICI;FA;;;{sid})"
        "(A;OICI;FA;;;SY)"
        "(A;OICI;FA;;;BA)"
    )


def establish_private_acl(
    path: Path,
    *,
    sid_reader: SidReader = current_user_sid,
) -> None:
    """Install a protected current-user/System/Administrators DACL."""
    import ctypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel.LocalFree.restype = ctypes.c_void_p
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        ctypes.c_wchar_p, ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_ulong),
    )
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = (
        ctypes.c_int
    )
    advapi.GetSecurityDescriptorDacl.argtypes = (
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_int),
    )
    advapi.GetSecurityDescriptorDacl.restype = ctypes.c_int
    advapi.SetNamedSecurityInfoW.argtypes = (
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    )
    advapi.SetNamedSecurityInfoW.restype = ctypes.c_uint32
    sid = sid_reader()
    if not sid:
        raise OSError("current Windows SID unavailable")
    descriptor = ctypes.c_void_p()
    size = ctypes.c_ulong()
    sddl = private_sddl(sid)
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(descriptor), ctypes.byref(size)
    ):
        raise OSError(
            ctypes.get_last_error(),
            "ConvertStringSecurityDescriptorToSecurityDescriptorW failed",
        )
    dacl_present = ctypes.c_int()
    dacl_defaulted = ctypes.c_int()
    dacl = ctypes.c_void_p()
    try:
        if not advapi.GetSecurityDescriptorDacl(
            descriptor, ctypes.byref(dacl_present), ctypes.byref(dacl),
            ctypes.byref(dacl_defaulted),
        ) or not dacl_present.value:
            raise OSError(
                ctypes.get_last_error(), "GetSecurityDescriptorDacl failed"
            )
        result = advapi.SetNamedSecurityInfoW(
            str(path), 1, 0x00000004 | 0x80000000,
            None, None, dacl, None,
        )
        if result:
            raise OSError(int(result), "SetNamedSecurityInfoW failed")
    finally:
        kernel.LocalFree(descriptor)
