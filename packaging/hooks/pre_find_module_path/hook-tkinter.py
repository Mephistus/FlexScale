"""Allow bundling tkinter from the portable Python runtime used for builds."""


def pre_find_module_path(hook_api):
    # The build machine's Tcl runtime cannot create a Tk window during
    # PyInstaller analysis, but the Tcl/Tk files are supplied explicitly by
    # build_exe.ps1 and work when bundled.
    return None
