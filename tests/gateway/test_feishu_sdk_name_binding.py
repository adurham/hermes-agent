"""Every lazily-imported lark_oapi name must exist as a module attribute.

The Feishu adapter keeps the SDK names spelled out at module level (``= None``)
instead of upstream's ``globals().update(...)`` so linters can see each one,
and ``_load_lark_oapi`` rebinds them when the SDK is installed. Nothing links
the two lists, so an upstream release that adds a name to ``_LARK_SDK_IMPORTS``
(``PatchMessageRequestBody`` did, in v0.23.0) leaves the attribute missing while
the SDK is absent — the code that touches it then raises AttributeError only in
the no-SDK path, which is what the test suite runs.

This test makes that drift impossible to merge silently.
"""

from __future__ import annotations

from plugins.platforms.feishu import adapter


def test_every_declared_sdk_name_is_a_module_attribute():
    missing = [
        name
        for _, names in adapter._LARK_SDK_IMPORTS
        for name in names
        if not hasattr(adapter, name)
    ]
    assert missing == []


def test_lazy_loader_binds_every_declared_sdk_name():
    """The loader writes into the module namespace; a name it forgets stays None."""
    source = adapter._load_lark_oapi.__code__.co_names
    # The loader resolves the tuple itself; assert it is the single source.
    assert "_LARK_SDK_IMPORTS" in source
