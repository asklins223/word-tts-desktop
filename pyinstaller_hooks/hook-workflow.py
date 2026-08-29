"""PyInstaller hook for the project's local ``workflow`` package.

The third-party hook with this module name targets an unrelated PyPI package
and calls ``copy_metadata('workflow')``.  This repository owns the package
namespace, so no distribution metadata needs to be copied.
"""

datas = []
binaries = []
hiddenimports = []
